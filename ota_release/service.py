"""与语音服务隔离的 OTA manifest/artifact 发布服务。"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import ssl
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from urllib.parse import parse_qs, unquote, urlsplit


SCHEMA_VERSION = 1
COPY_CHUNK_BYTES = 64 * 1024
TARGET_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
VERSION_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
DEVICE_SN_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")


class CatalogError(ValueError):
    """发布 catalog 或其中的产物未通过完整性校验。"""


@dataclass(frozen=True)
class Artifact:
    target: str
    version: str
    path: Path
    filename: str
    sha256: str
    size: int
    model: str
    published_at: str


class ReleaseCatalog:
    """只加载经过文件大小和 SHA-256 复核的已发布条目。"""

    def __init__(self, catalog_path: Path, artifact_root: Path) -> None:
        self.catalog_path = catalog_path.resolve()
        self.artifact_root = artifact_root.resolve()
        self.artifacts: dict[tuple[str, str], Artifact] = {}
        self.channels: dict[str, dict[str, str]] = {}

    def refresh(self) -> None:
        try:
            payload = json.loads(self.catalog_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise CatalogError(f"无法读取 OTA catalog: {self.catalog_path}") from exc
        if not isinstance(payload, dict) or payload.get("schema_version") != SCHEMA_VERSION:
            raise CatalogError("OTA catalog schema_version 不受支持")
        source_artifacts = payload.get("artifacts")
        source_channels = payload.get("channels")
        if not isinstance(source_artifacts, dict) or not isinstance(source_channels, dict):
            raise CatalogError("OTA catalog 必须包含 artifacts 和 channels 对象")

        loaded: dict[tuple[str, str], Artifact] = {}
        for target, versions in source_artifacts.items():
            self._require_identifier(target, TARGET_PATTERN, "target")
            if not isinstance(versions, dict):
                raise CatalogError(f"target {target} 的版本表格式错误")
            for version, record in versions.items():
                self._require_identifier(version, VERSION_PATTERN, "version")
                loaded[(target, version)] = self._parse_artifact(target, version, record)

        channels: dict[str, dict[str, str]] = {}
        for channel, targets in source_channels.items():
            self._require_identifier(channel, TARGET_PATTERN, "channel")
            if not isinstance(targets, dict):
                raise CatalogError(f"channel {channel} 的目标表格式错误")
            channel_targets: dict[str, str] = {}
            for target, version in targets.items():
                self._require_identifier(target, TARGET_PATTERN, "target")
                if not isinstance(version, str):
                    raise CatalogError(f"channel {channel} 的版本不是字符串")
                self._require_identifier(version, VERSION_PATTERN, "version")
                if (target, version) not in loaded:
                    raise CatalogError(f"channel {channel} 指向不存在的 {target} {version}")
                channel_targets[target] = version
            channels[channel] = channel_targets
        self.artifacts = loaded
        self.channels = channels

    def manifest(self, channel: str, base_url: str) -> dict[str, object]:
        target_versions = self.channels.get(channel)
        if target_versions is None:
            raise KeyError(f"发布通道不存在: {channel}")
        items: dict[str, object] = {}
        for target, version in target_versions.items():
            artifact = self.artifact(target, version)
            items[target] = {
                "version": artifact.version,
                "url": f"{base_url}/v1/ota/artifacts/{artifact.target}/{artifact.version}",
                "sha256": artifact.sha256,
                "size": artifact.size,
                "model": artifact.model,
                "published_at": artifact.published_at,
            }
        items["channel"] = channel
        items["schema_version"] = SCHEMA_VERSION
        return items

    def artifact(self, target: str, version: str) -> Artifact:
        try:
            return self.artifacts[(target, version)]
        except KeyError as exc:
            raise KeyError(f"OTA 产物不存在: {target} {version}") from exc

    def _parse_artifact(self, target: str, version: str, record: object) -> Artifact:
        if not isinstance(record, dict):
            raise CatalogError(f"{target} {version} 的产物记录格式错误")
        artifact = record.get("artifact")
        filename = record.get("filename")
        sha256 = record.get("sha256")
        size = record.get("size")
        record_version = record.get("version")
        if not isinstance(artifact, str) or not isinstance(filename, str):
            raise CatalogError(f"{target} {version} 缺少 artifact 或 filename")
        if not isinstance(sha256, str) or not SHA256_PATTERN.fullmatch(sha256.lower()):
            raise CatalogError(f"{target} {version} 的 SHA-256 格式错误")
        if not isinstance(size, int) or size <= 0:
            raise CatalogError(f"{target} {version} 的 size 格式错误")
        if record_version != version:
            raise CatalogError(f"{target} {version} 的 record.version 不一致")
        path = self._artifact_path(artifact)
        if path.name != filename or not path.is_file():
            raise CatalogError(f"{target} {version} 的文件不存在或文件名不一致")
        actual_size = path.stat().st_size
        actual_sha256 = self._hash_file(path)
        if actual_size != size or not hmac.compare_digest(actual_sha256, sha256.lower()):
            raise CatalogError(f"{target} {version} 的文件与 catalog 元数据不一致")
        return Artifact(
            target=target,
            version=version,
            path=path,
            filename=filename,
            sha256=sha256.lower(),
            size=size,
            model=str(record.get("model") or ""),
            published_at=str(record.get("published_at") or ""),
        )

    def _artifact_path(self, relative: str) -> Path:
        pure_path = PurePosixPath(relative)
        if pure_path.is_absolute() or ".." in pure_path.parts or not pure_path.parts:
            raise CatalogError("catalog artifact 路径不安全")
        path = (self.artifact_root.joinpath(*pure_path.parts)).resolve()
        try:
            path.relative_to(self.artifact_root)
        except ValueError as exc:
            raise CatalogError("catalog artifact 路径越界") from exc
        return path

    @staticmethod
    def _hash_file(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            while chunk := handle.read(COPY_CHUNK_BYTES):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _require_identifier(value: object, pattern: re.Pattern[str], label: str) -> str:
        if not isinstance(value, str) or not pattern.fullmatch(value):
            raise CatalogError(f"{label} 格式不合法: {value!r}")
        return value


class OtaReleaseHandler(BaseHTTPRequestHandler):
    """提供固定 manifest 与 artifact 路由，不支持任意文件路径。"""

    server_version = "VoiceOtaRelease/1.0"
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:
        self._serve(send_body=True)

    def do_HEAD(self) -> None:
        self._serve(send_body=False)

    def _serve(self, send_body: bool) -> None:
        parsed = urlsplit(self.path)
        if parsed.path == "/health":
            self._send_json(HTTPStatus.OK, {"ok": True, "service": "voice-ota-release"}, send_body)
            return
        if not self._authorized():
            return
        if parsed.path in {"/v1/ota/manifest", "/manifest.json"}:
            channel = parse_qs(parsed.query).get("channel", ["stable"])[0]
            if not TARGET_PATTERN.fullmatch(channel):
                self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "invalid channel"}, send_body)
                return
            if parsed.path == "/manifest.json":
                print(f"ota_release legacy_manifest_route channel=stable device_sn={self._device_sn_for_log()}")
            try:
                manifest = self._catalog().manifest(channel, self._public_base_url())
            except KeyError:
                self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "release channel not found"}, send_body)
                return
            print(f"ota_release manifest channel={channel} device_sn={self._device_sn_for_log()}")
            self._send_json(HTTPStatus.OK, manifest, send_body, cache_control="no-store")
            return
        if parsed.path.startswith("/v1/ota/artifacts/"):
            self._serve_artifact(parsed.path, send_body)
            return
        self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "not found"}, send_body)

    def _serve_artifact(self, path: str, send_body: bool) -> None:
        parts = path.split("/")
        if len(parts) != 6:
            self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "not found"}, send_body)
            return
        target, version = unquote(parts[4]), unquote(parts[5])
        if not TARGET_PATTERN.fullmatch(target) or not VERSION_PATTERN.fullmatch(version):
            self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "not found"}, send_body)
            return
        try:
            artifact = self._catalog().artifact(target, version)
        except KeyError:
            self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "artifact not found"}, send_body)
            return

        etag = f'"{artifact.sha256}"'
        if self.headers.get("If-None-Match") == etag:
            self.send_response(HTTPStatus.NOT_MODIFIED)
            self.send_header("ETag", etag)
            self.send_header("Cache-Control", "public, max-age=300")
            self.end_headers()
            return
        try:
            start, end = self._range(artifact.size)
        except ValueError:
            self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
            self.send_header("Content-Range", f"bytes */{artifact.size}")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        length = end - start + 1
        partial = start != 0 or end != artifact.size - 1
        self.send_response(HTTPStatus.PARTIAL_CONTENT if partial else HTTPStatus.OK)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("ETag", etag)
        self.send_header("Cache-Control", "public, max-age=300")
        self.send_header("Content-Disposition", f'attachment; filename="{artifact.filename}"')
        if partial:
            self.send_header("Content-Range", f"bytes {start}-{end}/{artifact.size}")
        self.end_headers()
        if not send_body:
            return
        sent = 0
        with artifact.path.open("rb") as handle:
            handle.seek(start)
            while sent < length:
                chunk = handle.read(min(COPY_CHUNK_BYTES, length - sent))
                if not chunk:
                    break
                self.wfile.write(chunk)
                sent += len(chunk)
        self.wfile.flush()
        print(
            f"ota_release artifact target={target} version={version} "
            f"device_sn={self._device_sn_for_log()} status={self.command} bytes={sent}"
        )

    def _range(self, size: int) -> tuple[int, int]:
        value = self.headers.get("Range")
        if not value:
            return 0, size - 1
        if not value.startswith("bytes=") or "," in value:
            raise ValueError("unsupported range")
        start_text, separator, end_text = value[6:].partition("-")
        if not separator:
            raise ValueError("invalid range")
        if not start_text:
            suffix = int(end_text)
            if suffix <= 0:
                raise ValueError("invalid suffix")
            return max(0, size - suffix), size - 1
        start = int(start_text)
        end = size - 1 if not end_text else min(int(end_text), size - 1)
        if start < 0 or start >= size or end < start:
            raise ValueError("unsatisfiable range")
        return start, end

    def _authorized(self) -> bool:
        token = self._server().token
        if not token:
            return True
        presented = self.headers.get("Authorization", "")
        expected = f"Bearer {token}"
        if hmac.compare_digest(presented, expected):
            return True
        self.send_response(HTTPStatus.UNAUTHORIZED)
        self.send_header("WWW-Authenticate", 'Bearer realm="voice-ota-release"')
        self.send_header("Content-Length", "0")
        self.end_headers()
        return False

    def _send_json(
        self,
        status: HTTPStatus,
        payload: dict[str, object],
        send_body: bool,
        cache_control: str = "no-store",
    ) -> None:
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", cache_control)
        self.end_headers()
        if send_body:
            self.wfile.write(data)

    def _public_base_url(self) -> str:
        configured = self._server().public_base_url
        if configured:
            return configured.rstrip("/")
        return f"http://{self.headers.get('Host', '127.0.0.1')}"

    def _catalog(self) -> ReleaseCatalog:
        return self._server().catalog

    def _device_sn_for_log(self) -> str:
        """只记录格式受限的公开设备 SN，避免不可信请求头污染终端日志。"""
        value = self.headers.get("X-Device-SN", "")
        if not value:
            return "unknown"
        return value if DEVICE_SN_PATTERN.fullmatch(value) else "invalid"

    def _server(self):
        return self.server  # type: ignore[return-value]

    def log_message(self, format: str, *args: object) -> None:
        print(
            f"ota_release http client={self.client_address[0]} "
            f"device_sn={self._device_sn_for_log()} {format % args}"
        )


def build_server(
    host: str,
    port: int,
    catalog_path: Path,
    artifact_root: Path,
    token: str = "",
    public_base_url: str = "",
) -> ThreadingHTTPServer:
    catalog = ReleaseCatalog(catalog_path, artifact_root)
    catalog.refresh()
    server = ThreadingHTTPServer((host, port), OtaReleaseHandler)
    server.daemon_threads = True
    server.catalog = catalog  # type: ignore[attr-defined]
    server.token = token  # type: ignore[attr-defined]
    server.public_base_url = public_base_url.rstrip("/")  # type: ignore[attr-defined]
    return server


def main(argv: list[str] | None = None) -> int:
    root = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description="独立 OTA manifest/artifact 发布服务")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--catalog", type=Path, default=root / "catalog.json")
    parser.add_argument("--artifact-root", type=Path, default=root / "artifacts")
    parser.add_argument("--public-base-url", default="", help="设备可访问的外部基础 URL，例如 https://ota.example.com")
    parser.add_argument("--token", default=os.environ.get("OTA_RELEASE_TOKEN", ""), help="可选 Bearer token；优先使用环境变量")
    parser.add_argument("--tls-cert", type=Path, help="HTTPS 证书 PEM 文件")
    parser.add_argument("--tls-key", type=Path, help="HTTPS 私钥 PEM 文件")
    args = parser.parse_args(argv)
    if bool(args.tls_cert) != bool(args.tls_key):
        parser.error("--tls-cert 与 --tls-key 必须同时提供")
    try:
        server = build_server(
            args.host,
            args.port,
            args.catalog,
            args.artifact_root,
            args.token,
            args.public_base_url,
        )
    except CatalogError as exc:
        parser.error(str(exc))
    if args.tls_cert:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(args.tls_cert, args.tls_key)
        server.socket = context.wrap_socket(server.socket, server_side=True)
    scheme = "https" if args.tls_cert else "http"
    public_url = args.public_base_url or f"{scheme}://{args.host}:{args.port}"
    print(f"ota_release listening={public_url} manifest=/v1/ota/manifest?channel=stable")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("ota_release stopping")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
