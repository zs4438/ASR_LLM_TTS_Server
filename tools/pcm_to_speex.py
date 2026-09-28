"""PCM → CI 格式裸 SPEEX 帧流转换工具。

CI 芯片端（cias_speex）上传的 speex 不是 Ogg 封装，而是**裸 speex 帧流**，
每帧格式为：`[1 字节长度头][speex 编码帧数据]`，每帧 20ms（16k wideband，
frame_size=320 样本），帧数据约 40 字节。

本工具流程：PCM → ffmpeg 编码为 Ogg Speex → 解析 Ogg 页提取音频包 → 输出：
  --raw  裸 speex 帧流（只拼帧数据，无长度头）
  --ci   CI 格式帧流（每帧前加 1 字节长度头，与 CI 端 cias_speex_compressed_data
         输出格式一致：buffer_out[0]=len，后续为帧数据）

用法示例：
    python tools/pcm_to_speex.py --pcm test_input.pcm --out test_input.speex.raw --raw
    python tools/pcm_to_speex.py --pcm test_input.pcm --out test_input.speex.ci --ci
"""

from __future__ import annotations

import argparse
import struct
import subprocess
from collections import Counter
from pathlib import Path


def parse_ogg_packets(data: bytes) -> list[bytes]:
    """功能说明：解析 Ogg 容器，返回所有包（packet）列表。

    入参含义：`data` 是 Ogg 文件字节。
    返回值说明：返回包列表；第一个包通常是 Speex 头（80 字节，含 Speex 签名）。
    使用注意事项：只处理非分页续帧的简单情况，speex 每个包就是一帧。
    """

    packets: list[bytes] = []
    pos = 0
    while pos + 27 <= len(data):
        if data[pos : pos + 4] != b"OggS":
            break
        header_type = data[pos + 5]
        # lacing values
        seg_count = data[pos + 26]
        seg_table = data[pos + 27 : pos + 27 + seg_count]
        pos = pos + 27 + seg_count
        packet = bytearray()
        for seg_len in seg_table:
            packet.extend(data[pos : pos + seg_len])
            pos += seg_len
            if seg_len < 255:
                packets.append(bytes(packet))
                packet = bytearray()
        if packet:
            packets.append(bytes(packet))
        if header_type & 0x04:  # end of stream
            break
    return packets


def main() -> int:
    """功能说明：把裸 PCM 编码为 CI 格式（或裸）speex 帧流。

    入参含义：命令行参数指定 PCM 路径、输出路径与输出模式。
    返回值说明：成功返回 0。
    使用注意事项：依赖 imageio-ffmpeg（项目 .venv 已安装）。
    """

    parser = argparse.ArgumentParser(description="PCM 转 CI 格式裸 SPEEX 帧流")
    parser.add_argument("--pcm", required=True, help="输入裸 PCM（16kHz 16bit mono）")
    parser.add_argument("--out", required=True, help="输出文件")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--raw", action="store_true", help="输出裸 speex 帧流（无长度头）")
    group.add_argument("--ci", action="store_true", help="输出 CI 格式帧流（1 字节长度头 + 帧）")
    parser.add_argument("--rate", type=int, default=16000, help="采样率，默认 16000")
    args = parser.parse_args()

    pcm_path = Path(args.pcm)
    if not pcm_path.is_file():
        raise SystemExit(f"PCM 文件不存在: {pcm_path}")

    import imageio_ffmpeg

    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    tmp_spx = pcm_path.with_suffix(".tmp.spx")
    cmd = [
        ffmpeg,
        "-y",
        "-f", "s16le",
        "-ar", str(args.rate),
        "-ac", "1",
        "-i", str(pcm_path),
        "-c:a", "libspeex",
        "-cbr_quality", "5",  # 16k wideband 下每帧 42 字节，与 CI 端 cias_speex 输出一致（+1 长度头=43）
        str(tmp_spx),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        tmp_spx.unlink(missing_ok=True)
        raise SystemExit(f"ffmpeg 编码失败:\n{result.stderr}")

    try:
        packets = parse_ogg_packets(tmp_spx.read_bytes())
    finally:
        tmp_spx.unlink(missing_ok=True)

    if len(packets) < 2:
        raise SystemExit("Ogg Speex 解析失败：未找到音频帧")

    header = packets[0]
    # 跳过 Speex 头；再用"长度众数"过滤：音频帧 CBR 长度一致，vorbiscomment 注释头（以 4 字节长度前缀开头）长度不同
    candidates = [p for p in packets[1:] if not p.startswith(b"Speex   ")]
    if not candidates:
        raise SystemExit("Ogg Speex 解析失败：未找到音频帧")
    common_len = Counter(len(p) for p in candidates).most_common(1)[0][0]
    frames = [p for p in candidates if len(p) == common_len]
    if not header.startswith(b"Speex   ") or not frames:
        raise SystemExit(f"Ogg Speex 解析失败: 头={header[:16]!r} 帧数={len(frames)}")

    if args.raw:
        out_bytes = b"".join(frames)
        print(f"裸 speex 帧流: {len(frames)} 帧, {len(out_bytes)} 字节（无长度头）")
    else:
        out_bytes = b"".join(bytes([len(f)]) + f for f in frames)
        print(f"CI 格式帧流: {len(frames)} 帧, {len(out_bytes)} 字节（1 字节长度头 + 帧）")

    Path(args.out).write_bytes(out_bytes)
    print(f"已保存: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
