"""豆包实时双工语音适配器和 CI MP3 输出桥。"""

from __future__ import annotations

import asyncio
import base64
import json
import queue
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Callable

from .config import ServerConfig
from .errors import ProviderError


AudioCallback = Callable[[bytes], None]
TraceCallback = Callable[[str, dict[str, object]], None]
DoneCallback = Callable[["RealtimeMetrics"], None]
ErrorCallback = Callable[[Exception], None]

_REALTIME_SILENCE_CHUNK_BYTES = 640
_REALTIME_SILENCE_CHUNK_SECONDS = 0.020
_REALTIME_SILENCE_CHUNK = b"\x00" * _REALTIME_SILENCE_CHUNK_BYTES


@dataclass
class RealtimeMetrics:
    """保存单次实时模型会话的输入、输出和延迟统计。"""

    started_at: float = field(default_factory=time.perf_counter)
    input_audio_bytes: int = 0
    input_audio_chunks: int = 0
    endpoint_silence_bytes: int = 0
    endpoint_silence_chunks: int = 0
    output_pcm_bytes: int = 0
    output_pcm_chunks: int = 0
    output_mp3_bytes: int = 0
    output_mp3_chunks: int = 0
    post_endpoint_input_bytes: int = 0
    post_endpoint_input_chunks: int = 0
    provider_audio_max_gap_ms: int = 0
    mp3_output_max_gap_ms: int = 0
    first_provider_audio_ms: int | None = None
    first_mp3_ms: int | None = None
    pcm_to_first_mp3_ms: int | None = None
    asr_text: str = ""
    answer_text: str = ""
    finish_reason: str = ""

    def elapsed_ms(self) -> int:
        """返回从会话建立到当前时刻的毫秒数。"""

        return int((time.perf_counter() - self.started_at) * 1000)


