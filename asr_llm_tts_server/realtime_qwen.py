"""千问端到端实时语音 Push-to-Talk 适配器。"""

from __future__ import annotations

import asyncio
import base64
import json
import math
import queue
import threading
import time
from typing import Callable

from .config import ServerConfig
from .errors import ProviderError
from .realtime import PcmToCiMp3Bridge, RealtimeMetrics


AudioCallback = Callable[[bytes], None]
TraceCallback = Callable[[str, dict[str, object]], None]
DoneCallback = Callable[[RealtimeMetrics], None]
ErrorCallback = Callable[[Exception], None]

_PCM_20MS_BYTES = 640
_PCM_20MS_SILENCE = b"\x00" * _PCM_20MS_BYTES


class QwenRealtimeSession:
    """把设备侧实时协议映射为千问原生 Push-to-Talk 会话。

    入参含义：`config` 提供千问连接参数，四个回调分别承接 MP3 下行、日志、正常完成和异常。
    返回值说明：实例通过 `start/append_audio/commit/cancel` 暴露与豆包适配器一致的接口。
    使用注意事项：本类固定发送 `turn_detection: null`，不允许退化为 `server_vad`。
    """

    def __init__(
        self,
        config: ServerConfig,
        on_audio: AudioCallback,
        on_trace: TraceCallback,
        on_done: DoneCallback,
        on_error: ErrorCallback,
        context_items: list[dict[str, str]] | None = None,
    ) -> None:
        """保存回调与历史上下文，并创建保持 PCM/控制命令顺序的有界队列。"""

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
        self._commit_lock = threading.Lock()
        self._commit_enqueued = False
        self._bridge: PcmToCiMp3Bridge | None = None
        self._session_updated_event: asyncio.Event | None = None
        self._commit_at: float | None = None
        self._last_provider_audio_at: float | None = None
        self._last_mp3_at: float | None = None
        self._answer_parts: list[str] = []
        self._asr_parts: list[str] = []
        self._answer_completed_logged = False

    def start(self) -> None:
        """建立千问 WSS 会话并等待 `session.updated`，失败时同步返回异常。"""

        if self._thread is not None:
            raise ProviderError("千问实时会话已经启动")
        self.config.require_qwen_realtime()
        self._thread = threading.Thread(target=self._thread_main, name="qwen-realtime", daemon=True)
        self._thread.start()
        if not self._started.wait(timeout=self.config.realtime_connect_timeout_sec):
            self.cancel()
            raise ProviderError("等待千问实时会话配置确认超时")
        if self._start_error is not None:
            raise ProviderError(str(self._start_error)) from self._start_error

    def append_audio(self, pcm: bytes) -> bool:
        """无阻塞加入 16 kHz/16-bit/mono PCM；队列满或会话结束时返回 false。"""

        if not pcm or self._cancelled.is_set() or self._finished.is_set():
            return False
        self.metrics.input_audio_bytes += len(pcm)
        self.metrics.input_audio_chunks += 1
        try:
            self._commands.put_nowait(("append", pcm))
            return True
        except queue.Full:
            self._on_trace("realtime_input_queue_full", {"provider": "qwen", "queue_size": self._commands.qsize()})
            return False

    def commit(self) -> bool:
        """在全部 PCM 之后排入一次显式 PTT 提交；重复提交被幂等忽略。"""

        with self._commit_lock:
            if self._commit_enqueued:
                self._on_trace("realtime_capture_finish_duplicate", {"provider": "qwen"})
                return True
            accepted = self._enqueue_control("commit")
            if accepted:
                self._commit_enqueued = True
            return accepted

    def cancel(self) -> None:
        """请求取消当前响应并终止会话，阻止迟到音频继续下发。"""

        if self._cancelled.is_set() or self._finished.is_set():
            return
        self._cancelled.set()
        self._enqueue_control("cancel")

    def _enqueue_control(self, name: str) -> bool:
        """把提交或取消命令放入与音频共用的顺序队列。"""

        if self._finished.is_set():
            return False
        try:
            self._commands.put_nowait((name, None))
            return True
        except queue.Full:
            self._on_trace(
                "realtime_control_queue_full",
                {"provider": "qwen", "command": name, "queue_size": self._commands.qsize()},
            )
            return False

    def _next_event_id(self) -> str:
        """生成单会话内递增的客户端事件 ID。"""

        with self._event_id_lock:
            self._event_id += 1
            return f"qwen_event_{self._event_id}"

    def _thread_main(self) -> None:
        """在专用线程内运行 asyncio 收发循环，并统一处理启动与运行时异常。"""

        try:
            asyncio.run(self._run())
        except Exception as exc:  # noqa: BLE001
            wrapped = exc if isinstance(exc, ProviderError) else ProviderError(str(exc))
            if not self._started.is_set():
                self._start_error = wrapped
                self._started.set()
            elif not self._cancelled.is_set():
                self._on_error(wrapped)
        finally:
            if self._bridge is not None:
                self._bridge.close(report_nonzero=False, drain=False)
                self._bridge = None
            self._finished.set()
            if not self._started.is_set():
                self._started.set()

    async def _run(self) -> None:
        """连接 DashScope，先启动接收任务，再发送并等待 `session.update` 确认。"""

        try:
            import websockets
        except ImportError as exc:
            raise ProviderError("缺少 websockets，无法使用千问实时语音模型") from exc

        headers = {"Authorization": f"Bearer {self.config.qwen_realtime_api_key}"}
        connect_kwargs = {
            "ping_interval": None,
            "open_timeout": self.config.realtime_connect_timeout_sec,
            "max_size": 2**24,
        }
        try:
            ws = await websockets.connect(
                self.config.qwen_realtime_url,
                additional_headers=headers,
                **connect_kwargs,
            )
        except TypeError:
            ws = await websockets.connect(
                self.config.qwen_realtime_url,
                extra_headers=headers,
                **connect_kwargs,
            )

        async with ws:
            self._session_updated_event = asyncio.Event()
            receiver = asyncio.create_task(self._receive_events(ws))
            await asyncio.sleep(0)
            await self._send_event(ws, self._session_update_event())
            session_wait = asyncio.create_task(self._session_updated_event.wait())
            try:
                done, _ = await asyncio.wait(
                    {receiver, session_wait},
                    timeout=self.config.realtime_connect_timeout_sec,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if receiver in done:
                    receiver.result()
                    raise ProviderError("千问实时连接在 session.updated 前结束")
                if session_wait not in done:
                    raise ProviderError("千问实时 session.updated 超时")
            finally:
                if not session_wait.done():
                    session_wait.cancel()
                await asyncio.gather(session_wait, return_exceptions=True)

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
            self._on_trace(
                "realtime_session_ready",
                {
                    "provider": "qwen",
                    "model": self.config.qwen_realtime_model,
                    "turn_mode": "push_to_talk",
                    "voice": self.config.qwen_realtime_voice,
                },
            )
            sender = asyncio.create_task(self._send_commands(ws))
            try:
                done, _ = await asyncio.wait({sender, receiver}, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    task.result()
            finally:
                for task in (sender, receiver):
                    if not task.done():
                        task.cancel()
                await asyncio.gather(sender, receiver, return_exceptions=True)

    def _session_update_event(self) -> dict[str, object]:
        """构造千问会话配置，明确以 `turn_detection: null` 禁用服务端 VAD。"""

        session: dict[str, object] = {
            "modalities": ["text", "audio"],
            "voice": self.config.qwen_realtime_voice,
            "instructions": self._instructions_with_context(),
            "turn_detection": None,
            "enable_search": self.config.qwen_realtime_web_search_enable,
        }
        if self.config.qwen_realtime_transcription_model:
            session["input_audio_transcription"] = {
                "model": self.config.qwen_realtime_transcription_model,
            }
        return {
            "event_id": self._next_event_id(),
            "type": "session.update",
            "session": session,
        }

    def _instructions_with_context(self) -> str:
        """把最近问答拼入 instructions，绕开千问忽略普通文本 item 的限制。"""

        lines: list[str] = []
        for item in self._context_items:
            role = str(item.get("role") or "")
            value = str(item.get("text") or "").strip()
            if not value or role not in ("user", "assistant"):
                continue
            label = "用户" if role == "user" else "助手"
            lines.append(f"{label}：{value}")
        prompt = self.config.qwen_realtime_system_prompt.strip()
        if not lines:
            return prompt
        return f"{prompt}\n以下是该设备最近的对话，仅用于保持上下文：\n" + "\n".join(lines)

    async def _send_commands(self, ws: object) -> None:
        """按设备顺序发送 PCM；收到 commit 后补短静音并显式创建响应。"""

        while not self._finished.is_set():
            try:
                name, payload = await asyncio.to_thread(self._commands.get, True, 0.1)
            except queue.Empty:
                continue

            if name == "append":
                if self._cancelled.is_set() or payload is None:
                    continue
                await self._send_event(
                    ws,
                    {
                        "event_id": self._next_event_id(),
                        "type": "input_audio_buffer.append",
                        "audio": base64.b64encode(payload).decode("ascii"),
                    },
                )
                continue

            if name == "commit":
                if self._cancelled.is_set():
                    continue
                self._commit_at = time.monotonic()
                tail_packets = math.ceil(self.config.qwen_realtime_tail_silence_ms / 20)
                for _ in range(tail_packets):
                    await self._send_event(
                        ws,
                        {
                            "event_id": self._next_event_id(),
                            "type": "input_audio_buffer.append",
                            "audio": base64.b64encode(_PCM_20MS_SILENCE).decode("ascii"),
                        },
                    )
                self.metrics.endpoint_silence_chunks += tail_packets
                self.metrics.endpoint_silence_bytes += tail_packets * _PCM_20MS_BYTES
                await self._send_event(
                    ws,
                    {"event_id": self._next_event_id(), "type": "input_audio_buffer.commit"},
                )
                await self._send_event(
                    ws,
                    {"event_id": self._next_event_id(), "type": "response.create"},
                )
                self._on_trace(
                    "realtime_capture_finished",
                    {
                        "provider": "qwen",
                        "turn_mode": "push_to_talk",
                        "input_audio_bytes": self.metrics.input_audio_bytes,
                        "tail_silence_ms": tail_packets * 20,
                        "tail_silence_packets": tail_packets,
                    },
                )
                continue

            if name == "cancel":
                self._on_trace("realtime_session_cancel", {"provider": "qwen"})
                for event_type in ("response.cancel", "session.finish"):
                    try:
                        await self._send_event(
                            ws,
                            {"event_id": self._next_event_id(), "type": event_type},
                        )
                    except Exception:  # noqa: BLE001
                        break
                await ws.close()  # type: ignore[attr-defined]
                return

    async def _receive_events(self, ws: object) -> None:
        """解析千问会话、ASR、回复文本和 24 kHz PCM 下行事件。"""

        while not self._cancelled.is_set():
            timeout: float | None = None
            timeout_reason = ""
            now = time.monotonic()
            if self._commit_at is not None and self.metrics.first_provider_audio_ms is None:
                timeout = self.config.realtime_first_response_timeout_sec - (now - self._commit_at)
                timeout_reason = "first_provider_audio"
            elif self._last_provider_audio_at is not None:
                timeout = self.config.realtime_output_idle_timeout_sec - (now - self._last_provider_audio_at)
                timeout_reason = "output_audio_idle"
            if timeout is not None and timeout <= 0:
                self._raise_receive_timeout(timeout_reason)

            try:
                if timeout is None:
                    raw = await ws.recv()  # type: ignore[attr-defined]
                else:
                    raw = await asyncio.wait_for(ws.recv(), timeout=timeout)  # type: ignore[attr-defined]
            except asyncio.TimeoutError:
                self._raise_receive_timeout(timeout_reason)
                raise AssertionError("unreachable")

            event = self._parse_event(raw)
            event_type = str(event.get("type") or "")
            if event_type == "session.created":
                self._on_trace(
                    "realtime_session_created",
                    {"provider": "qwen", "session_id": str((event.get("session") or {}).get("id") or "")},
                )
            elif event_type == "session.updated":
                if self._session_updated_event is not None:
                    self._session_updated_event.set()
                session = event.get("session") if isinstance(event.get("session"), dict) else {}
                self._on_trace(
                    "realtime_session_updated",
                    {
                        "provider": "qwen",
                        "turn_detection": session.get("turn_detection"),
                        "voice": session.get("voice"),
                        "enable_search": session.get("enable_search"),
                    },
                )
            elif event_type in ("response.audio.delta", "response.output_audio.delta"):
                self._handle_provider_audio(event)
            elif event_type in ("response.audio_transcript.delta", "response.text.delta", "response.output_text.delta"):
                delta = str(event.get("delta") or "")
                if delta:
                    self._answer_parts.append(delta)
                    self.metrics.answer_text = "".join(self._answer_parts).strip()
                self._on_trace("realtime_text_delta", {"provider": "qwen", "chars": len(delta), "total_chars": len(self.metrics.answer_text)})
            elif event_type in (
                "response.audio_transcript.done",
                "response.text.done",
                "response.output_text.done",
            ):
                completed = str(event.get("transcript") or event.get("text") or "").strip()
                if completed and not self._answer_parts:
                    self._answer_parts.append(completed)
                self._finish_answer_text(event_type)
            elif event_type.startswith("conversation.item.input_audio_transcription."):
                self._handle_transcription_event(event_type, event)
            elif event_type in ("response.audio.done", "response.output_audio.done"):
                self._on_trace(
                    "realtime_provider_event",
                    {"provider": "qwen", "type": event_type, "output_pcm_bytes": self.metrics.output_pcm_bytes},
                )
            elif event_type == "response.done":
                fallback = self._response_transcript(event)
                if fallback and not self._answer_parts:
                    self._answer_parts.append(fallback)
                self._finish_normal(self._response_finish_reason(event))
                return
            elif event_type == "error":
                raise ProviderError(self._event_error_text(event))
            else:
                self._on_trace("realtime_provider_event", {"provider": "qwen", "type": event_type})

    def _raise_receive_timeout(self, reason: str) -> None:
        """把首包或输出空闲超时转换为带提供方信息的业务异常。"""

        if reason == "output_audio_idle":
            raise ProviderError(
                f"千问实时首段音频后连续 {self.config.realtime_output_idle_timeout_sec:g} 秒未收到后续音频或完成事件"
            )
        raise ProviderError(
            f"千问实时提交后 {self.config.realtime_first_response_timeout_sec:g} 秒仍未收到首段音频"
        )

    def _handle_transcription_event(self, event_type: str, event: dict[str, object]) -> None:
        """累计千问输入转写，并在 completed 时保存完整用户文本。"""

        if event_type.endswith(".delta"):
            delta = str(event.get("delta") or "")
            if delta:
                self._asr_parts.append(delta)
            return
        if event_type.endswith(".completed"):
            completed = str(event.get("transcript") or event.get("text") or "").strip()
            self.metrics.asr_text = completed or "".join(self._asr_parts).strip()
            self._on_trace("realtime_asr_completed", {"provider": "qwen", "text": self.metrics.asr_text})
            return
        if event_type.endswith(".failed"):
            raise ProviderError(f"千问实时输入转写失败: {self._event_error_text(event)}")

    def _handle_provider_audio(self, event: dict[str, object]) -> None:
        """解码供应商 PCM，更新首包/间隔指标并写入现有 CI MP3 桥。"""

        try:
            pcm = base64.b64decode(str(event.get("delta") or ""), validate=True)
        except (ValueError, TypeError) as exc:
            raise ProviderError(f"千问实时音频 Base64 解码失败: {exc}") from exc
        if not pcm:
            return
        self.metrics.output_pcm_bytes += len(pcm)
        self.metrics.output_pcm_chunks += 1
        provider_audio_at = time.monotonic()
        if self._last_provider_audio_at is not None:
            gap_ms = int((provider_audio_at - self._last_provider_audio_at) * 1000)
            self.metrics.provider_audio_max_gap_ms = max(self.metrics.provider_audio_max_gap_ms, gap_ms)
        self._last_provider_audio_at = provider_audio_at
        if self.metrics.first_provider_audio_ms is None:
            self.metrics.first_provider_audio_ms = self.metrics.elapsed_ms()
            after_commit_ms = (
                int((provider_audio_at - self._commit_at) * 1000)
                if self._commit_at is not None
                else None
            )
            self._on_trace(
                "realtime_first_provider_audio",
                {
                    "provider": "qwen",
                    "first_provider_audio_ms": self.metrics.first_provider_audio_ms,
                    "after_commit_ms": after_commit_ms,
                },
            )
        if self._bridge is None or not self._bridge.write(pcm):
            raise ProviderError("千问实时 PCM 到 MP3 桥不可用或已满")

    def _handle_mp3(self, chunk: bytes) -> None:
        """记录 MP3 首包及相邻间隔后转交给服务器 WebSocket 下行。"""

        if self._cancelled.is_set() or not chunk:
            return
        self.metrics.output_mp3_bytes += len(chunk)
        self.metrics.output_mp3_chunks += 1
        mp3_at = time.monotonic()
        if self._last_mp3_at is not None:
            gap_ms = int((mp3_at - self._last_mp3_at) * 1000)
            self.metrics.mp3_output_max_gap_ms = max(self.metrics.mp3_output_max_gap_ms, gap_ms)
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
                    "provider": "qwen",
                    "first_mp3_ms": self.metrics.first_mp3_ms,
                    "pcm_to_first_mp3_ms": self.metrics.pcm_to_first_mp3_ms,
                    "chunk_bytes": len(chunk),
                },
            )
        self._on_audio(chunk)

    def _handle_bridge_error(self, exc: Exception) -> None:
        """把异步转码错误升级为会话错误并立即停止供应商输出。"""

        if not self._cancelled.is_set():
            self.cancel()
            self._on_error(exc)

    def _finish_answer_text(self, reason: str) -> None:
        """汇总并只记录一次千问最终回复文本。"""

        self.metrics.answer_text = "".join(self._answer_parts).strip()
        if self._answer_completed_logged:
            return
        self._answer_completed_logged = True
        self._on_trace(
            "realtime_answer_completed",
            {
                "provider": "qwen",
                "text": self.metrics.answer_text,
                "chars": len(self.metrics.answer_text),
                "reason": reason,
            },
        )

    def _finish_normal(self, reason: str) -> None:
        """排空转码桥，记录统一指标并通知服务器发送 `dialogue.done`。"""

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
                        "provider": "qwen",
                        "returncode": returncode,
                        "detail": close_detail[:300],
                        "output_pcm_bytes": self.metrics.output_pcm_bytes,
                        "output_mp3_bytes": self.metrics.output_mp3_bytes,
                    },
                )
                if self.metrics.output_mp3_bytes <= 0:
                    raise ProviderError(f"千问实时 MP3 转码失败: {close_detail}")
        self._finish_answer_text(reason)
        self._on_trace(
            "realtime_response_done",
            {
                "provider": "qwen",
                "finish_reason": reason,
                "input_audio_bytes": self.metrics.input_audio_bytes,
                "endpoint_silence_bytes": self.metrics.endpoint_silence_bytes,
                "output_pcm_bytes": self.metrics.output_pcm_bytes,
                "output_mp3_bytes": self.metrics.output_mp3_bytes,
                "answer_chars": len(self.metrics.answer_text),
                "total_ms": self.metrics.elapsed_ms(),
            },
        )
        self._on_done(self.metrics)

    @staticmethod
    async def _send_event(ws: object, event: dict[str, object]) -> None:
        """以紧凑 JSON 发送一条千问客户端事件。"""

        await ws.send(json.dumps(event, ensure_ascii=False, separators=(",", ":")))  # type: ignore[attr-defined]

    @staticmethod
    def _parse_event(raw: object) -> dict[str, object]:
        """把 DashScope 文本帧解析成 JSON 对象。"""

        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        try:
            event = json.loads(str(raw))
        except json.JSONDecodeError as exc:
            raise ProviderError(f"千问实时服务返回非法 JSON: {exc}") from exc
        if not isinstance(event, dict):
            raise ProviderError("千问实时服务返回的事件不是对象")
        return event

    @staticmethod
    def _event_error_text(event: dict[str, object]) -> str:
        """提取千问 error 或 failed 事件的可读信息。"""

        error = event.get("error")
        if isinstance(error, dict):
            return str(error.get("message") or error.get("code") or error)
        return str(error or event.get("message") or event)

    @staticmethod
    def _response_transcript(event: dict[str, object]) -> str:
        """从 response.done 的 output/content 结构兜底提取最终文本。"""

        response = event.get("response")
        if not isinstance(response, dict):
            return ""
        parts: list[str] = []
        output = response.get("output")
        if not isinstance(output, list):
            return ""
        for item in output:
            if not isinstance(item, dict):
                continue
            content = item.get("content")
            if not isinstance(content, list):
                continue
            for block in content:
                if not isinstance(block, dict):
                    continue
                text = str(block.get("transcript") or block.get("text") or "").strip()
                if text:
                    parts.append(text)
        return "".join(parts)

    @staticmethod
    def _response_finish_reason(event: dict[str, object]) -> str:
        """生成适合统一日志的千问响应完成原因。"""

        response = event.get("response")
        if not isinstance(response, dict):
            return "response.done"
        status = str(response.get("status") or "completed")
        details = response.get("status_details")
        reason = ""
        if isinstance(details, dict):
            reason = str(details.get("reason") or details.get("type") or "")
        return f"response.done:{status}" + (f":{reason}" if reason else "")
