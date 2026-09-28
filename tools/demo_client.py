"""本地 Demo 客户端。"""

from __future__ import annotations

import argparse
import base64
import json
import urllib.request
from pathlib import Path


def main() -> int:
    """功能说明：调用本地语音服务器，测试 PCM 对话或文本转语音。

    入参含义：从命令行读取服务器地址、PCM 路径、文本和输出 MP3 路径。
    返回值说明：成功返回 0。
    使用注意事项：`--pcm` 和 `--text` 二选一；文本模式用于先验证 LLM/TTS。
    """

    parser = argparse.ArgumentParser(description="ASR_LLM_TTS_Server demo client")
    parser.add_argument("--server", default="http://127.0.0.1:8000", help="服务器地址")
    parser.add_argument("--pcm", help="16 kHz 16-bit mono 裸 PCM 文件")
    parser.add_argument("--text", help="直接测试文本问题，跳过 ASR")
    parser.add_argument("--out", default="reply.mp3", help="输出 MP3 文件")
    args = parser.parse_args()

    if bool(args.pcm) == bool(args.text):
        parser.error("--pcm 和 --text 必须二选一")

    if args.pcm:
        pcm_path = Path(args.pcm)
        if not pcm_path.exists():
            raise SystemExit(
                f"PCM 文件不存在: {pcm_path}\n"
                "说明：README 里的 input.pcm 只是示例占位名，需要换成真实 PCM 文件路径。\n"
                "要求：16 kHz / 16-bit / 单声道 / 裸 PCM。"
            )
        if not pcm_path.is_file():
            raise SystemExit(f"PCM 路径不是文件: {pcm_path}")
        body = pcm_path.read_bytes()
        if not body:
            raise SystemExit(f"PCM 文件为空: {pcm_path}")
        request = urllib.request.Request(
            args.server.rstrip("/") + "/v1/dialogue",
            data=body,
            headers={"Content-Type": "application/octet-stream"},
            method="POST",
        )
    else:
        body = json.dumps({"question": args.text}, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            args.server.rstrip("/") + "/v1/text-dialogue",
            data=body,
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )

    with urllib.request.urlopen(request, timeout=180) as response:
        data = json.loads(response.read().decode("utf-8"))
    if not data.get("ok"):
        raise SystemExit(f"server returned error: {data}")

    audio = base64.b64decode(data["audio_base64"])
    Path(args.out).write_bytes(audio)
    print("ASR:", data.get("asr_text") or data.get("question") or "")
    print("Answer:", data["answer_text"])
    print("Timings:", data["timings_ms"])
    print("Saved:", args.out, len(audio), "bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
