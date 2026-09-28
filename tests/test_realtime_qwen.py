"""千问实时 PTT 适配器的无网络协议测试。"""

from __future__ import annotations

import asyncio
import json
import unittest
from dataclasses import replace

from asr_llm_tts_server.config import ServerConfig
from asr_llm_tts_server.errors import ConfigError
from asr_llm_tts_server.realtime_qwen import QwenRealtimeSession


def make_session(config: ServerConfig | None = None) -> QwenRealtimeSession:
    """创建不启动网络线程的测试会话。"""

    return QwenRealtimeSession(
        config or ServerConfig.load(),
        on_audio=lambda _chunk: None,
        on_trace=lambda _stage, _fields: None,
        on_done=lambda _metrics: None,
        on_error=lambda _exc: None,
        context_items=[
            {"role": "user", "text": "我叫小明"},
            {"role": "assistant", "text": "你好，小明"},
        ],
    )


class FakeWebSocket:
    """记录适配器发送事件，并在 response.create 后结束发送循环。"""

    def __init__(self, session: QwenRealtimeSession) -> None:
        self.session = session
        self.events: list[dict[str, object]] = []

    async def send(self, payload: str) -> None:
        event = json.loads(payload)
        self.events.append(event)
        if event.get("type") == "response.create":
            self.session._finished.set()  # noqa: SLF001 - deterministic protocol test


class QwenRealtimeProtocolTest(unittest.TestCase):
    """验证千问首版始终采用客户端决定轮次边界。"""

    def test_session_update_disables_server_vad(self) -> None:
        session = make_session(replace(ServerConfig.load(), qwen_realtime_web_search_enable=True))
        event = session._session_update_event()  # noqa: SLF001
        config = event["session"]
        self.assertIsNone(config["turn_detection"])
        self.assertIs(config["enable_search"], True)
        self.assertNotIn("input_audio_format", config)
        self.assertNotIn("output_audio_format", config)
        self.assertIn("用户：我叫小明", config["instructions"])

    def test_session_update_can_disable_native_web_search(self) -> None:
        session = make_session(replace(ServerConfig.load(), qwen_realtime_web_search_enable=False))
        event = session._session_update_event()  # noqa: SLF001
        self.assertIs(event["session"]["enable_search"], False)
        self.assertNotIn("tools", event["session"])

    def test_server_vad_configuration_is_rejected(self) -> None:
        config = replace(ServerConfig.load(), qwen_realtime_turn_mode="server_vad")
        with self.assertRaisesRegex(ConfigError, "禁止 server_vad"):
            config.require_qwen_realtime()

    def test_commit_order_is_tail_then_commit_then_response_create(self) -> None:
        session = make_session()
        ws = FakeWebSocket(session)
        self.assertTrue(session.commit())
        asyncio.run(session._send_commands(ws))  # noqa: SLF001
        event_types = [str(event.get("type")) for event in ws.events]
        tail_packets = session.config.qwen_realtime_tail_silence_ms // 20
        self.assertEqual(event_types[:tail_packets], ["input_audio_buffer.append"] * tail_packets)
        self.assertEqual(event_types[-2:], ["input_audio_buffer.commit", "response.create"])
        self.assertEqual(session.metrics.endpoint_silence_chunks, tail_packets)


if __name__ == "__main__":
    unittest.main()
