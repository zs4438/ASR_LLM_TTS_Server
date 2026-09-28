"""本地语音服务器 HTTP + WebSocket 入口。"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import socket
import struct
import sys
import threading
import time
import traceback
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable

from .config import ServerConfig
from .doubao_realtime_memory import DoubaoRealtimeMemory
from .errors import CancelledError, ConfigError, ProviderError, VoiceServerError
from .memory import ConversationMemory
from .music import LocalMusicLibrary
from .pipeline import VoicePipeline
from .providers import build_asr_client, build_llm_client, build_tts_client
from .realtime import DoubaoRealtimeSession, RealtimeMetrics
from .realtime_qwen import QwenRealtimeSession
from .tool_router import LLMToolRouter
from .weather import QWeatherClient


WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
DEVICE_SEEN: set[str] = set()


class WebSocketClientDisconnected(ConnectionError):
    """客户端正常断开或网络复位。"""


@dataclass
class WsDialogueState:
    """功能说明：保存 WebSocket 当前一轮对话的状态。"""

    req: str
    client_req: object = None
    device_sn: str = ""
    device_ip: str = ""
    audio_format: str = "pcm"
    route: str = "cascade"
    chunks: list[bytes] = field(default_factory=list)
    realtime_session: DoubaoRealtimeSession | QwenRealtimeSession | None = None
    realtime_audio_bytes: int = 0
    realtime_audio_chunks: int = 0
    realtime_error_reported: bool = False
    cancel_event: threading.Event = field(default_factory=threading.Event)
    done_event: threading.Event = field(default_factory=threading.Event)
    worker: threading.Thread | None = None


class TimestampTee:
    """功能说明：把终端输出同步写入日志文件，并为每行添加毫秒时间戳。"""

    def __init__(self, *streams):
        self.streams = streams
        self._line_start = True
        self._lock = threading.Lock()

    def write(self, text: str) -> int:
        """功能说明：写入文本并在行首添加时间戳。"""

        with self._lock:
            for part in text.splitlines(keepends=True):
                output = part
                if self._line_start and part:
                    output = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]} {part}"
                self._line_start = part.endswith("\n")
                for stream in self.streams:
                    stream.write(output)
                    stream.flush()
        return len(text)

    def flush(self) -> None:
        """功能说明：刷新所有输出目标。"""

        for stream in self.streams:
            stream.flush()


class VoiceRequestHandler(BaseHTTPRequestHandler):
    """功能说明：处理 HTTP 调试接口和 ESP32 WebSocket 主链路。"""

    server_version = "ASR_LLM_TTS_Server/0.1"
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:
        """功能说明：处理健康检查、配置查看和 WebSocket 握手。"""

        if self.path.startswith("/v1/dialogue/ws"):
            self._handle_dialogue_websocket()
            return
        if self.path == "/health":
            self._send_json({"ok": True, "service": "ASR_LLM_TTS_Server"})
            return
        if self.path == "/v1/config":
            self._send_json({"ok": True, "config": self.server.config.masked_dict()})  # type: ignore[attr-defined]
            return
        if self.path == "/v1/music/list":
            music = self.server.music  # type: ignore[attr-defined]
            self._send_json({"ok": True, "tracks": music.list_tracks() if music else []})
            return
        self.send_error(HTTPStatus.NOT_FOUND, "not found")

    def do_POST(self) -> None:
        """功能说明：处理本地调试 HTTP 接口。"""

        try:
            if self.path == "/v1/asr":
                audio = self._read_body()
                audio_format = self.headers.get("X-Voice-Audio-Format", "pcm")
                text = self.server.pipeline.asr.recognize_pcm(self.server.pipeline._decode_audio(audio, audio_format))  # type: ignore[attr-defined]
                self._send_json({"ok": True, "text": text})
            elif self.path == "/v1/llm":
                payload = self._read_json()
                answer = self.server.pipeline.llm.ask(str(payload.get("question") or ""))  # type: ignore[attr-defined]
                self._send_json({"ok": True, "answer": answer})
            elif self.path == "/v1/tts":
                payload = self._read_json()
                audio = self.server.pipeline.tts.synthesize_mp3(str(payload.get("text") or ""))  # type: ignore[attr-defined]
                self._send_bytes(audio, "audio/mpeg")
            elif self.path == "/v1/tts/stream":
                payload = self._read_json()
                self._send_chunked_audio(lambda send: _stream_iter(self.server.pipeline.tts.stream_synthesize_mp3(str(payload.get("text") or "")), send))  # type: ignore[attr-defined]
            elif self.path == "/v1/dialogue":
                self._handle_dialogue_json()
            elif self.path == "/v1/dialogue/audio":
                self._handle_dialogue_audio(stream=False)
            elif self.path == "/v1/dialogue/audio-stream":
                self._handle_dialogue_audio(stream=True)
            elif self.path == "/v1/text-dialogue":
                self._handle_text_dialogue_json()
            elif self.path == "/v1/text-dialogue/audio-stream":
                self._handle_text_dialogue_audio_stream()
            elif self.path == "/v1/memory/recent":
                self._handle_memory_recent()
            elif self.path == "/v1/memory/reset":
                self._handle_memory_reset()
            else:
                self.send_error(HTTPStatus.NOT_FOUND, "not found")
        except (ConfigError, ProviderError, VoiceServerError, ValueError) as exc:
            self._send_json({"ok": False, "error": str(exc)}, status=500)
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            self._send_json({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, status=500)

    def log_message(self, format: str, *args) -> None:  # noqa: A003
        """功能说明：统一 HTTP 基础日志格式。"""

        print(f"voice_server http peer={self.client_address} {format % args}")

    def _handle_dialogue_json(self) -> None:
        """功能说明：HTTP 完整音频对话，返回 JSON + base64 音频。"""

        req = _new_req()
        audio = self._read_body()
        audio_format = self.headers.get("X-Voice-Audio-Format", "pcm")
        result = self.server.pipeline.dialogue(audio, audio_format=audio_format, request_id=req)  # type: ignore[attr-defined]
        self._send_json(_result_json(result))

    def _handle_dialogue_audio(self, *, stream: bool) -> None:
        """功能说明：HTTP 音频对话，返回 MP3 音频。"""

        req = _new_req()
        audio = self._read_body()
        audio_format = self.headers.get("X-Voice-Audio-Format", "pcm")
        if "speex" in self.headers.get("Content-Type", "").lower():
            audio_format = "speex"
        if stream:
            self._send_chunked_audio(
                lambda send: self.server.pipeline.stream_audio_dialogue(  # type: ignore[attr-defined]
                    audio,
                    audio_format=audio_format,
                    send_audio=send,
                    request_id=req,
                    trace=lambda stage, fields: _trace(req, stage, fields),
                )
            )
        else:
            result = self.server.pipeline.dialogue(audio, audio_format=audio_format, request_id=req)  # type: ignore[attr-defined]
            self._send_bytes(result.audio, "audio/mpeg")

    def _handle_text_dialogue_json(self) -> None:
        """功能说明：HTTP 文本对话，返回 JSON + base64 音频。"""

        payload = self._read_json()
        req = _new_req()
        result = self.server.pipeline.text_dialogue(  # type: ignore[attr-defined]
            str(payload.get("question") or ""),
            device_sn=str(payload.get("device_sn") or ""),
            request_id=req,
        )
        self._send_json(_result_json(result))

    def _handle_text_dialogue_audio_stream(self) -> None:
        """功能说明：HTTP 文本对话，流式返回 MP3。"""

        payload = self._read_json()
        req = _new_req()
        question = str(payload.get("question") or "")
        device_sn = str(payload.get("device_sn") or "")
        self._send_chunked_audio(
            lambda send: self.server.pipeline.stream_text_dialogue(  # type: ignore[attr-defined]
                question,
                send,
                device_sn=device_sn,
                request_id=req,
                trace=lambda stage, fields: _trace(req, stage, fields),
            )
        )

    def _handle_memory_recent(self) -> None:
        """功能说明：返回指定设备最近记忆。"""

        payload = self._read_json()
        memory = self.server.memory  # type: ignore[attr-defined]
        if not memory:
            self._send_json({"ok": True, "turns": []})
            return
        turns = memory.recent_turns(str(payload.get("device_sn") or ""), int(payload.get("top_k") or 10))
        self._send_json({"ok": True, "turns": turns})

    def _handle_memory_reset(self) -> None:
        """功能说明：清空指定设备或全部设备记忆。"""

        payload = self._read_json()
        memory = self.server.memory  # type: ignore[attr-defined]
        if not memory:
            self._send_json({"ok": True, "deleted": 0})
            return
        deleted = memory.reset(None if payload.get("all") else str(payload.get("device_sn") or ""))
        self._send_json({"ok": True, "deleted": deleted})

    def _handle_dialogue_websocket(self) -> None:
        """功能说明：处理 ESP32 使用的 WebSocket 流式语音链路。"""

        if not self._websocket_handshake():
            return
        peer = self.client_address
        send_lock = threading.Lock()
        current: WsDialogueState | None = None
        print(f"voice_server ws stage=connected peer={peer}")
        try:
            while True:
                if current is not None and current.done_event.is_set():
                    print(
                        f"voice_server ws stage=dialogue_complete req={current.req} "
                        f"client_req={current.client_req} route={current.route}"
                    )
                    current = None
                opcode, payload = self._ws_recv_frame()
                if opcode is None:
                    break
                if opcode == 0x8:
                    break
                if opcode == 0x9:
                    self._ws_send_frame(0xA, payload, send_lock)
                    continue
                if current is not None and current.done_event.is_set():
                    print(
                        f"voice_server ws stage=dialogue_complete req={current.req} "
                        f"client_req={current.client_req} route={current.route}"
                    )
                    current = None
                if opcode == 0x2:
                    if current is not None:
                        if current.route == "realtime":
                            if current.realtime_session is None or not current.realtime_session.append_audio(payload):
                                self._ws_send_json(
                                    {
                                        "type": "dialogue.error",
                                        "req": current.req,
                                        "client_req": current.client_req,
                                        "error": "realtime input queue unavailable or full",
                                    },
                                    send_lock,
                                )
                                if current.realtime_session is not None:
                                    current.realtime_session.cancel()
                                current.cancel_event.set()
                                current = None
                            else:
                                current.realtime_audio_bytes += len(payload)
                                current.realtime_audio_chunks += 1
                        else:
                            current.chunks.append(payload)
                    continue
                if opcode != 0x1:
                    continue
                message = json.loads(payload.decode("utf-8"))
                msg_type = message.get("type")
                if msg_type == "device.online":
                    self._handle_ws_device_online(message, peer, send_lock)
                elif msg_type == "dialogue.start":
                    current = self._handle_ws_dialogue_start(message, peer, send_lock)
                elif msg_type in ("dialogue.end", "dialogue.commit"):
                    if current is not None:
                        if current.route == "realtime":
                            if current.realtime_session is None or not current.realtime_session.commit():
                                self._ws_send_json(
                                    {
                                        "type": "dialogue.error",
                                        "req": current.req,
                                        "client_req": current.client_req,
                                        "error": "realtime capture-finish handling failed",
                                    },
                                    send_lock,
                                )
                                if current.realtime_session is not None:
                                    current.realtime_session.cancel()
                                current.cancel_event.set()
                                current = None
                            else:
                                print(
                                    f"voice_server realtime stage=capture_finished req={current.req} "
                                    f"client_req={current.client_req} chunks={current.realtime_audio_chunks} "
                                    f"bytes={current.realtime_audio_bytes}"
                                )
                        else:
                            self._start_ws_worker(current, send_lock)
                elif msg_type == "dialogue.cancel":
                    if current is not None:
                        current.cancel_event.set()
                        if current.realtime_session is not None:
                            current.realtime_session.cancel()
                        reason = str(message.get("reason") or "client_cancel")
                        print(
                            f"voice_server ws stage=client_cancel req={current.req} peer={peer} "
                            f"client_req={current.client_req} device_sn={message.get('device_sn') or current.device_sn} "
                            f"device_ip={message.get('device_ip') or current.device_ip} reason={reason}"
                        )
                        self._ws_send_json(
                            {"type": "dialogue.cancelled", "req": current.req, "client_req": current.client_req, "reason": reason},
                            send_lock,
                        )
                        print(f"voice_server ws stage=cancel_ack req={current.req} client_req={current.client_req}")
                        current = None
                if current is not None and current.done_event.is_set():
                    print(
                        f"voice_server ws stage=dialogue_complete req={current.req} "
                        f"client_req={current.client_req} route={current.route}"
                    )
                    current = None
        except WebSocketClientDisconnected:
            print(f"voice_server ws stage=client_closed peer={peer}")
            if current is not None:
                current.cancel_event.set()
                if current.realtime_session is not None:
                    current.realtime_session.cancel()
        except Exception as exc:  # noqa: BLE001
            print(f"voice_server ws stage=error peer={peer} error={type(exc).__name__}: {exc}")
            traceback.print_exc()
            if current is not None:
                current.cancel_event.set()
                if current.realtime_session is not None:
                    current.realtime_session.cancel()
                self._ws_send_json({"type": "dialogue.error", "req": current.req, "client_req": current.client_req, "error": str(exc)}, send_lock)
        finally:
            if current is not None:
                current.cancel_event.set()
                if current.realtime_session is not None:
                    current.realtime_session.cancel()
            self.close_connection = True
            try:
                self.connection.close()
            except OSError:
                pass
            print(f"voice_server ws stage=closed peer={peer}")

    def _handle_ws_device_online(self, message: dict, peer: object, send_lock: threading.Lock) -> None:
        """功能说明：记录 ESP32 长连接上线信息并回应 ack。"""

        req = _new_req()
        device_sn = str(message.get("device_sn") or "")
        device_ip = str(message.get("device_ip") or "")
        status = "known" if device_sn in DEVICE_SEEN else "new"
        if device_sn:
            DEVICE_SEEN.add(device_sn)
        print(
            f"voice_server ws stage=device_online req={req} peer={peer} device_sn={device_sn} "
            f"device_ip={device_ip} client_online_seq={message.get('client_online_seq')} "
            f"client_uptime_ms={message.get('client_uptime_ms')} device_status={status} unique_devices={len(DEVICE_SEEN)}"
        )
        self._ws_send_json({"type": "device.online.ack", "req": req}, send_lock)

    def _handle_ws_dialogue_start(self, message: dict, peer: object, send_lock: threading.Lock) -> WsDialogueState | None:
        """功能说明：初始化 WebSocket 新一轮对话状态，并立即发送 ready。"""

        state = WsDialogueState(
            req=_new_req(),
            client_req=message.get("client_req"),
            device_sn=str(message.get("device_sn") or ""),
            device_ip=str(message.get("device_ip") or ""),
            audio_format=str(message.get("audio_format") or "pcm"),
            route=str(message.get("route") or self.server.config.voice_route).lower(),  # type: ignore[attr-defined]
        )
        if state.route not in ("cascade", "realtime"):
            self._ws_send_json(
                {"type": "dialogue.error", "req": state.req, "client_req": state.client_req, "error": f"unsupported voice route: {state.route}"},
                send_lock,
            )
            return None
        if state.route == "realtime" and state.audio_format != "pcm":
            self._ws_send_json(
                {"type": "dialogue.error", "req": state.req, "client_req": state.client_req, "error": "realtime route requires pcm input"},
                send_lock,
            )
            return None
        print(
            f"voice_server ws stage=dialogue_start req={state.req} peer={peer} client_req={state.client_req} "
            f"device_sn={state.device_sn} device_ip={state.device_ip} audio_format={state.audio_format} route={state.route}"
        )
        if state.route == "realtime":
            try:
                state.realtime_session = self._create_realtime_session(state, send_lock)
                state.realtime_session.start()
            except Exception as exc:  # noqa: BLE001
                self._ws_send_json(
                    {"type": "dialogue.error", "req": state.req, "client_req": state.client_req, "error": str(exc)},
                    send_lock,
                )
                print(
                    f"voice_server realtime stage=session_start_error req={state.req} "
                    f"provider={self.server.config.realtime_provider} error={type(exc).__name__}: {exc}"  # type: ignore[attr-defined]
                )
                return None
        self._ws_send_json(
            {"type": "dialogue.ready", "req": state.req, "client_req": state.client_req, "route": state.route},
            send_lock,
        )
        return state

    def _create_realtime_session(
        self,
        state: WsDialogueState,
        send_lock: threading.Lock,
    ) -> DoubaoRealtimeSession | QwenRealtimeSession:
        """功能说明：为当前设备对话创建实时模型适配器及安全下行回调。"""

        def send_audio(chunk: bytes) -> None:
            if state.cancel_event.is_set():
                raise CancelledError("cancelled before realtime ws binary send")
            self._ws_send_frame(0x2, chunk, send_lock, cancel_event=state.cancel_event)

        def trace(stage: str, fields: dict[str, object]) -> None:
            _trace(state.req, stage, fields)

        realtime_memory = self.server.realtime_memory  # type: ignore[attr-defined]
        provider = self.server.config.realtime_provider  # type: ignore[attr-defined]
        memory_recent_turns = (
            self.server.config.qwen_realtime_memory_max_recent_turns  # type: ignore[attr-defined]
            if provider == "qwen"
            else self.server.config.doubao_realtime_memory_max_recent_turns  # type: ignore[attr-defined]
        )
        context_items: list[dict[str, str]] = []
        if realtime_memory and state.device_sn:
            context_items = realtime_memory.context_items(  # type: ignore[union-attr]
                state.device_sn,
                memory_recent_turns,
            )
            trace(
                "realtime_memory_loaded",
                {"provider": provider, "device_sn": state.device_sn, "turns": len(context_items) // 2},
            )

        def done(metrics: RealtimeMetrics) -> None:
            if state.cancel_event.is_set() or state.done_event.is_set():
                return
            state.done_event.set()
            payload = {
                "type": "dialogue.done",
                "req": state.req,
                "client_req": state.client_req,
                "route": "realtime",
                "provider": provider,
                "finish_reason": metrics.finish_reason,
                "endpoint_silence_bytes": metrics.endpoint_silence_bytes,
                "endpoint_silence_chunks": metrics.endpoint_silence_chunks,
                "output_pcm_bytes": metrics.output_pcm_bytes,
                "output_pcm_chunks": metrics.output_pcm_chunks,
                "audio_bytes": metrics.output_mp3_bytes,
                "audio_chunks": metrics.output_mp3_chunks,
                "post_endpoint_input_bytes": metrics.post_endpoint_input_bytes,
                "post_endpoint_input_chunks": metrics.post_endpoint_input_chunks,
                "provider_audio_max_gap_ms": metrics.provider_audio_max_gap_ms,
                "mp3_output_max_gap_ms": metrics.mp3_output_max_gap_ms,
                "first_provider_audio_ms": metrics.first_provider_audio_ms,
                "first_chunk_ms": metrics.first_mp3_ms,
                "pcm_to_first_mp3_ms": metrics.pcm_to_first_mp3_ms,
                "total_ms": metrics.elapsed_ms(),
            }
            self._ws_send_json(payload, send_lock)
            print(
                f"voice_server realtime stage=done req={state.req} client_req={state.client_req} provider={provider} "
                f"finish_reason={metrics.finish_reason} "
                f"input_bytes={metrics.input_audio_bytes} endpoint_silence_bytes={metrics.endpoint_silence_bytes} "
                f"output_pcm_bytes={metrics.output_pcm_bytes} "
                f"output_mp3_bytes={metrics.output_mp3_bytes} "
                f"post_endpoint_input_bytes={metrics.post_endpoint_input_bytes} "
                f"provider_audio_max_gap_ms={metrics.provider_audio_max_gap_ms} "
                f"mp3_output_max_gap_ms={metrics.mp3_output_max_gap_ms} "
                f"first_mp3_ms={metrics.first_mp3_ms} total_ms={metrics.elapsed_ms()}"
            )
            if realtime_memory and state.device_sn and metrics.asr_text and metrics.answer_text:
                try:
                    realtime_memory.store_turn(state.device_sn, metrics.asr_text, metrics.answer_text, state.req)  # type: ignore[union-attr]
                    trace(
                        "realtime_memory_stored",
                        {"provider": provider, "device_sn": state.device_sn, "request_id": state.req},
                    )
                except Exception as exc:  # noqa: BLE001
                    trace("realtime_memory_store_failed", {"error": str(exc)[:200]})

        def error(exc: Exception) -> None:
            if state.cancel_event.is_set() or state.realtime_error_reported:
                return
            state.realtime_error_reported = True
            state.done_event.set()
            self._ws_send_json(
                {"type": "dialogue.error", "req": state.req, "client_req": state.client_req, "error": str(exc)},
                send_lock,
            )
            print(
                f"voice_server realtime stage=error req={state.req} provider={provider} "
                f"error={type(exc).__name__}: {exc}"
            )

        session_type = QwenRealtimeSession if provider == "qwen" else DoubaoRealtimeSession
        return session_type(
            self.server.config,  # type: ignore[attr-defined]
            on_audio=send_audio,
            on_trace=trace,
            on_done=done,
            on_error=error,
            context_items=context_items,
        )

    def _start_ws_worker(self, state: WsDialogueState, send_lock: threading.Lock) -> None:
        """功能说明：启动后台线程处理 ASR/动作路由/LLM/TTS，主线程继续收 cancel。"""

        if state.worker is not None:
            return

        def worker() -> None:
            start = time.perf_counter()
            audio_bytes = 0
            audio_chunks = 0
            first_chunk_ms: int | None = None

            def send_audio(chunk: bytes) -> None:
                nonlocal audio_bytes, audio_chunks, first_chunk_ms
                if state.cancel_event.is_set():
                    raise CancelledError("cancelled before ws binary send")
                if first_chunk_ms is None:
                    first_chunk_ms = _elapsed_ms(start)
                    print(
                        f"voice_server ws stage=first_binary_sent server_req={state.req} client_req={state.client_req} "
                        f"device_sn={state.device_sn} device_ip={state.device_ip} first_chunk_ms={first_chunk_ms} chunk_bytes={len(chunk)}"
                    )
                self._ws_send_frame(0x2, chunk, send_lock, cancel_event=state.cancel_event)
                audio_bytes += len(chunk)
                audio_chunks += 1

            def notify_action(fields: dict[str, object]) -> None:
                if state.cancel_event.is_set():
                    raise CancelledError("cancelled before ws action send")
                payload = {
                    "type": "dialogue.action",
                    "req": state.req,
                    "client_req": state.client_req,
                }
                payload.update(fields)
                self._ws_send_json(payload, send_lock)
                print(
                    f"voice_server ws stage=action_sent server_req={state.req} client_req={state.client_req} "
                    f"action={payload.get('action')} title={payload.get('title', '')} timeout_hint_ms={payload.get('timeout_hint_ms', '')}"
                )

            try:
                audio = b"".join(state.chunks)
                result = self.server.pipeline.stream_audio_dialogue(  # type: ignore[attr-defined]
                    audio,
                    audio_format=state.audio_format,
                    send_audio=send_audio,
                    device_sn=state.device_sn,
                    request_id=state.req,
                    trace=lambda stage, fields: _trace(state.req, stage, fields),
                    cancel_event=state.cancel_event,
                    notify_action=notify_action,
                )
                if not state.cancel_event.is_set():
                    self._ws_send_json(
                        {
                            "type": "dialogue.done",
                            "req": state.req,
                            "client_req": state.client_req,
                            "audio_bytes": audio_bytes,
                            "audio_chunks": audio_chunks,
                            "first_chunk_ms": first_chunk_ms,
                            "total_ms": _elapsed_ms(start),
                            "action": result.action,
                        },
                        send_lock,
                    )
            except CancelledError as exc:
                print(f"voice_server trace stage=upstream_cancelled req={state.req} fields={{'cancel_point': '{exc}'}}")
            except WebSocketClientDisconnected as exc:
                state.cancel_event.set()
                print(f"voice_server ws stage=client_closed_during_stream req={state.req} error={exc}")
            except Exception as exc:  # noqa: BLE001
                print(f"voice_server ws stage=worker_error req={state.req} error={type(exc).__name__}: {exc}")
                traceback.print_exc()
                self._ws_send_json({"type": "dialogue.error", "req": state.req, "client_req": state.client_req, "error": str(exc)}, send_lock)
            finally:
                state.done_event.set()

        state.worker = threading.Thread(target=worker, name=f"dialogue-{state.req}", daemon=True)
        state.worker.start()

    def _websocket_handshake(self) -> bool:
        """功能说明：执行 WebSocket 握手。"""

        key = self.headers.get("Sec-WebSocket-Key", "")
        if not key:
            self.send_error(HTTPStatus.BAD_REQUEST, "missing websocket key")
            return False
        accept = base64.b64encode(hashlib.sha1((key + WS_GUID).encode("ascii")).digest()).decode("ascii")
        self.send_response(HTTPStatus.SWITCHING_PROTOCOLS)
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", accept)
        self.end_headers()
        return True

    def _ws_recv_frame(self) -> tuple[int | None, bytes]:
        """功能说明：读取一个 WebSocket 帧，支持客户端 mask。"""

        header = self._recv_exact(2)
        if not header:
            return None, b""
        b1, b2 = header
        opcode = b1 & 0x0F
        masked = bool(b2 & 0x80)
        length = b2 & 0x7F
        if length == 126:
            length = struct.unpack("!H", self._recv_exact(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", self._recv_exact(8))[0]
        mask = self._recv_exact(4) if masked else b""
        payload = self._recv_exact(length) if length else b""
        if masked:
            payload = bytes(payload[i] ^ mask[i % 4] for i in range(len(payload)))
        return opcode, payload

    def _ws_send_json(self, payload: dict, send_lock: threading.Lock) -> None:
        """功能说明：向 ESP32 发送一个 JSON 文本帧。"""

        self._ws_send_frame(0x1, json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"), send_lock)

    def _ws_send_frame(
        self,
        opcode: int,
        payload: bytes,
        send_lock: threading.Lock,
        cancel_event: threading.Event | None = None,
    ) -> None:
        """功能说明：发送服务端 WebSocket 帧，二进制发送可被取消。"""

        while True:
            if cancel_event is not None and cancel_event.is_set():
                raise CancelledError("cancelled while waiting websocket send lock")
            if send_lock.acquire(timeout=0.05):
                break
        try:
            if cancel_event is not None and cancel_event.is_set():
                raise CancelledError("cancelled before websocket send")
            length = len(payload)
            first = 0x80 | (opcode & 0x0F)
            if length < 126:
                header = bytes([first, length])
            elif length <= 0xFFFF:
                header = bytes([first, 126]) + struct.pack("!H", length)
            else:
                header = bytes([first, 127]) + struct.pack("!Q", length)
            self.connection.sendall(header + payload)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError) as exc:
            raise WebSocketClientDisconnected(str(exc)) from exc
        finally:
            send_lock.release()

    def _recv_exact(self, size: int) -> bytes:
        """功能说明：从 socket 精确读取指定字节数。"""

        data = bytearray()
        while len(data) < size:
            try:
                chunk = self.connection.recv(size - len(data))
            except (ConnectionResetError, ConnectionAbortedError, socket.timeout, OSError) as exc:
                raise WebSocketClientDisconnected(str(exc)) from exc
            if not chunk:
                raise WebSocketClientDisconnected("peer closed")
            data.extend(chunk)
        return bytes(data)

    def _read_body(self) -> bytes:
        """功能说明：读取 HTTP 请求体。"""

        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length > 0 else b""

    def _read_json(self) -> dict:
        """功能说明：读取并解析 JSON 请求体。"""

        body = self._read_body()
        if not body:
            return {}
        return json.loads(body.decode("utf-8"))

    def _send_json(self, payload: dict, status: int = 200) -> None:
        """功能说明：发送 JSON HTTP 响应。"""

        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(data)

    def _send_bytes(self, data: bytes, content_type: str) -> None:
        """功能说明：发送普通二进制 HTTP 响应。"""

        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(data)

    def _send_chunked_audio(self, producer: Callable[[Callable[[bytes], None]], object]) -> None:
        """功能说明：发送 chunked MP3 响应，供本地流式调试使用。"""

        self.send_response(200)
        self.send_header("Content-Type", "audio/mpeg")
        self.send_header("Transfer-Encoding", "chunked")
        self.send_header("Connection", "close")
        self.end_headers()

        def send(chunk: bytes) -> None:
            if not chunk:
                return
            self.wfile.write(f"{len(chunk):X}\r\n".encode("ascii"))
            self.wfile.write(chunk)
            self.wfile.write(b"\r\n")
            self.wfile.flush()

        producer(send)
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()


def build_server(config: ServerConfig) -> ThreadingHTTPServer:
    """功能说明：构造 HTTP/WebSocket 服务器并注入各功能模块。"""

    asr = build_asr_client(config)
    tts = build_tts_client(config)
    llm = build_llm_client(config)
    memory = ConversationMemory(config.memory_db_path, config.memory_max_stored_turns) if config.memory_enable else None
    if config.realtime_provider == "qwen":
        realtime_memory = (
            DoubaoRealtimeMemory(config.qwen_realtime_memory_db_path, config.qwen_realtime_memory_max_stored_turns)
            if config.qwen_realtime_memory_enable
            else None
        )
        realtime_memory_db_path = config.qwen_realtime_memory_db_path
    else:
        realtime_memory = (
            DoubaoRealtimeMemory(config.doubao_realtime_memory_db_path, config.doubao_realtime_memory_max_stored_turns)
            if config.doubao_realtime_memory_enable
            else None
        )
        realtime_memory_db_path = config.doubao_realtime_memory_db_path
    weather = QWeatherClient(config) if config.weather_enable else None
    music = LocalMusicLibrary(config) if config.music_enable else None
    tool_router = LLMToolRouter(config, llm) if config.tool_router_enable else None
    pipeline = VoicePipeline(
        asr,
        tts,
        llm,
        weather=weather,
        music=music,
        memory=memory,
        tool_router=tool_router,
        memory_recent_turns=config.memory_max_recent_turns,
    )
    server = ThreadingHTTPServer((config.host, config.port), VoiceRequestHandler)
    server.config = config  # type: ignore[attr-defined]
    server.pipeline = pipeline  # type: ignore[attr-defined]
    server.memory = memory  # type: ignore[attr-defined]
    server.realtime_memory = realtime_memory  # type: ignore[attr-defined]
    server.music = music  # type: ignore[attr-defined]
    if memory:
        print(f"voice_server memory enabled db={config.memory_db_path}")
    if realtime_memory:
        print(
            f"voice_server realtime memory enabled provider={config.realtime_provider} "
            f"db={realtime_memory_db_path}"
        )
    if weather:
        print(f"voice_server weather enabled default_city={config.weather_default_city} geo={config.qweather_geo_enable}")
    if music:
        print(f"voice_server music enabled dir={config.music_dir} tracks={len(music.tracks)} chunk_bytes={config.music_chunk_bytes}")
    if tool_router:
        print(
            "voice_server tool_router enabled "
            f"min_confidence={config.tool_router_min_confidence} "
            f"music_min_confidence={config.tool_router_music_min_confidence}"
        )
    return server


def _setup_log_file(port: int, log_dir: str | None, no_log_file: bool) -> None:
    """功能说明：按启动参数设置自动日志文件。"""

    if no_log_file:
        return
    root = Path(__file__).resolve().parents[1]
    directory = Path(log_dir) if log_dir else root / "logs"
    if not directory.is_absolute():
        directory = root / directory
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"server_{datetime.now().strftime('%Y%m%d_%H%M%S')}_port{port}.txt"
    log_file = path.open("a", encoding="utf-8", buffering=1)
    sys.stdout = TimestampTee(sys.stdout, log_file)  # type: ignore[assignment]
    sys.stderr = TimestampTee(sys.stderr, log_file)  # type: ignore[assignment]
    print(f"Log file: {path}")


def _trace(req: str, stage: str, fields: dict) -> None:
    """功能说明：打印统一流水线阶段日志。"""

    print(f"voice_server trace stage={stage} req={req} fields={fields}")


def _stream_iter(chunks, send: Callable[[bytes], None]) -> None:
    """功能说明：把任意音频迭代器转发给 HTTP chunked 发送回调。"""

    for chunk in chunks:
        send(chunk)


def _result_json(result) -> dict:
    """功能说明：把 `DialogueResult` 转成 HTTP JSON。"""

    return {
        "ok": True,
        "question": result.question,
        "asr_text": result.question,
        "answer_text": result.answer_text,
        "audio_base64": base64.b64encode(result.audio).decode("ascii"),
        "audio_bytes": len(result.audio),
        "timings_ms": result.timings_ms,
        "action": result.action,
    }


def _new_req() -> str:
    """功能说明：生成短请求 ID，便于 ESP32 与服务器日志对齐。"""

    return uuid.uuid4().hex[:6]


def _elapsed_ms(start: float) -> int:
    """功能说明：计算毫秒耗时。"""

    return int((time.perf_counter() - start) * 1000)


def main(argv: list[str] | None = None) -> int:
    """功能说明：命令行启动服务器。"""

    parser = argparse.ArgumentParser(description="ASR/LLM/TTS voice server")
    parser.add_argument("--host", help="监听地址，默认读取 VOICE_SERVER_HOST")
    parser.add_argument("--port", type=int, help="监听端口，默认读取 VOICE_SERVER_PORT")
    parser.add_argument("--no-log-file", action="store_true", help="本次启动不自动保存日志文件")
    parser.add_argument("--log-dir", help="日志目录，默认 ASR_LLM_TTS_Server/logs")
    args = parser.parse_args(argv)

    config = ServerConfig.load()
    if args.host:
        config = config.__class__(**{**config.__dict__, "host": args.host})
    if args.port:
        config = config.__class__(**{**config.__dict__, "port": args.port})
    _setup_log_file(config.port, args.log_dir, args.no_log_file)
    server = build_server(config)
    print(f"voice_server listening host={config.host} port={config.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("voice_server stopping")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
