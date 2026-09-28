"""实时 WebSocket 路线的本地验收客户端。"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from pathlib import Path

import websockets


def build_parser() -> argparse.ArgumentParser:
    """构建命令行参数，约束实时路线使用 16 kHz PCM 输入。"""

    parser = argparse.ArgumentParser(description="Realtime voice-route WebSocket test client")
    parser.add_argument("--server", default="ws://127.0.0.1:8001/v1/dialogue/ws", help="语音服务器 WebSocket 地址")
    parser.add_argument("--pcm", required=True, type=Path, help="16 kHz / 16-bit / mono 裸 PCM 文件")
    parser.add_argument("--out", type=Path, help="保存收到的 CI 兼容 MP3 文件")
    parser.add_argument("--chunk-bytes", type=int, default=640, help="每帧 PCM 字节数，默认对应 20 ms")
    parser.add_argument("--pace", action="store_true", help="按 PCM 时长节奏发送，模拟 ESP32 采集期上行")
    parser.add_argument("--timeout", type=float, default=60.0, help="等待模型完成的最长秒数")
    parser.add_argument("--client-req", type=int, default=99001, help="用于服务端与设备日志关联的请求序号")
    parser.add_argument("--cancel-after-commit", action="store_true", help="提交后立即取消，并检查是否还有下行 MP3")
    return parser


async def run(args: argparse.Namespace) -> int:
    """建立实时会话、按序发送 PCM 和 commit，收集下行 MP3 或取消确认。"""

    if args.chunk_bytes <= 0 or args.chunk_bytes % 2 != 0:
        raise ValueError("--chunk-bytes 必须是正偶数")

    pcm = args.pcm.read_bytes()
    if not pcm:
        raise ValueError("PCM 文件为空")

    started = time.perf_counter()
    audio = bytearray()
    first_audio_ms: int | None = None
    terminal_event: dict[str, object] | None = None
    post_cancel_audio = 0

    async with websockets.connect(args.server, open_timeout=10, max_size=2**22) as ws:
        await ws.send(
            json.dumps(
                {
                    "type": "dialogue.start",
                    "client_req": args.client_req,
                    "device_sn": "realtime-local-test",
                    "device_ip": "127.0.0.1",
                    "audio_format": "pcm",
                    "route": "realtime",
                }
            )
        )
        ready = json.loads(await asyncio.wait_for(ws.recv(), timeout=15))
        if ready.get("type") != "dialogue.ready":
            raise RuntimeError(f"实时会话未就绪: {ready}")
        print(f"dialogue.ready route={ready.get('route')} client_req={ready.get('client_req')}")

        # 每帧默认 640 字节，即 16 kHz / 16-bit / mono 的 20 ms PCM。
        for offset in range(0, len(pcm), args.chunk_bytes):
            chunk = pcm[offset : offset + args.chunk_bytes]
            await ws.send(chunk)
            if args.pace:
                await asyncio.sleep(len(chunk) / 32000.0)

        await ws.send(json.dumps({"type": "dialogue.commit"}))
        if args.cancel_after_commit:
            await ws.send(
                json.dumps(
                    {
                        "type": "dialogue.cancel",
                        "client_req": args.client_req,
                        "reason": "local_realtime_test_cancel",
                    }
                )
            )

        deadline = time.perf_counter() + args.timeout
        while time.perf_counter() < deadline:
            try:
                frame = await asyncio.wait_for(ws.recv(), timeout=min(5.0, max(0.1, deadline - time.perf_counter())))
            except asyncio.TimeoutError:
                continue
            if isinstance(frame, bytes):
                if args.cancel_after_commit:
                    post_cancel_audio += len(frame)
                else:
                    if first_audio_ms is None:
                        first_audio_ms = int((time.perf_counter() - started) * 1000)
                    audio.extend(frame)
                continue

            event = json.loads(frame)
            event_type = str(event.get("type") or "")
            if event_type in ("dialogue.done", "dialogue.error", "dialogue.cancelled"):
                terminal_event = event
                break

    elapsed_ms = int((time.perf_counter() - started) * 1000)
    if terminal_event is None:
        raise TimeoutError(f"等待实时路线结束超时: {args.timeout} s")

    print(f"terminal_event={terminal_event.get('type')} elapsed_ms={elapsed_ms}")
    if terminal_event.get("type") == "dialogue.error":
        raise RuntimeError(str(terminal_event.get("error") or "实时路线返回未知错误"))
    if args.cancel_after_commit:
        print(f"binary_after_cancel={post_cancel_audio}")
        if terminal_event.get("type") != "dialogue.cancelled" or post_cancel_audio:
            raise RuntimeError("取消后仍收到实时 MP3，取消隔离检查失败")
        return 0

    print(f"first_audio_ms={first_audio_ms} mp3_bytes={len(audio)} mp3_header={audio[:4].hex()}")
    if terminal_event.get("type") != "dialogue.done" or not audio:
        raise RuntimeError("实时路线未返回完整 MP3")
    if len(audio) < 2 or audio[0] != 0xFF or (audio[1] & 0xE0) != 0xE0:
        raise RuntimeError("下行音频不是预期的 MPEG MP3 数据")
    if args.out:
        args.out.write_bytes(audio)
        print(f"saved_mp3={args.out}")
    return 0


def main() -> int:
    """解析参数并运行异步验收流程，返回 shell 可识别的退出码。"""

    args = build_parser().parse_args()
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
