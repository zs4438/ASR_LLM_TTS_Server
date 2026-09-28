"""把原始音乐批量转换为更适合 CI 播放器的 MP3 文件。"""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

import imageio_ffmpeg


def main() -> int:
    """功能说明：命令行入口，扫描输入目录并生成规范化 MP3。
    入参含义：通过命令行参数传入输入目录、输出目录和是否覆盖。
    返回值说明：成功返回 0；转换失败时抛出异常并由命令行显示错误。
    使用注意事项：输出格式为 16kHz、单声道、CBR，并尽量去除 ID3 元数据；默认补一小段静音让 CI 精简播放器更容易识别首帧。
    """

    parser = argparse.ArgumentParser(description="Normalize local music files for CI MP3 playback")
    parser.add_argument("--src", required=True, help="原始音乐目录")
    parser.add_argument("--out", required=True, help="输出音乐目录")
    parser.add_argument("--overwrite", action="store_true", help="覆盖已存在的输出文件")
    parser.add_argument("--bitrate", default="64k", help="输出 MP3 码率，默认 64k；硬件不稳定时可试 32k")
    parser.add_argument("--lead-silence-ms", type=int, default=300, help="歌曲开头补静音毫秒数，默认 300；填 0 可关闭")
    parser.add_argument("--no-ci-header-patch", action="store_true", help="不修正首帧中会被 CI 精简播放器误判为 ID3 长度的 4 个字节")
    args = parser.parse_args()

    src_dir = Path(args.src)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    files = [path for path in sorted(src_dir.iterdir(), key=lambda item: item.name.lower()) if path.is_file()]
    for src_path in files:
        if src_path.suffix.lower() not in {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg"}:
            continue
        out_path = out_dir / f"{src_path.stem}.mp3"
        if out_path.exists() and not args.overwrite:
            print(f"skip exists: {out_path.name}", flush=True)
            continue
        command = [ffmpeg, "-y" if args.overwrite else "-n"]
        if args.lead_silence_ms > 0:
            lead_seconds = max(1, args.lead_silence_ms) / 1000.0
            command.extend(
                [
                    "-f",
                    "lavfi",
                    "-t",
                    f"{lead_seconds:.3f}",
                    "-i",
                    "anullsrc=r=16000:cl=mono",
                    "-i",
                    str(src_path),
                    "-filter_complex",
                    "[0:a][1:a]concat=n=2:v=0:a=1[a]",
                    "-map",
                    "[a]",
                ]
            )
        else:
            command.extend(["-i", str(src_path), "-map", "0:a:0"])

        command.extend(
            [
                "-vn",
                "-map_metadata",
                "-1",
                "-ar",
                "16000",
                "-ac",
                "1",
                "-codec:a",
                "libmp3lame",
                "-b:a",
                args.bitrate,
                "-write_xing",
                "0",
                "-id3v2_version",
                "0",
                "-write_id3v1",
                "0",
                str(out_path),
            ]
        )
        print(f"convert: {src_path.name} -> {out_path.name}", flush=True)
        subprocess.run(command, check=True)
        if not args.no_ci_header_patch:
            patch_ci_simple_player_header(out_path)
    return 0


def patch_ci_simple_player_header(path: Path) -> None:
    """功能说明：把裸 MP3 首帧中会被 CI 精简播放器误读为 ID3 长度的字段清零。

    入参含义：`path` 为已经生成的 MP3 文件路径。
    返回值说明：无返回值；文件太短或不是 MP3 同步帧时直接跳过。
    使用注意事项：CI 精简播放器会把开头第 6-9 字节当成自定义头长度字段，
    这里清零后只影响第一帧极短静音，不影响后续歌曲主体。
    """

    with path.open("r+b") as file:
        head = file.read(10)
        if len(head) < 10:
            return
        if head[0] != 0xFF or (head[1] & 0xE0) != 0xE0:
            return
        if head[6:10] == b"\x00\x00\x00\x00":
            return
        file.seek(6)
        file.write(b"\x00\x00\x00\x00")


if __name__ == "__main__":
    raise SystemExit(main())
