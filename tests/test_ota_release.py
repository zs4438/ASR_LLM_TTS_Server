"""独立 OTA 发布服务的本地协议测试。"""

from __future__ import annotations

import hashlib
import json
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from ota_release.publish import publish_release
from ota_release.service import build_server


class OtaReleaseServiceTest(unittest.TestCase):
    """验证发布工具、manifest、Range、ETag 与可选鉴权。"""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.catalog = self.root / "catalog.json"
        self.artifact_root = self.root / "artifacts"
        source = self.root / "Wifi_Test.bin"
        source.write_bytes(b"voice-ota-release-test-artifact")
        self.artifact = publish_release(
            source=source,
            target="esp32",
            version="1.2.3",
            channel="stable",
            catalog_path=self.catalog,
            artifact_root=self.artifact_root,
            model="esp32-s3-master",
        )
        self.server = build_server("127.0.0.1", 0, self.catalog, self.artifact_root)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        host, port = self.server.server_address[:2]
        self.base_url = f"http://{host}:{port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temporary.cleanup()

    def _get(self, path: str, headers: dict[str, str] | None = None):
        request = Request(f"{self.base_url}{path}", headers=headers or {})
        return urlopen(request, timeout=2)

    def test_manifest_uses_published_hash_and_fixed_artifact_route(self) -> None:
        headers = {"X-Device-SN": "VI-ESP32S3-68EE8FC60BB0"}
        with self._get("/v1/ota/manifest?channel=stable", headers) as response:
            manifest = json.loads(response.read().decode("utf-8"))
        esp32 = manifest["esp32"]
        self.assertEqual(manifest["channel"], "stable")
        self.assertEqual(esp32["version"], "1.2.3")
        self.assertEqual(esp32["size"], len(b"voice-ota-release-test-artifact"))
        self.assertEqual(
            esp32["sha256"],
            hashlib.sha256(b"voice-ota-release-test-artifact").hexdigest(),
        )
        self.assertEqual(esp32["url"], f"{self.base_url}/v1/ota/artifacts/esp32/1.2.3")

        with self._get("/manifest.json", headers) as response:
            legacy_manifest = json.loads(response.read().decode("utf-8"))
        self.assertEqual(legacy_manifest["esp32"], esp32)

    def test_artifact_supports_range_and_etag(self) -> None:
        headers = {"Range": "bytes=6-8", "X-Device-SN": "VI-ESP32S3-68EE8FC60BB0"}
        with self._get("/v1/ota/artifacts/esp32/1.2.3", headers) as response:
            self.assertEqual(response.status, 206)
            self.assertEqual(response.headers["Content-Range"], "bytes 6-8/31")
            self.assertEqual(response.headers["Accept-Ranges"], "bytes")
            self.assertEqual(response.read(), b"ota")
            etag = response.headers["ETag"]
        request = Request(f"{self.base_url}/v1/ota/artifacts/esp32/1.2.3", headers={"If-None-Match": etag})
        with self.assertRaises(HTTPError) as context:
            urlopen(request, timeout=2)
        self.assertEqual(context.exception.code, 304)
        context.exception.close()

    def test_bearer_token_can_protect_manifest_and_artifact_routes(self) -> None:
        self.server.token = "test-token"  # type: ignore[attr-defined]
        with self.assertRaises(HTTPError) as context:
            self._get("/v1/ota/manifest?channel=stable")
        self.assertEqual(context.exception.code, 401)
        context.exception.close()
        with self._get(
            "/v1/ota/manifest?channel=stable",
            {"Authorization": "Bearer test-token"},
        ) as response:
            self.assertEqual(response.status, 200)


if __name__ == "__main__":
    unittest.main()
