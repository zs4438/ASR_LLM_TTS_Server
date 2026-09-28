"""本地流式 Demo 客户端。"""

from __future__ import annotations

import argparse
import json
import time
import urllib.request
from pathlib import Path


def main() -> int:
    """功能说明：请求服务器流式音频接口，边接收边写入 MP3 文件并统计首包耗时。
    入参含义：命令行传入服务器地址、文本/PCM/TTS 输入，以及输出 MP3 路径。
    返回值说明：成功返回 0；参数错误或请求失败会抛出异常并退出。
    使用注意事项：`--text` 用于测试 LLM->TTS 流式链路，`--pcm` 用于测试 ASR->LLM->TTS 流式链路。
    """

    parser = argparse.ArgumentParser(description="ASR_LLM_TTS_Server streaming client")
    parser.add_argument("--server", default="http://127.0.0.1:8000", help="服务器地址")
    parser.add_argument("--text", help="文本问题：调用 /v1/text-dialogue/audio-stream")
    parser.add_argument("--pcm", help="16 kHz / 16-bit / mono 裸 PCM 文件：调用 /v1/dialogue/audio-stream")
    parser.add_argument("--tts", help="只测试 TTS 流式合成：调用 /v1/tts/stream")
    parser.add_argument("--out", default="stream_reply.mp3", help="输出 MP3 文件")
    args = parser.parse_args()

    selected = [bool(args.text), bool(args.pcm), bool(args.tts)]
    if selected.count(True) != 1:
        parser.error("--text、--pcm、--tts 必须三选一")

    request = _build_request(args.server.rstrip("/"), args.text, args.pcm, args.tts)
    out_path = Path(args.out)

    total_start = time.perf_counter()
    first_byte_ms: int | None = None
    total_bytes = 0
    with urllib.request.urlopen(request, timeout=180) as response:
        with out_path.open("wb") as output:
            while True:
                chunk = response.read(4096)
                if not chunk:
                    break
                if first_byte_ms is None:
                    first_byte_ms = _elapsed_ms(total_start)
                output.write(chunk)
                total_bytes += len(chunk)

    print("First audio byte:", first_byte_ms, "ms")
    print("Total:", _elapsed_ms(total_start), "ms")
    print("Saved:", str(out_path), total_bytes, "bytes")
    return 0


def _build_request(server: str, text: str | None, pcm: str | None, tts: str | None) -> urllib.request.Request:
    """功能说明：根据输入模式构造对应的 HTTP 请求。
    入参含义：`server` 是服务器根地址，`text`/`pcm`/`tts` 三者只有一个有值。
    返回值说明：返回可直接传给 `urllib.request.urlopen` 的请求对象。
    使用注意事项：PCM 文件必须是真实裸 PCM，不是 WAV/MP3 容器文件。
    """

    if pcm:
        pcm_path = Path(pcm)
        if not pcm_path.is_file():
            raise SystemExit(f"PCM 文件不存在或不是文件: {pcm_path}")
        body = pcm_path.read_bytes()
        if not body:
            raise SystemExit(f"PCM 文件为空: {pcm_path}")
        return urllib.request.Request(
            server + "/v1/dialogue/audio-stream",
            data=body,
            headers={"Content-Type": "application/octet-stream"},
            method="POST",
        )

    endpoint = "/v1/tts/stream" if tts else "/v1/text-dialogue/audio-stream"
    payload_key = "text" if tts else "question"
    payload_value = tts or text or ""
    body = json.dumps({payload_key: payload_value}, ensure_ascii=False).encode("utf-8")
    return urllib.request.Request(
        server + endpoint,
        data=body,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )


def _elapsed_ms(start: float) -> int:
    """功能说明：计算从指定时间点到现在的毫秒耗时。
    入参含义：`start` 是 `time.perf_counter()` 返回值。
    返回值说明：返回整数毫秒。
    使用注意事项：只用于性能统计，不用于绝对时间。
    """

    return int((time.perf_counter() - start) * 1000)


if __name__ == "__main__":
    raise SystemExit(main())
