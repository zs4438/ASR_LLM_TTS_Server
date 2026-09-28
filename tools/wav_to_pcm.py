"""WAV 转裸 PCM 的本地辅助工具。"""

from __future__ import annotations

import argparse
import wave
from pathlib import Path


def main() -> int:
    """功能说明：从 WAV 文件中提取 16 kHz/16-bit/单声道裸 PCM。

    入参含义：从命令行读取输入 WAV 路径和输出 PCM 路径。
    返回值说明：成功返回 0。
    使用注意事项：本工具不做重采样；WAV 必须已经是 16 kHz、16-bit、单声道。
    """

    parser = argparse.ArgumentParser(description="Extract raw PCM from a 16k/16-bit/mono WAV file")
    parser.add_argument("--wav", required=True, help="输入 WAV 文件")
    parser.add_argument("--out", required=True, help="输出裸 PCM 文件")
    args = parser.parse_args()

    wav_path = Path(args.wav)
    if not wav_path.exists():
        raise SystemExit(f"WAV 文件不存在: {wav_path}")

    with wave.open(str(wav_path), "rb") as wav_file:
        channels = wav_file.getnchannels()
        sample_width = wav_file.getsampwidth()
        sample_rate = wav_file.getframerate()
        frames = wav_file.getnframes()
        if channels != 1 or sample_width != 2 or sample_rate != 16000:
            raise SystemExit(
                "WAV 格式不符合百度 ASR 当前配置。\n"
                f"当前: channels={channels}, sample_width={sample_width}, sample_rate={sample_rate}\n"
                "需要: channels=1, sample_width=2, sample_rate=16000"
            )
        pcm_data = wav_file.readframes(frames)

    out_path = Path(args.out)
    out_path.write_bytes(pcm_data)
    print(f"Saved PCM: {out_path} ({len(pcm_data)} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
