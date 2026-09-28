"""CI 格式 Speex 帧流解码为 16k PCM。"""

from __future__ import annotations

import os
import struct
import subprocess
import tempfile
from pathlib import Path

from .errors import ProviderError


def decode_ci_speex_to_pcm(data: bytes) -> bytes:
    """功能说明：把 CI 上传的 `[1字节长度][42字节帧]` Speex 流解码为裸 PCM。

    入参含义：`data` 是 ESP32 透传上来的 CI Speex 字节流。
    返回值说明：返回 16 kHz、16-bit、mono、小端裸 PCM。
    使用注意事项：依赖 `imageio-ffmpeg` 自带 ffmpeg；如果输入为空则返回空字节。
    """

    if not data:
        return b""
    frames = _extract_ci_frames(data)
    if not frames:
        raise ProviderError("Speex 数据中没有有效帧")
    ogg_data = _build_ogg_speex(frames)
    try:
        import imageio_ffmpeg

        ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as exc:  # noqa: BLE001
        raise ProviderError(f"找不到 imageio-ffmpeg: {exc}") from exc
    with tempfile.TemporaryDirectory() as temp_dir:
        ogg_path = Path(temp_dir) / "input.spx"
        pcm_path = Path(temp_dir) / "output.pcm"
        ogg_path.write_bytes(ogg_data)
        cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-i", str(ogg_path), "-f", "s16le", "-ar", "16000", "-ac", "1", str(pcm_path)]
        result = subprocess.run(cmd, capture_output=True, timeout=20)
        if result.returncode != 0:
            detail = result.stderr.decode("utf-8", errors="replace")
            raise ProviderError(f"ffmpeg Speex 解码失败: {detail}")
        return pcm_path.read_bytes()


def _extract_ci_frames(data: bytes) -> list[bytes]:
    """功能说明：解析 CI Speex 帧，兼容末尾不完整数据直接丢弃。"""

    frames: list[bytes] = []
    offset = 0
    while offset < len(data):
        frame_len = data[offset]
        offset += 1
        if frame_len <= 0 or offset + frame_len > len(data):
            break
        frames.append(data[offset : offset + frame_len])
        offset += frame_len
    return frames


def _build_ogg_speex(frames: list[bytes]) -> bytes:
    """功能说明：构造最小 Ogg Speex 容器，供 ffmpeg 解码。"""

    serial = int.from_bytes(os.urandom(4), "little")
    header = bytearray(80)
    header[:8] = b"Speex   "
    header[8:28] = b"speex-1.2rc1".ljust(20, b"\0")
    struct.pack_into("<i", header, 28, 1)
    struct.pack_into("<i", header, 32, 80)
    struct.pack_into("<i", header, 36, 16000)
    struct.pack_into("<i", header, 40, 1)
    struct.pack_into("<i", header, 44, 4)
    struct.pack_into("<i", header, 48, -1)
    struct.pack_into("<i", header, 52, 1)
    struct.pack_into("<i", header, 56, -1)
    struct.pack_into("<i", header, 60, 160)
    struct.pack_into("<i", header, 64, 0)
    struct.pack_into("<i", header, 68, 0)
    pages = [
        _ogg_page(serial, 0, 0, 0x02, [bytes(header)]),
        _ogg_page(serial, 1, 0, 0x00, [b""]),
    ]
    granule = 0
    seq = 2
    for frame in frames:
        granule += 320
        pages.append(_ogg_page(serial, seq, granule, 0x04 if seq == len(frames) + 1 else 0x00, [frame]))
        seq += 1
    return b"".join(pages)


def _ogg_page(serial: int, seq: int, granule: int, flags: int, packets: list[bytes]) -> bytes:
    """功能说明：生成单页 Ogg 数据并填入 CRC。"""

    lacing = bytes(len(packet) for packet in packets)
    body = b"".join(packets)
    header = bytearray()
    header.extend(b"OggS")
    header.extend(b"\0")
    header.extend(bytes([flags]))
    header.extend(struct.pack("<qIIiB", granule, serial, seq, 0, len(lacing)))
    header.extend(lacing)
    crc_data = bytes(header) + body
    crc = _ogg_crc(crc_data)
    struct.pack_into("<I", header, 22, crc)
    return bytes(header) + body


def _ogg_crc(data: bytes) -> int:
    """功能说明：计算 Ogg 页 CRC。"""

    crc = 0
    for byte in data:
        crc ^= byte << 24
        for _ in range(8):
            crc = ((crc << 1) ^ 0x04C11DB7) & 0xFFFFFFFF if crc & 0x80000000 else (crc << 1) & 0xFFFFFFFF
    return crc