class PcmToCiMp3Bridge:
    """将 24 kHz PCM 持续转换为 CI 可播放的 MP3 数据流。"""

    def __init__(
        self,
        input_rate: int,
        bitrate: str,
        queue_size: int,
        put_timeout_sec: float,
        close_timeout_sec: float,
        on_mp3: AudioCallback,
        on_error: ErrorCallback,
        on_trace: TraceCallback | None = None,
    ) -> None:
        """启动独立输入、输出线程，避免转码阻塞实时模型收包循环。"""

        try:
            import imageio_ffmpeg

            ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
        except Exception as exc:  # noqa: BLE001
            raise ProviderError(f"无法取得实时音频转码器: {exc}") from exc

        command = [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            # 原始 PCM 无需探测；关闭探测缓存，让转码器尽快吐出首个 MP3 帧。
            "-fflags",
            "+nobuffer",
            "-flags",
            "low_delay",
            "-analyzeduration",
            "0",
            "-probesize",
            "32",
            "-f",
            "s16le",
            "-ar",
            str(input_rate),
            "-ac",
            "1",
            "-i",
            "pipe:0",
            "-ar",
            "16000",
            "-ac",
            "1",
            "-c:a",
            "libmp3lame",
            "-b:a",
            bitrate,
            "-write_xing",
            "0",
            "-id3v2_version",
            "0",
            "-flush_packets",
            "1",
            "-f",
            "mp3",
            "pipe:1",
        ]
        try:
            self._process = subprocess.Popen(
                command,
                # stdin 使用无缓冲管道，避免 FFmpeg 提前退出时 BufferedWriter 析构再次 flush 并打印噪声。
                bufsize=0,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except OSError as exc:
            raise ProviderError(f"启动实时音频转码器失败: {exc}") from exc

        self._input_queue_maxsize = max(4, queue_size)
        self._put_timeout_sec = max(0.0, put_timeout_sec)
        self._close_timeout_sec = max(1.0, close_timeout_sec)
        self._input_bytes_per_second = max(1, input_rate * 2)
        self._input_queue: queue.Queue[bytes | None] = queue.Queue(maxsize=self._input_queue_maxsize)
        self._on_mp3 = on_mp3
        self._on_error = on_error
        self._on_trace = on_trace or (lambda _stage, _fields: None)
        self._closed = threading.Event()
        self._reader_failed = threading.Event()
        self._reader_error: Exception | None = None
        self._input_pcm_bytes = 0
        self._input_pcm_chunks = 0
        self._written_pcm_bytes = 0
        self._written_pcm_chunks = 0
        # CI 简易 MP3 解码器会误把任意 MP3 第 6~9 字节当作 ID3 长度；
        # 首次输出前暂存数据，以便安全修正该兼容字段。
        self._ci_first_mp3_pending = True
        self._ci_first_mp3_prefix = bytearray()
        self._writer = threading.Thread(target=self._write_loop, name="realtime-pcm-writer", daemon=True)
        self._reader = threading.Thread(target=self._read_loop, name="realtime-mp3-reader", daemon=True)
        self._writer.start()
        self._reader.start()

    def write(self, pcm: bytes) -> bool:
        """把一段模型 PCM 放入有界转码队列，满载时不阻塞调用线程。"""

        if not pcm or self._closed.is_set():
            return False
        try:
            if self._put_timeout_sec > 0:
                self._input_queue.put(pcm, timeout=self._put_timeout_sec)
            else:
                self._input_queue.put_nowait(pcm)
            self._input_pcm_bytes += len(pcm)
            self._input_pcm_chunks += 1
            return True
        except queue.Full:
            wait_ms = int(self._put_timeout_sec * 1000)
            qsize = self._input_queue.qsize()
            self._on_trace(
                "realtime_mp3_bridge_queue_full",
                {"qsize": qsize, "maxsize": self._input_queue_maxsize, "wait_ms": wait_ms},
            )
            self._on_error(
                ProviderError(
                    "实时 PCM 到 MP3 转码队列已满: "
                    f"qsize={qsize}, maxsize={self._input_queue_maxsize}, wait_ms={wait_ms}"
                )
            )
            return False

    def close(self, *, report_nonzero: bool = True, drain: bool = True) -> tuple[int | None, str]:
        """关闭 PCM 输入并等待 FFmpeg 刷出最后的 MP3 帧。

        入参含义：`report_nonzero` 控制 FFmpeg 非 0 退出码是否立刻上报为业务错误。
        返回值说明：返回 `(returncode, detail)`，用于上层区分正常收尾警告和真正转码失败。
        使用注意事项：正常 provider 完成路径会自行决定是否把 close-time 异常降级为 warning。
        """

        if self._closed.is_set():
            return self._process.returncode, ""
        self._closed.set()
        detail_parts: list[str] = []
        close_wait_sec = self._normal_close_timeout_sec() if drain else 1.0
        if drain:
            try:
                self._input_queue.put(None, timeout=close_wait_sec)
            except queue.Full:
                detail_parts.append(
                    "bridge close sentinel enqueue timeout: "
                    f"qsize={self._input_queue.qsize()}, maxsize={self._input_queue_maxsize}, "
                    f"input_pcm_bytes={self._input_pcm_bytes}, written_pcm_bytes={self._written_pcm_bytes}"
                )
                self._close_stdin_quietly()
            else:
                writer_deadline = time.monotonic() + close_wait_sec
                while self._writer.is_alive():
                    remaining = writer_deadline - time.monotonic()
                    if remaining <= 0 or self._reader_failed.wait(timeout=min(0.1, remaining)):
                        break
        else:
            try:
                self._input_queue.put_nowait(None)
            except queue.Full:
                detail_parts.append(
                    "bridge fast close with pending pcm: "
                    f"qsize={self._input_queue.qsize()}, maxsize={self._input_queue_maxsize}"
                )
            self._writer.join(timeout=0.5)
        reader_failed_during_close = self._reader_failed.is_set()
        if reader_failed_during_close:
            detail_parts.append(f"bridge MP3 downlink stopped: {self._reader_error}")
        if self._writer.is_alive():
            detail_parts.append(
                "bridge writer drain timeout: "
                f"qsize={self._input_queue.qsize()}, input_pcm_bytes={self._input_pcm_bytes}, "
                f"written_pcm_bytes={self._written_pcm_bytes}, wait_sec={close_wait_sec:.1f}"
            )
            self._close_stdin_quietly()
        process_wait_sec = close_wait_sec if drain and not reader_failed_during_close else 1.0
        try:
            self._process.wait(timeout=process_wait_sec)
        except subprocess.TimeoutExpired:
            detail_parts.append("ffmpeg process wait timeout")
            self._process.terminate()
            try:
                self._process.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                detail_parts.append("ffmpeg process kill after terminate timeout")
                self._process.kill()
                try:
                    self._process.wait(timeout=1.0)
                except subprocess.TimeoutExpired:
                    detail_parts.append("ffmpeg process still alive after kill")
                    pass
        self._reader.join(timeout=process_wait_sec)
        if self._reader.is_alive():
            detail_parts.append("bridge reader drain timeout")
        detail_text = ""
        if self._process.returncode not in (0, None):
            detail = b""
            if self._process.stderr is not None:
                detail = self._process.stderr.read()
            stderr_text = detail.decode("utf-8", errors="replace").strip()
            detail_parts.insert(0, f"ffmpeg returncode={self._process.returncode}")
            if stderr_text:
                detail_parts.append(f"stderr={stderr_text[:300]}")
        if detail_parts:
            detail_text = "; ".join(detail_parts)
            if report_nonzero:
                self._on_error(ProviderError(f"实时 MP3 转码失败: {detail_text}"))
        return self._process.returncode, detail_text

    def _normal_close_timeout_sec(self) -> float:
        """Return a close timeout scaled to the provider PCM duration."""

        audio_sec = self._input_pcm_bytes / self._input_bytes_per_second
        return min(180.0, max(self._close_timeout_sec, audio_sec + 10.0))

    def _close_stdin_quietly(self) -> None:
        """静默关闭 FFmpeg stdin，避免正常收尾阶段泄露 Python 文件析构警告。

        返回值说明：无返回值。
        使用注意事项：仅用于结束转码输入；关闭失败通常表示 FFmpeg 已经先行退出。
        """

        stdin = self._process.stdin
        if stdin is None:
            return
        try:
            stdin.close()
        except (OSError, ValueError):
            pass
        finally:
            self._process.stdin = None

    def _write_loop(self) -> None:
        """把排队的 PCM 安全写入 FFmpeg 标准输入。"""

        try:
            stdin = self._process.stdin
            if stdin is None:
                raise ProviderError("实时音频转码器没有标准输入")
            while True:
                chunk = self._input_queue.get()
                if chunk is None:
                    self._close_stdin_quietly()
                    return
                view = memoryview(chunk)
                written = 0
                while written < len(view):
                    count = stdin.write(view[written:])
                    if count is None or count <= 0:
                        raise OSError("FFmpeg stdin write returned no progress")
                    written += count
                self._written_pcm_bytes += len(chunk)
                self._written_pcm_chunks += 1
        except Exception as exc:  # noqa: BLE001
            if not self._closed.is_set():
                self._on_error(ProviderError(f"实时 PCM 写入转码器失败: {exc}"))

    def _read_loop(self) -> None:
        """持续读取 FFmpeg 标准输出并交给下行 WebSocket 回调。"""

        try:
            if self._process.stdout is None:
                raise ProviderError("实时音频转码器没有标准输出")
            while True:
                chunk = self._process.stdout.read(1024)
                if not chunk:
                    return
                self._forward_ci_compatible_mp3(chunk)
        except Exception as exc:  # noqa: BLE001
            self._reader_error = exc
            self._reader_failed.set()
            if not self._closed.is_set():
                self._on_error(ProviderError(f"实时 MP3 读取或下行失败: {exc}"))

    def _forward_ci_compatible_mp3(self, chunk: bytes) -> None:
        """将转码 MP3 下发给 CI，并只修正首帧的历史兼容字段。

        入参含义：``chunk`` 是 FFmpeg 连续输出的一段原始 MP3 字节。
        返回值说明：无返回值，完整可下发数据通过构造器传入的回调发送。
        使用注意事项：CI 当前简易播放器未确认 ``ID3`` 就读取第 6~9 字节作为
        ID3 长度。实时原始 MP3 的压缩数据常使该长度非零，导致 CI 错跳过几十 KB
        并停止拉取。这里只在首个 MPEG 帧存在时清零该四字节，后续字节保持原样。
        """

        if not self._ci_first_mp3_pending:
            self._on_mp3(chunk)
            return

        # 首个 stdout 读取理论上为 1024 字节；仍兼容极端情况下不足 10 字节的分段输出。
        self._ci_first_mp3_prefix.extend(chunk)
        if len(self._ci_first_mp3_prefix) < 10:
            return

        if self._ci_first_mp3_prefix[0] == 0xFF and (self._ci_first_mp3_prefix[1] & 0xE0) == 0xE0:
            self._ci_first_mp3_prefix[6:10] = b"\x00\x00\x00\x00"

        self._ci_first_mp3_pending = False
        output = bytes(self._ci_first_mp3_prefix)
        self._ci_first_mp3_prefix.clear()
        self._on_mp3(output)


class DoubaoRealtimeSession:
    """豆包实时双工会话的线程安全服务端适配器。"""

    def __init__(
        self,
        config: ServerConfig,
        on_audio: AudioCallback,
        on_trace: TraceCallback,
        on_done: DoneCallback,
        on_error: ErrorCallback,
        context_items: list[dict[str, str]] | None = None,
    ) -> None:
        """保存回调并创建有界上行命令队列；网络连接在 start 后台线程中建立。"""

        self.config = config
        self._on_audio = on_audio
        self._on_trace = on_trace
        self._on_done = on_done
        self._on_error = on_error
        self._context_items = list(context_items or [])
        self.metrics = RealtimeMetrics()
        self._commands: queue.Queue[tuple[str, bytes | None]] = queue.Queue(
            maxsize=max(4, config.realtime_command_queue_size)
        )
        self._started = threading.Event()
        self._finished = threading.Event()
        self._cancelled = threading.Event()
        self._thread: threading.Thread | None = None
        self._start_error: Exception | None = None
        self._event_id = 0
        self._event_id_lock = threading.Lock()
        self._bridge: PcmToCiMp3Bridge | None = None
        self._commit_at: float | None = None
        # 最近一次收到有效供应商 PCM 的单调时刻，用于半截 TTS 响应的快速回收。
        self._last_provider_audio_at: float | None = None
        self._last_mp3_at: float | None = None
        # 标记供应商已完成断句；仅用于取消 3 秒端点检测上限，不停止后续静音续传。
        self._endpoint_detected_event: asyncio.Event | None = None
        self._endpoint_detected_reason: str | None = None
        self._provider_turn_event_seen = False
        self._answer_parts: list[str] = []
        self._answer_completed_logged = False
        self._response_done_seen = False
        self._response_done_at: float | None = None
        self._output_audio_done_seen = False
        self._post_endpoint_drop_lock = threading.Lock()

    def start(self) -> None:
        """建立供应商会话并等待 session.created，失败时向调用方返回明确错误。"""

        if self._thread is not None:
            raise ProviderError("实时会话已经启动")
        self.config.require_doubao_realtime()
        self._thread = threading.Thread(target=self._thread_main, name="doubao-realtime", daemon=True)
        self._thread.start()
        if not self._started.wait(timeout=self.config.realtime_connect_timeout_sec):
            self.cancel()
            raise ProviderError("等待豆包实时会话创建超时")
        if self._start_error is not None:
            raise ProviderError(str(self._start_error)) from self._start_error

    def append_audio(self, pcm: bytes) -> bool:
        """无阻塞地加入一段 16 kHz PCM，上行队列满载时返回 false。"""

        if not pcm or self._cancelled.is_set() or self._finished.is_set():
            return False
        self.metrics.input_audio_bytes += len(pcm)
        self.metrics.input_audio_chunks += 1
        if self._provider_endpoint_detected():
            self._record_post_endpoint_input_drop(len(pcm), "producer")
            return True
        try:
            self._commands.put_nowait(("append", pcm))
            return True
        except queue.Full:
            self._on_trace("realtime_input_queue_full", {"queue_size": self._commands.qsize()})
            return False

    def commit(self) -> bool:
        """按音频加入顺序报告设备采集完成，由服务端继续补零直到豆包断句。"""

        return self._enqueue_control("commit")

    def cancel(self) -> None:
        """关闭供应商会话并阻止任何迟到音频再下发到设备。"""

        if self._cancelled.is_set() or self._finished.is_set():
            return
        self._cancelled.set()
        self._enqueue_control("cancel")

    def _enqueue_control(self, name: str) -> bool:
        """把提交或取消控制命令加入与 PCM 共用的顺序队列。"""

        if self._finished.is_set():
            return False
        try:
            self._commands.put_nowait((name, None))
            return True
        except queue.Full:
            self._on_trace("realtime_control_queue_full", {"command": name, "queue_size": self._commands.qsize()})
            return False

    def _next_event_id(self) -> str:
        """生成供应商事件唯一标识，确保单会话内请求可追踪。"""

        with self._event_id_lock:
            self._event_id += 1
            return f"server_event_{self._event_id}"

    def _thread_main(self) -> None:
        """运行专用 asyncio 事件循环，保持供应商收包和发包并发。"""

        try:
            asyncio.run(self._run())
        except Exception as exc:  # noqa: BLE001
            if not self._started.is_set():
                self._start_error = exc
                self._started.set()
            elif not self._cancelled.is_set():
                self._on_error(exc if isinstance(exc, ProviderError) else ProviderError(str(exc)))
        finally:
            if self._bridge is not None:
                self._bridge.close(report_nonzero=False, drain=False)
            self._finished.set()
            if not self._started.is_set():
                self._started.set()

    async def _run(self) -> None:
        """创建会话后并发处理上行命令和供应商下行事件。"""

        try:
            import websockets
        except ImportError as exc:
            raise ProviderError("缺少 websockets，无法使用实时语音模型") from exc

        headers = {"X-Api-Key": self.config.doubao_realtime_api_key}
        connect_kwargs = {"ping_interval": None, "open_timeout": self.config.realtime_connect_timeout_sec}
        try:
            ws = await websockets.connect(
                self.config.doubao_realtime_url,
                additional_headers=headers,
                **connect_kwargs,
            )
        except TypeError:
            ws = await websockets.connect(
                self.config.doubao_realtime_url,
                extra_headers=headers,
                **connect_kwargs,
            )

        async with ws:
            await ws.send(json.dumps(self._session_create_event(), ensure_ascii=False, separators=(",", ":")))
            await self._wait_session_created(ws)
            await self._send_context_items(ws)
            self._bridge = PcmToCiMp3Bridge(
                input_rate=self.config.realtime_output_sample_rate,
                bitrate=self.config.realtime_mp3_bitrate,
                queue_size=self.config.realtime_pcm_bridge_queue_size,
                put_timeout_sec=self.config.realtime_pcm_bridge_put_timeout_sec,
                close_timeout_sec=self.config.realtime_pcm_bridge_close_timeout_sec,
                on_mp3=self._handle_mp3,
                on_error=self._handle_bridge_error,
                on_trace=self._on_trace,
            )
            self._started.set()
            self._on_trace("realtime_session_ready", {"provider": "doubao"})
            self._endpoint_detected_event = asyncio.Event()
            sender = asyncio.create_task(self._send_commands(ws))
            receiver = asyncio.create_task(self._receive_events(ws))
            try:
                done, _ = await asyncio.wait({sender, receiver}, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    task.result()
            finally:
                for task in (sender, receiver):
                    if not task.done():
                        task.cancel()
                await asyncio.gather(sender, receiver, return_exceptions=True)

    def _session_create_event(self) -> dict[str, object]:
        """构造与已验证 POC 一致的豆包实时会话创建事件。"""

        return {
            "type": "session.create",
            "event_id": self._next_event_id(),
            "session": {
                "id": str(uuid.uuid4()),
                "model": self.config.doubao_realtime_model,
                "instructions": self.config.doubao_realtime_system_prompt,
                "audio": {
                    "input": {"format": {"type": self.config.doubao_realtime_asr_format, "rate": 16000}},
                    "output": {
                        "format": {"type": self.config.doubao_realtime_tts_format, "rate": self.config.realtime_output_sample_rate},
                        "voice": self.config.doubao_realtime_voice,
                    },
                },
                "tools": [],
            },
            "extension": {"asr": {"extra": {}}, "tts": {"extra": {}}, "dialog": {"extra": {"enable_music": False}}},
        }

    def _conversation_item_create_event(self, items: list[dict[str, object]]) -> dict[str, object]:
        """功能说明：把实时专用历史消息封装成豆包上下文批量注入事件。"""

        return {
            "type": "conversation.item.create",
            "event_id": self._next_event_id(),
            "items": items,
        }

    async def _send_context_items(self, ws: object) -> None:
        """功能说明：在新一轮实时音频进入前注入豆包端到端历史上下文。"""

        items: list[dict[str, object]] = []
        for item in self._context_items:
            role = str(item.get("role") or "")
            text = str(item.get("text") or "").strip()
            if role not in ("user", "assistant") or not text:
                continue
            items.append(
                {
                    "type": "message",
                    "role": role,
                    "content": [{"type": "input_text", "text": text}],
                }
            )
        if items:
            await ws.send(json.dumps(self._conversation_item_create_event(items), ensure_ascii=False, separators=(",", ":")))  # type: ignore[attr-defined]
            self._on_trace("realtime_memory_context_sent", {"items": len(items), "turns": len(items) // 2})

    async def _wait_session_created(self, ws: object) -> None:
        """在启动时仅消费 session.created 或 error，避免把事件丢给正式收包循环。"""

        deadline = time.monotonic() + self.config.realtime_connect_timeout_sec
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ProviderError("豆包实时 session.created 超时")
            raw = await asyncio.wait_for(ws.recv(), timeout=remaining)  # type: ignore[attr-defined]
            event = self._parse_event(raw)
            event_type = str(event.get("type") or "")
            if event_type == "session.created":
                self._on_trace("realtime_session_created", {"session_id": str((event.get("session") or {}).get("id") or "")})
                return
            if event_type == "error":
                raise ProviderError(self._event_error_text(event))
            self._on_trace("realtime_start_event", {"type": event_type})

    async def _send_commands(self, ws: object) -> None:
        """按队列顺序发送真实 PCM；采集结束后以服务端自适应静音驱动豆包断句。"""

        tail_active = False
        tail_deadline = 0.0
        next_tail_at = 0.0
        tail_packets = 0

        while not self._finished.is_set():
            if tail_active:
                try:
                    name, payload = self._commands.get_nowait()
                except queue.Empty:
                    now = time.monotonic()
                    if (
                        not self._provider_turn_event_seen
                        and self.config.realtime_endpoint_no_event_max_sec > 0
                        and self._commit_at is not None
                        and now >= self._commit_at + self.config.realtime_endpoint_no_event_max_sec
                    ):
                        self._on_trace(
                            "realtime_endpoint_no_event_timeout",
                            {
                                "max_ms": int(self.config.realtime_endpoint_no_event_max_sec * 1000),
                                "packets": tail_packets,
                            },
                        )
                        raise ProviderError(
                            f"豆包实时在 {self.config.realtime_endpoint_no_event_max_sec:g} 秒内未收到转写或回复事件"
                        )
                    if (
                        (self._endpoint_detected_event is None or not self._endpoint_detected_event.is_set())
                        and now >= tail_deadline
                    ):
                        self._on_trace(
                            "realtime_endpoint_silence_timeout",
                            {
                                "max_ms": int(self.config.realtime_endpoint_silence_max_sec * 1000),
                                "packets": tail_packets,
                            },
                        )
                        raise ProviderError(
                            f"豆包实时端点检测在 {self.config.realtime_endpoint_silence_max_sec:g} 秒静音内未完成"
                        )
                    if now < next_tail_at:
                        await asyncio.sleep(min(_REALTIME_SILENCE_CHUNK_SECONDS, next_tail_at - now))
                        continue
                    event = {
                        "type": "input_audio_buffer.append",
                        "audio": base64.b64encode(_REALTIME_SILENCE_CHUNK).decode("ascii"),
                    }
                    await ws.send(json.dumps(event, separators=(",", ":")))  # type: ignore[attr-defined]
                    tail_packets += 1
                    self.metrics.endpoint_silence_chunks += 1
                    self.metrics.endpoint_silence_bytes += _REALTIME_SILENCE_CHUNK_BYTES
                    next_tail_at += _REALTIME_SILENCE_CHUNK_SECONDS
                    continue
            else:
                try:
                    name, payload = await asyncio.to_thread(self._commands.get, True, 0.1)
                except queue.Empty:
                    continue

            if name == "append":
                if self._cancelled.is_set() or payload is None:
                    continue
                if self._provider_endpoint_detected():
                    self._record_post_endpoint_input_drop(len(payload), "sender")
                    continue
                event = {"type": "input_audio_buffer.append", "audio": base64.b64encode(payload).decode("ascii")}
                await ws.send(json.dumps(event, separators=(",", ":")))  # type: ignore[attr-defined]
            elif name == "commit":
                if self._cancelled.is_set():
                    continue
                if tail_active:
                    self._on_trace("realtime_capture_finish_duplicate", {})
                    continue
                self._commit_at = time.monotonic()
                endpoint_already_detected = self._provider_endpoint_detected()
                # 豆包双工回复期间仍需要连续输入时钟。即使端点已经提前确认，也要从设备
                # PCM 切换为 20 ms 零 PCM，不能停止上行，否则长回复可能在中途停住。
                tail_active = True
                tail_deadline = self._commit_at + self.config.realtime_endpoint_silence_max_sec
                next_tail_at = self._commit_at
                tail_packets = 0
                self._on_trace(
                    "realtime_capture_finished",
                    {
                        "input_audio_bytes": self.metrics.input_audio_bytes,
                        "endpoint_detection_max_ms": int(self.config.realtime_endpoint_silence_max_sec * 1000),
                        "endpoint_already_detected": endpoint_already_detected,
                        "endpoint_reason": self._endpoint_detected_reason,
                        "post_endpoint_input_bytes": self.metrics.post_endpoint_input_bytes,
                        "silence_continues_until": "response_done",
                    },
                )
                self._on_trace(
                    "realtime_endpoint_silence_started",
                    {
                        "chunk_bytes": _REALTIME_SILENCE_CHUNK_BYTES,
                        "chunk_ms": 20,
                        "endpoint_already_detected": endpoint_already_detected,
                    },
                )
            elif name == "cancel":
                self._on_trace("realtime_session_cancel", {})
                try:
                    await ws.send(json.dumps({"type": "session.close", "event_id": self._next_event_id()}, separators=(",", ":")))  # type: ignore[attr-defined]
                finally:
                    await ws.close()  # type: ignore[attr-defined]
                return

    def _mark_provider_endpoint(self, reason: str) -> None:
        """记录供应商已断句，但持续补零直至本轮回复真正结束。

        入参含义：``reason`` 是识别完成、文本开始或音频开始等供应商边界事件。
        返回值说明：无返回值。
        使用注意事项：豆包双工示例会在模型回复期间持续发送 20 ms 零 PCM；此处
        只解除“尚未断句”的三秒保护，不能像旧逻辑一样停止发送静音。
        """

        if self._endpoint_detected_event is None or self._endpoint_detected_event.is_set():
            return
        self._endpoint_detected_reason = reason
        self._endpoint_detected_event.set()
        self._on_trace("realtime_endpoint_detected", {"reason": reason})

    def _provider_endpoint_detected(self) -> bool:
        """返回供应商是否已经确定本轮用户输入端点。"""

        return self._endpoint_detected_event is not None and self._endpoint_detected_event.is_set()

    def _record_post_endpoint_input_drop(self, byte_count: int, source: str) -> None:
        """统计供应商端点之后到达的真实麦克风数据，避免回复期回声重新进入模型。"""

        if byte_count <= 0:
            return
        with self._post_endpoint_drop_lock:
            self.metrics.post_endpoint_input_bytes += byte_count
            self.metrics.post_endpoint_input_chunks += 1
            chunks = self.metrics.post_endpoint_input_chunks
            total_bytes = self.metrics.post_endpoint_input_bytes
        if chunks == 1 or chunks % 100 == 0:
            self._on_trace(
                "realtime_post_endpoint_input_dropped",
                {
                    "source": source,
                    "chunks": chunks,
                    "bytes": total_bytes,
                    "endpoint_reason": self._endpoint_detected_reason,
                },
            )

    async def _receive_events(self, ws: object) -> None:
        """处理实时 ASR、文本和 PCM 音频事件，并在一轮完成后收尾。"""

        while not self._cancelled.is_set():
            response_wait_timeout: float | None = None
            response_wait_reason = ""
            now = time.monotonic()
            if self._commit_at is not None and self.metrics.first_provider_audio_ms is None:
                elapsed_after_commit = now - self._commit_at
                response_wait_timeout = self.config.realtime_first_response_timeout_sec - elapsed_after_commit
                response_wait_reason = "first_provider_audio"
                if response_wait_timeout <= 0:
                    raise ProviderError(
                        f"豆包实时提交后 {self.config.realtime_first_response_timeout_sec:g} 秒仍未收到首段音频"
                    )
            if self._last_provider_audio_at is not None:
                if self._response_done_seen:
                    done_grace = self.config.realtime_response_done_grace_sec
                    done_reference_at = max(
                        self._last_provider_audio_at,
                        self._response_done_at or self._last_provider_audio_at,
                    )
                    done_grace_remaining = done_grace - (now - done_reference_at)
                    if done_grace_remaining <= 0:
                        self._on_trace(
                            "realtime_response_done_audio_idle",
                            {
                                "grace_ms": int(done_grace * 1000),
                                "output_pcm_bytes": self.metrics.output_pcm_bytes,
                                "output_pcm_chunks": self.metrics.output_pcm_chunks,
                                "output_mp3_bytes": self.metrics.output_mp3_bytes,
                            },
                        )
                        self._finish_normal("response_done_audio_idle")
                        return
                    response_wait_timeout = (
                        done_grace_remaining
                        if response_wait_timeout is None
                        else min(response_wait_timeout, done_grace_remaining)
                    )
                    response_wait_reason = "response_done_audio_idle"
                output_idle_timeout = self.config.realtime_output_idle_timeout_sec
                output_idle_remaining = output_idle_timeout - (now - self._last_provider_audio_at)
                if output_idle_remaining <= 0:
                    self._on_trace(
                        "realtime_provider_audio_idle_timeout",
                        {"timeout_ms": int(output_idle_timeout * 1000)},
                    )
                    raise ProviderError(
                        f"豆包实时首段音频后连续 {output_idle_timeout:g} 秒未收到后续音频或完成事件"
                    )
                response_wait_timeout = (
                    output_idle_remaining
                    if response_wait_timeout is None
                    else min(response_wait_timeout, output_idle_remaining)
                )
                if not response_wait_reason:
                    response_wait_reason = "output_audio_idle"
            try:
                if response_wait_timeout is None:
                    raw = await ws.recv()  # type: ignore[attr-defined]
                else:
                    raw = await asyncio.wait_for(ws.recv(), timeout=response_wait_timeout)  # type: ignore[attr-defined]
            except asyncio.TimeoutError as exc:
                if response_wait_reason == "response_done_audio_idle":
                    self._on_trace(
                        "realtime_response_done_audio_idle",
                        {
                            "grace_ms": int(self.config.realtime_response_done_grace_sec * 1000),
                            "output_pcm_bytes": self.metrics.output_pcm_bytes,
                            "output_pcm_chunks": self.metrics.output_pcm_chunks,
                            "output_mp3_bytes": self.metrics.output_mp3_bytes,
                        },
                    )
                    self._finish_normal("response_done_audio_idle")
                    return
                if response_wait_reason == "output_audio_idle":
                    output_idle_timeout = self.config.realtime_output_idle_timeout_sec
                    self._on_trace(
                        "realtime_provider_audio_idle_timeout",
                        {"timeout_ms": int(output_idle_timeout * 1000)},
                    )
                    raise ProviderError(
                        f"豆包实时首段音频后连续 {output_idle_timeout:g} 秒未收到后续音频或完成事件"
                    ) from exc
                raise ProviderError(
                    f"豆包实时提交后 {self.config.realtime_first_response_timeout_sec:g} 秒仍未收到首段音频"
                ) from exc
            event = self._parse_event(raw)
            event_type = str(event.get("type") or "")
            if event_type.startswith("conversation.item.input_audio_transcription.") or event_type.startswith("response.output_") or event_type == "response.done":
                self._provider_turn_event_seen = True
            if event_type == "response.output_audio.delta":
                self._mark_provider_endpoint("first_provider_audio")
                raw_delta = str(event.get("delta") or "")
                try:
                    pcm = base64.b64decode(raw_delta)
                except (ValueError, TypeError) as exc:
                    raise ProviderError(f"实时音频 Base64 解码失败: {exc}") from exc
                if pcm:
                    self.metrics.output_pcm_bytes += len(pcm)
                    self.metrics.output_pcm_chunks += 1
                    provider_audio_at = time.monotonic()
                    if self._last_provider_audio_at is not None:
                        provider_gap_ms = int((provider_audio_at - self._last_provider_audio_at) * 1000)
                        self.metrics.provider_audio_max_gap_ms = max(
                            self.metrics.provider_audio_max_gap_ms,
                            provider_gap_ms,
                        )
                    self._last_provider_audio_at = provider_audio_at
                    if self.metrics.first_provider_audio_ms is None:
                        self.metrics.first_provider_audio_ms = self.metrics.elapsed_ms()
                        self._on_trace("realtime_first_provider_audio", {"first_provider_audio_ms": self.metrics.first_provider_audio_ms})
                    if self._bridge is None or not self._bridge.write(pcm):
                        raise ProviderError("实时 PCM 到 MP3 桥不可用或已满")
            elif event_type == "response.output_audio.started":
                self._mark_provider_endpoint("output_audio_started")
                self._on_trace("realtime_provider_event", {"type": event_type})
            elif event_type == "response.output_text.delta":
                self._mark_provider_endpoint("response_text_started")
                delta = str(event.get("delta") or "")
                if delta:
                    self._answer_parts.append(delta)
                    self.metrics.answer_text = "".join(self._answer_parts).strip()
                self._on_trace("realtime_text_delta", {"chars": len(delta), "total_chars": len(self.metrics.answer_text)})
            elif event_type == "response.output_text.done":
                self._mark_provider_endpoint("response_text_done")
                self._finish_answer_text("output_text_done")
                self._on_trace("realtime_provider_event", {"type": event_type})
            elif event_type == "response.output_audio.done":
                self._mark_provider_endpoint("output_audio_done")
                self._output_audio_done_seen = True
                self._finish_answer_text(event_type)
                self._on_trace(
                    "realtime_provider_event",
                    {
                        "type": event_type,
                        "output_pcm_bytes": self.metrics.output_pcm_bytes,
                        "output_pcm_chunks": self.metrics.output_pcm_chunks,
                        "output_mp3_bytes": self.metrics.output_mp3_bytes,
                    },
                )
                self._finish_normal(event_type)
                return
            elif event_type == "response.done":
                self._mark_provider_endpoint("response_done")
                self._response_done_seen = True
                self._response_done_at = time.monotonic()
                self._finish_answer_text(event_type)
                self._on_trace(
                    "realtime_provider_event",
                    {
                        "type": event_type,
                        "output_audio_done_seen": self._output_audio_done_seen,
                        "output_pcm_bytes": self.metrics.output_pcm_bytes,
                        "output_pcm_chunks": self.metrics.output_pcm_chunks,
                        "output_mp3_bytes": self.metrics.output_mp3_bytes,
                    },
                )
                if self.metrics.first_provider_audio_ms is None:
                    self._finish_normal(event_type)
                    return
            elif event_type == "error":
                self._mark_provider_endpoint("provider_error")
                raise ProviderError(self._event_error_text(event))
            elif event_type.endswith("transcription.completed"):
                self._mark_provider_endpoint("transcription_completed")
                self.metrics.asr_text = str(event.get("transcript") or event.get("text") or "")
                self._on_trace("realtime_asr_completed", {"text": self.metrics.asr_text})
            else:
                self._on_trace("realtime_provider_event", {"type": event_type})

    def _finish_answer_text(self, reason: str) -> None:
        """功能说明：汇总并只打印一次豆包实时模型的最终文字回复。"""

        self.metrics.answer_text = "".join(self._answer_parts).strip()
        if self._answer_completed_logged:
            return
        self._answer_completed_logged = True
        self._on_trace(
            "realtime_answer_completed",
            {"text": self.metrics.answer_text, "chars": len(self.metrics.answer_text), "reason": reason},
        )

    def _handle_mp3(self, chunk: bytes) -> None:
        """记录首个 MP3 时刻后将转码结果转交给服务端 WebSocket 下行。"""

        if self._cancelled.is_set() or not chunk:
            return
        self.metrics.output_mp3_bytes += len(chunk)
        self.metrics.output_mp3_chunks += 1
        mp3_at = time.monotonic()
        if self._last_mp3_at is not None:
            mp3_gap_ms = int((mp3_at - self._last_mp3_at) * 1000)
            self.metrics.mp3_output_max_gap_ms = max(self.metrics.mp3_output_max_gap_ms, mp3_gap_ms)
        self._last_mp3_at = mp3_at
        if self.metrics.first_mp3_ms is None:
            self.metrics.first_mp3_ms = self.metrics.elapsed_ms()
            if self.metrics.first_provider_audio_ms is not None:
                self.metrics.pcm_to_first_mp3_ms = max(
                    0,
                    self.metrics.first_mp3_ms - self.metrics.first_provider_audio_ms,
                )
            self._on_trace(
                "realtime_first_mp3",
                {
                    "first_mp3_ms": self.metrics.first_mp3_ms,
                    "pcm_to_first_mp3_ms": self.metrics.pcm_to_first_mp3_ms,
                    "chunk_bytes": len(chunk),
                },
            )
        self._on_audio(chunk)

    def _handle_bridge_error(self, exc: Exception) -> None:
        """将异步转码错误转化为本会话失败，避免静默生成残缺音频。"""

        if not self._cancelled.is_set():
            # 下游 WebSocket 或 MP3 reader 已经失败时立刻关闭供应商会话，避免继续生成的
            # PCM 堵住 FFmpeg 管道，并让下一轮对话尽快恢复。
            self.cancel()
            self._on_error(exc)

    def _finish_normal(self, reason: str = "finish_normal") -> None:
        """关闭转码器、刷完尾帧并向服务端报告本轮自然结束。"""

        if self._cancelled.is_set() or self._finished.is_set():
            return
        self.metrics.finish_reason = reason
        if self._bridge is not None:
            returncode, close_detail = self._bridge.close(report_nonzero=False)
            self._bridge = None
            if self._cancelled.is_set():
                return
            if returncode not in (0, None) or close_detail:
                self._on_trace(
                    "realtime_mp3_bridge_close_warning",
                    {
                        "returncode": returncode,
                        "detail": close_detail[:300],
                        "finish_reason": reason,
                        "output_pcm_bytes": self.metrics.output_pcm_bytes,
                        "output_pcm_chunks": self.metrics.output_pcm_chunks,
                        "output_mp3_bytes": self.metrics.output_mp3_bytes,
                    },
                )
                if self.metrics.output_mp3_bytes <= 0:
                    self._on_error(ProviderError(f"实时 MP3 转码失败: {close_detail}"))
                    return
        self._finish_answer_text(reason)
        self._on_trace(
            "realtime_response_done",
            {
                "finish_reason": reason,
                "input_audio_bytes": self.metrics.input_audio_bytes,
                "endpoint_silence_bytes": self.metrics.endpoint_silence_bytes,
                "endpoint_silence_chunks": self.metrics.endpoint_silence_chunks,
                "output_pcm_bytes": self.metrics.output_pcm_bytes,
                "output_pcm_chunks": self.metrics.output_pcm_chunks,
                "output_mp3_bytes": self.metrics.output_mp3_bytes,
                "output_mp3_chunks": self.metrics.output_mp3_chunks,
                "post_endpoint_input_bytes": self.metrics.post_endpoint_input_bytes,
                "post_endpoint_input_chunks": self.metrics.post_endpoint_input_chunks,
                "provider_audio_max_gap_ms": self.metrics.provider_audio_max_gap_ms,
                "mp3_output_max_gap_ms": self.metrics.mp3_output_max_gap_ms,
                "answer_chars": len(self.metrics.answer_text),
                "pcm_to_first_mp3_ms": self.metrics.pcm_to_first_mp3_ms,
                "total_ms": self.metrics.elapsed_ms(),
            },
        )
        self._on_done(self.metrics)

    @staticmethod
    def _parse_event(raw: object) -> dict[str, object]:
        """把供应商 WebSocket 文本帧解析为 JSON 对象。"""

        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        try:
            event = json.loads(str(raw))
        except json.JSONDecodeError as exc:
            raise ProviderError(f"实时服务返回非法 JSON: {exc}") from exc
        if not isinstance(event, dict):
            raise ProviderError("实时服务返回的事件不是对象")
        return event

    @staticmethod
    def _event_error_text(event: dict[str, object]) -> str:
        """提取供应商 error 事件的可读信息。"""

        error = event.get("error")
        if isinstance(error, dict):
            return str(error.get("message") or error)
        return str(error or event)
