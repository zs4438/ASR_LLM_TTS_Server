"""豆包实时对话服务的直连诊断客户端。"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import sys
import time
import uuid
import wave
from pathlib import Path
from typing import Any


# 服务端工程根目录，用于加载与运行服务完全一致的 .env 配置。
SERVER_ROOT = Path(__file__).resolve().parents[1]
if str(SERVER_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVER_ROOT))

from asr_llm_tts_server.config import ServerConfig


# 随附的标准语音样本，可用于排除 ESP32 和本地服务端后的供应商直连验证。
DEFAULT_AUDIO = (
    SERVER_ROOT.parent
    / "示例代码"
    / "python3.7_duplex_demo"
    / "python3.7_duplex_demo"
    / "whoareyou.wav"
)


def parse_args() -> argparse.Namespace:
    """解析直连诊断参数，运行期间不输出 API 密钥。"""

    parser = argparse.ArgumentParser(description="Direct Doubao realtime diagnostic client")
    parser.add_argument("--audio", type=Path, default=DEFAULT_AUDIO, help="16 kHz mono s16le WAV or raw PCM input")
    parser.add_argument("--chunk-bytes", type=int, default=640, help="PCM bytes per append; 640 bytes equals 20 ms")
    parser.add_argument(
        "--endpoint-silence-max-ms",
        type=int,
        default=3000,
        help="Maximum paced zero PCM before provider endpoint is detected; silence continues until the reply ends",
    )
    parser.add_argument("--timeout", type=float, default=40.0, help="Maximum wait for provider response")
    parser.add_argument("--out", type=Path, help="Optional path for the provider 24 kHz PCM response")
    parser.add_argument(
        "--stop-after-first-audio",
        action="store_true",
        help="Receive the first provider PCM chunk, then finish without waiting for the entire reply",
    )
    return parser.parse_args()


def load_pcm(path: Path) -> bytes:
    """读取并校验 16 kHz、单声道、16 位 WAV，或直接读取裸 PCM。"""

    if path.suffix.lower() != ".wav":
        data = path.read_bytes()
        if not data:
            raise ValueError(f"Audio file is empty: {path}")
        return data

    with wave.open(str(path), "rb") as source:
        if source.getnchannels() != 1 or source.getsampwidth() != 2 or source.getframerate() != 16000:
            raise ValueError(
                "WAV must be 16 kHz, mono, 16-bit PCM; "
                f"got rate={source.getframerate()} channels={source.getnchannels()} width={source.getsampwidth()}"
            )
        data = source.readframes(source.getnframes())
    if not data:
        raise ValueError(f"Audio file is empty: {path}")
    return data


def build_session(config: ServerConfig) -> dict[str, Any]:
    """构造与本地实时适配器完全一致的豆包 session.create 事件。"""

    return {
        "type": "session.create",
        "event_id": "direct_event_1",
        "session": {
            "id": str(uuid.uuid4()),
            "model": config.doubao_realtime_model,
            "instructions": config.doubao_realtime_system_prompt,
            "audio": {
                "input": {"format": {"type": config.doubao_realtime_asr_format, "rate": 16000}},
                "output": {
                    "format": {"type": config.doubao_realtime_tts_format, "rate": config.realtime_output_sample_rate},
                    "voice": config.doubao_realtime_voice,
                },
            },
            "tools": [],
        },
        "extension": {"asr": {"extra": {}}, "tts": {"extra": {}}, "dialog": {"extra": {"enable_music": False}}},
    }


async def connect_provider(url: str, api_key: str) -> Any:
    """兼容不同 websockets 版本建立供应商 WebSocket 连接。"""

    import websockets

    kwargs = {"ping_interval": None, "open_timeout": 12}
    headers = {"X-Api-Key": api_key}
    try:
        return await websockets.connect(url, additional_headers=headers, **kwargs)
    except TypeError:
        return await websockets.connect(url, extra_headers=headers, **kwargs)


async def run(args: argparse.Namespace) -> int:
    """按 ESP32 的节奏直连发送 PCM，并输出供应商关键阶段。"""

    if args.chunk_bytes <= 0 or args.chunk_bytes % 2:
        raise ValueError("--chunk-bytes must be a positive even number")
    if args.endpoint_silence_max_ms <= 0:
        raise ValueError("--endpoint-silence-max-ms must be positive")

    config = ServerConfig.load(SERVER_ROOT / ".env")
    config.require_doubao_realtime()
    pcm = load_pcm(args.audio.resolve())
    started = time.perf_counter()
    terminal = asyncio.Event()
    endpoint_detected = asyncio.Event()
    response_audio = bytearray()
    result: dict[str, object] = {"error": "", "terminal_type": "", "transcript": "", "first_audio_ms": None}

    async with await connect_provider(config.doubao_realtime_url, config.doubao_realtime_api_key) as ws:
        await ws.send(json.dumps(build_session(config), ensure_ascii=False, separators=(",", ":")))
        while True:
            event = json.loads(await asyncio.wait_for(ws.recv(), timeout=12))
            event_type = str(event.get("type") or "")
            print(f"provider_event={event_type}")
            if event_type == "session.created":
                break
            if event_type == "error":
                raise RuntimeError(str(event.get("error") or event))

        async def receive() -> None:
            """在上行 PCM 的同时持续接收供应商事件，避免收包滞后。"""

            while not terminal.is_set():
                event = json.loads(await ws.recv())
                event_type = str(event.get("type") or "")
                if event_type == "response.output_audio.delta":
                    endpoint_detected.set()
                    delta = str(event.get("delta") or "")
                    audio = base64.b64decode(delta)
                    if audio:
                        response_audio.extend(audio)
                        if result["first_audio_ms"] is None:
                            result["first_audio_ms"] = int((time.perf_counter() - started) * 1000)
                            print(f"provider_first_audio_ms={result['first_audio_ms']}")
                            if args.stop_after_first_audio:
                                result["terminal_type"] = "first_audio"
                                terminal.set()
                                return
                    continue
                if event_type.endswith("input_audio_transcription.delta"):
                    print(f"provider_event={event_type} delta={str(event.get('delta') or '')}")
                    continue
                if event_type.endswith("input_audio_transcription.completed"):
                    endpoint_detected.set()
                    result["transcript"] = str(event.get("transcript") or event.get("text") or "")
                    print(f"provider_event={event_type} transcript={result['transcript']}")
                    continue
                if event_type in ("response.output_audio.started", "response.output_text.delta"):
                    endpoint_detected.set()
                    print(f"provider_event={event_type}")
                    continue
                if event_type == "error":
                    result["error"] = str(event.get("error") or event)
                    result["terminal_type"] = event_type
                    terminal.set()
                    return
                print(f"provider_event={event_type}")
                if event_type in ("response.output_audio.done", "response.done", "session.closed"):
                    result["terminal_type"] = event_type
                    terminal.set()
                    return

        receiver = asyncio.create_task(receive())
        try:
            for offset in range(0, len(pcm), args.chunk_bytes):
                chunk = pcm[offset : offset + args.chunk_bytes]
                await ws.send(
                    json.dumps(
                        {"type": "input_audio_buffer.append", "audio": base64.b64encode(chunk).decode("ascii")},
                        separators=(",", ":"),
                    )
                )
                await asyncio.sleep(len(chunk) / 32000.0)
            endpoint_deadline = time.monotonic() + (args.endpoint_silence_max_ms / 1000.0)
            response_deadline = time.monotonic() + args.timeout
            endpoint_packets = 0
            while not terminal.is_set():
                if not endpoint_detected.is_set() and time.monotonic() >= endpoint_deadline:
                    raise RuntimeError(
                        f"Provider endpoint did not complete within {args.endpoint_silence_max_ms} ms of paced silence"
                    )
                if time.monotonic() >= response_deadline:
                    raise RuntimeError(f"Provider reply did not finish within {args.timeout:g} seconds")
                chunk = b"\x00" * args.chunk_bytes
                await ws.send(
                    json.dumps(
                        {"type": "input_audio_buffer.append", "audio": base64.b64encode(chunk).decode("ascii")},
                        separators=(",", ":"),
                    )
                )
                endpoint_packets += 1
                await asyncio.sleep(0.020)
            print(
                f"provider_reply_finished pcm_bytes={len(pcm)} "
                f"silence_packets={endpoint_packets} silence_ms={endpoint_packets * 20}"
            )
        finally:
            if not receiver.done():
                receiver.cancel()
            try:
                await receiver
            except asyncio.CancelledError:
                pass

    elapsed_ms = int((time.perf_counter() - started) * 1000)
    print(
        "provider_result "
        f"terminal={result['terminal_type']} elapsed_ms={elapsed_ms} "
        f"transcript={result['transcript']!r} output_pcm_bytes={len(response_audio)}"
    )
    if args.out and response_audio:
        args.out.write_bytes(response_audio)
        print(f"saved_output_pcm={args.out.resolve()}")
    if result["error"]:
        raise RuntimeError(str(result["error"]))
    if not response_audio:
        raise RuntimeError("Provider returned no output audio")
    return 0


def main() -> int:
    """作为命令行入口执行一次直连诊断。"""

    return asyncio.run(run(parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
