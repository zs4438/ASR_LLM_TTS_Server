"""将已构建的固件作为不可变 OTA 产物发布到本地 catalog。"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 1
CHUNK_BYTES = 64 * 1024
TARGET_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
VERSION_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$")


class PublishError(ValueError):
    """发布参数、catalog 或固件文件不满足发布要求。"""


def _require_identifier(value: str, pattern: re.Pattern[str], label: str) -> str:
    if not pattern.fullmatch(value):
        raise PublishError(f"{label} 格式不合法: {value!r}")
    return value


def _load_catalog(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"schema_version": SCHEMA_VERSION, "artifacts": {}, "channels": {}}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PublishError(f"无法读取 catalog: {path}") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != SCHEMA_VERSION:
        raise PublishError("catalog schema_version 不受支持")
    if not isinstance(payload.get("artifacts"), dict) or not isinstance(payload.get("channels"), dict):
        raise PublishError("catalog 缺少 artifacts 或 channels 对象")
    return payload


def _write_catalog(path: Path, catalog: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            json.dump(catalog, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        raise


def _copy_and_hash(source: Path, destination: Path) -> tuple[int, str]:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=destination.parent,
            prefix=f".{destination.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            digest = hashlib.sha256()
            size = 0
            with source.open("rb") as input_file:
                while chunk := input_file.read(CHUNK_BYTES):
                    handle.write(chunk)
                    digest.update(chunk)
                    size += len(chunk)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
        return size, digest.hexdigest()
    except Exception:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        raise


def publish_release(
    source: Path,
    target: str,
    version: str,
    channel: str,
    catalog_path: Path,
    artifact_root: Path,
    model: str = "",
    replace: bool = False,
) -> dict[str, Any]:
    """复制固件、计算元数据，并原子更新发布 catalog。"""

    source = source.resolve()
    if not source.is_file():
        raise PublishError(f"固件文件不存在: {source}")
    target = _require_identifier(target, TARGET_PATTERN, "target")
    version = _require_identifier(version, VERSION_PATTERN, "version")
    channel = _require_identifier(channel, TARGET_PATTERN, "channel")
    filename = source.name
    if filename in {"", ".", ".."} or "/" in filename or "\\" in filename:
        raise PublishError("固件文件名不合法")

    artifact_root = artifact_root.resolve()
    destination = artifact_root / target / version / filename
    catalog = _load_catalog(catalog_path)
    artifacts = catalog["artifacts"].setdefault(target, {})
    if version in artifacts and not replace:
        raise PublishError(f"{target} {version} 已发布；版本号必须不可变")
    if destination.exists() and not replace:
        raise PublishError(f"目标固件已存在: {destination}")

    size, sha256 = _copy_and_hash(source, destination)
    artifact = {
        "artifact": destination.relative_to(artifact_root).as_posix(),
        "filename": filename,
        "model": model,
        "published_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "sha256": sha256,
        "size": size,
        "version": version,
    }
    artifacts[version] = artifact
    catalog["channels"].setdefault(channel, {})[target] = version
    _write_catalog(catalog_path, catalog)
    return artifact


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="发布 OTA 固件并自动生成 SHA-256 catalog")
    parser.add_argument("--file", required=True, type=Path, help="已构建的固件文件")
    parser.add_argument("--target", required=True, help="目标，例如 esp32 或 ci1306")
    parser.add_argument("--version", required=True, help="发布版本，例如 1.0.13")
    parser.add_argument("--channel", default="stable", help="发布通道，默认 stable")
    parser.add_argument("--model", default="", help="可选硬件型号标识")
    parser.add_argument("--catalog", type=Path, default=Path(__file__).with_name("catalog.json"))
    parser.add_argument("--artifact-root", type=Path, default=Path(__file__).with_name("artifacts"))
    parser.add_argument("--replace", action="store_true", help="仅开发环境允许替换已发布版本")
    args = parser.parse_args(argv)
    try:
        artifact = publish_release(
            source=args.file,
            target=args.target,
            version=args.version,
            channel=args.channel,
            catalog_path=args.catalog,
            artifact_root=args.artifact_root,
            model=args.model,
            replace=args.replace,
        )
    except PublishError as exc:
        parser.error(str(exc))
    print(json.dumps(artifact, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
