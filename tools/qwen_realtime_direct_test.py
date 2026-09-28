"""直连 DashScope 验证千问实时 PTT 与 CI MP3 转码链路。"""

from __future__ import annotations

import argparse
import sys
import threading
import time
import wave
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from asr_llm_tts_server.config import ServerConfig  # noqa: E402
from asr_llm_tts_server.realtime import RealtimeMetrics  # noqa: E402
from asr_llm_tts_server.realtime_qwen import QwenRealtimeSession  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    """构造直连测试参数，输入支持裸 PCM 或标准单声道 WAV。"""

    parser = argparse.ArgumentParser(description="Qwen realtime push-to-talk direct test")
    parser.add_argument("--audio", required=True, type=Path, help="16 kHz/16-bit/mono WAV 或裸 PCM 文件")
    parser.add_argument("--out", type=Path, default=Path("qwen_realtime_direct_output.mp3"), help="保存 CI 兼容 MP3")
    parser.add_argument("--chunk-bytes", type=int, default=640, help="上行帧大小，默认 20 ms PCM")
    parser.add_argument("--timeout", type=float, default=60.0, help="等待本轮完成的最长秒数")
    parser.add_argument("--no-pace", action="store_true", help="不按真实 PCM 时长发送，仅用于压力测试")
    return parser


def read_audio(path: Path) -> bytes:
    """读取并验证 16 kHz/16-bit/mono WAV，或原样读取裸 PCM。"""

    if path.suffix.lower() != ".wav":
        data = path.read_bytes()
        if not data:
            raise ValueError("输入 PCM 文件为空")
        return data
    with wave.open(str(path), "rb") as wav:
        properties = (wav.getframerate(), wav.getsampwidth(), wav.getnchannels(), wav.getcomptype())
        if properties != (16000, 2, 1, "NONE"):
            raise ValueError(
                "WAV 必须是 16 kHz/16-bit/mono PCM，"
                f"当前 rate={properties[0]} width={properties[1]} channels={properties[2]} codec={properties[3]}"
            )
        data = wav.readframes(wav.getnframes())
    if not data:
        raise ValueError("输入 WAV 文件没有音频帧")
    return data


def main() -> int:
    """运行单轮直连测试，成功时落盘 MP3 并打印无敏感信息的指标。"""

    args = build_parser().parse_args()
    if args.chunk_bytes <= 0 or args.chunk_bytes % 2:
        raise ValueError("--chunk-bytes 必须是正偶数")
    pcm = read_audio(args.audio)
    config = ServerConfig.load()
    config.require_qwen_realtime()

    mp3 = bytearray()
    completed = threading.Event()
    result: dict[str, object] = {}

    def on_audio(chunk: bytes) -> None:
        mp3.extend(chunk)

    def on_trace(stage: str, fields: dict[str, object]) -> None:
        if stage in {
            "realtime_session_ready",
            "realtime_capture_finished",
            "realtime_asr_completed",
            "realtime_first_provider_audio",
            "realtime_first_mp3",
            "realtime_answer_completed",
            "realtime_response_done",
        }:
            print(f"stage={stage} fields={fields}")

    def on_done(metrics: RealtimeMetrics) -> None:
        result["metrics"] = metrics
        completed.set()

    def on_error(exc: Exception) -> None:
        result["error"] = exc
        completed.set()

    session = QwenRealtimeSession(
        config,
        on_audio=on_audio,
        on_trace=on_trace,
        on_done=on_done,
        on_error=on_error,
    )
    session.start()
    started = time.perf_counter()
    try:
        for offset in range(0, len(pcm), args.chunk_bytes):
            chunk = pcm[offset : offset + args.chunk_bytes]
            if not session.append_audio(chunk):
                raise RuntimeError("千问实时输入队列不可用或已满")
            if not args.no_pace:
                time.sleep(len(chunk) / 32000.0)
        if not session.commit():
            raise RuntimeError("千问实时 PTT commit 入队失败")
        if not completed.wait(timeout=args.timeout):
            raise TimeoutError(f"等待千问实时响应超时: {args.timeout:g} 秒")
        if "error" in result:
            raise RuntimeError(str(result["error"]))
        metrics = result.get("metrics")
        if not isinstance(metrics, RealtimeMetrics):
            raise RuntimeError("千问实时结束但没有返回指标")
        if not mp3:
            raise RuntimeError("千问实时没有返回可播放 MP3")
        if len(mp3) < 2 or mp3[0] != 0xFF or (mp3[1] & 0xE0) != 0xE0:
            raise RuntimeError(f"输出不是预期的 MPEG MP3，header={bytes(mp3[:8]).hex()}")
        args.out.write_bytes(mp3)
        print(
            "qwen_direct_test_ok "
            f"elapsed_ms={int((time.perf_counter() - started) * 1000)} "
            f"input_pcm_bytes={len(pcm)} output_pcm_bytes={metrics.output_pcm_bytes} "
            f"output_mp3_bytes={len(mp3)} asr_text={metrics.asr_text!r} "
            f"answer_text={metrics.answer_text!r} out={args.out}"
        )
        return 0
    finally:
        session.cancel()


if __name__ == "__main__":
    raise SystemExit(main())
