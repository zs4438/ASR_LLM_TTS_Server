"""TTS 文本转语音测试客户端。

用法示例：
    python tools/tts_client.py --server http://127.0.0.1:8001 --text "好的" --out [199]好的.wav
    python tools/tts_client.py --server http://127.0.0.1:8001 --text "今天天气不错" --stream --out tts_stream.mp3

说明：
    - 默认调用 /v1/tts（一次性合成）；加 --stream 调用 /v1/tts/stream（流式）。
    - 服务器返回 JSON 错误时会原样打印出来，方便排查，而不是当成音频保存。
"""

from __future__ import annotations

import argparse
import json
import urllib.error
import urllib.request
from pathlib import Path


def main() -> int:
    """功能说明：把文本交给服务器 TTS 合成并保存为 MP3。

    入参含义：命令行参数指定服务器地址、文本、输出文件与是否流式。
    返回值说明：成功返回 0，失败返回 1。
    使用注意事项：文本走 UTF-8 传输，中文不会乱码。
    """

    parser = argparse.ArgumentParser(description="TTS 文本转语音测试客户端")
    parser.add_argument("--server", default="http://127.0.0.1:8001", help="服务器地址，默认 http://127.0.0.1:8001")
    parser.add_argument("--text", required=True, help="要合成的文本（支持中文）")
    parser.add_argument("--out", default="tts_client.mp3", help="输出 MP3 文件路径")
    parser.add_argument("--stream", action="store_true", help="使用流式接口 /v1/tts/stream")
    args = parser.parse_args()

    endpoint = "/v1/tts/stream" if args.stream else "/v1/tts"
    body = json.dumps({"text": args.text}, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        args.server.rstrip("/") + endpoint,
        data=body,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            content_type = response.headers.get("Content-Type", "")
            data = response.read()
    except urllib.error.HTTPError as exc:
        error_body = exc.read().decode("utf-8", errors="replace")
        raise SystemExit(f"服务器返回 HTTP {exc.code}:\n{error_body}")
    except urllib.error.URLError as exc:
        raise SystemExit(
            f"连接服务器失败: {exc}\n"
            "请确认服务器已启动（python -m asr_llm_tts_server.server --host 0.0.0.0 --port 8001）"
        )

    # 音频响应：Content-Type 含 audio，或数据头是 MP3 帧同步字（FF FB/F3）或 ID3 头（49 44 33）
    is_audio = "audio" in content_type or data[:2] == b"\xff\xfb" or data[:3] == b"ID3"
    if is_audio and data:
        Path(args.out).write_bytes(data)
        print(f"OK: 已保存 {args.out}（{len(data)} 字节，Content-Type: {content_type}）")
        return 0

    print("服务器未返回音频，原始响应如下（可能是错误信息）：")
    print(data.decode("utf-8", errors="replace")[:2000])
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
