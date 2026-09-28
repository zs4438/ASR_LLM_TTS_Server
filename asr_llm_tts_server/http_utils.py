"""HTTP 调用辅助函数。"""

from __future__ import annotations

import gzip
import json
import urllib.error
import urllib.request

from .errors import ProviderError


def http_request(
    url: str,
    *,
    data: bytes | None = None,
    headers: dict[str, str] | None = None,
    method: str | None = None,
    timeout: float = 30.0,
) -> tuple[int, dict[str, str], bytes]:
    """功能说明：发送 HTTP 请求并返回状态码、响应头和原始响应体。

    入参含义：`url` 是完整地址，`data` 是请求体，`headers` 是请求头，`method` 可显式指定 POST/GET。
    返回值说明：返回 `(status, headers, body)`。
    使用注意事项：云端返回非 2xx 时保留响应体摘要，方便定位鉴权或额度问题。
    """

    request = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read()
            if response.headers.get("Content-Encoding", "").lower() == "gzip":
                body = gzip.decompress(body)
            return response.status, dict(response.headers.items()), body
    except urllib.error.HTTPError as exc:
        body = exc.read()
        if exc.headers.get("Content-Encoding", "").lower() == "gzip":
            body = gzip.decompress(body)
        raise ProviderError(f"HTTP {exc.code}: {body[:300].decode('utf-8', errors='replace')}") from exc
    except urllib.error.URLError as exc:
        raise ProviderError(f"HTTP 请求失败: {exc}") from exc


def json_request(
    url: str,
    *,
    payload: object | None = None,
    headers: dict[str, str] | None = None,
    method: str | None = None,
    timeout: float = 30.0,
) -> dict:
    """功能说明：发送 JSON 请求并解析 JSON 响应。"""

    body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    merged_headers = {"Accept": "application/json"}
    if payload is not None:
        merged_headers["Content-Type"] = "application/json; charset=utf-8"
    if headers:
        merged_headers.update(headers)
    _, _, response_body = http_request(url, data=body, headers=merged_headers, method=method, timeout=timeout)
    try:
        return json.loads(response_body.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise ProviderError(f"JSON 响应解析失败: {response_body[:300]!r}") from exc
