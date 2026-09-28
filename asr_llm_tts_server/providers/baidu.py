"""百度 ASR 与 TTS 客户端。"""

from __future__ import annotations

import base64
import json
import time
import urllib.parse

from ..config import ServerConfig
from ..errors import ConfigError, ProviderError
from ..http_utils import http_request, json_request


class BaiduClient:
    """功能说明：封装百度 OAuth、ASR 识别和 TTS 合成。

    入参含义：`config` 提供百度 API Key、Secret Key 和语音参数。
    返回值说明：实例方法返回识别文本或音频字节。
    使用注意事项：token 会缓存在进程内，提前 10 分钟刷新；日志中不要打印完整 token。
    """

    _ASR_OAUTH_URL = "https://aip.baidubce.com/oauth/2.0/token"
    _ASR_URL = "http://vop.baidu.com/server_api"
    _TTS_URL = "https://tsn.baidu.com/text2audio"

    def __init__(self, config: ServerConfig):
        self.config = config
        self._asr_token = ""
        self._asr_expire_at = 0.0
        self._tts_token = ""
        self._tts_expire_at = 0.0

    def recognize_pcm(self, pcm: bytes) -> str:
        """功能说明：把 16k/16bit/mono 裸 PCM 提交给百度 ASR 并返回文本。"""

        self.config.require_baidu_asr()
        if not pcm:
            return ""
        token = self._get_token("asr")
        query = urllib.parse.urlencode(
            {
                "cuid": self.config.baidu_asr_cuid,
                "token": token,
                "dev_pid": self.config.baidu_asr_dev_pid,
            }
        )
        headers = {
            "Content-Type": f"audio/{self.config.baidu_asr_format};rate={self.config.baidu_asr_rate}",
            "Accept": "application/json",
        }
        _, _, body = http_request(f"{self._ASR_URL}?{query}", data=pcm, headers=headers, method="POST", timeout=30)
        try:
            payload = json.loads(body.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise ProviderError(f"百度 ASR 响应不是 JSON: {body[:200]!r}") from exc
        if payload.get("err_no") not in (0, "0", None):
            raise ProviderError(f"百度 ASR 失败: {payload}")
        result = payload.get("result") or []
        return str(result[0]).strip() if result else ""

    def synthesize_mp3(self, text: str) -> bytes:
        """功能说明：把文本一次性合成为 MP3 字节。"""

        return b"".join(self.stream_synthesize_mp3(text))

    def stream_synthesize_mp3(self, text: str, cancel_event: object | None = None):
        """功能说明：生成 TTS 音频分片。

        入参含义：`text` 是待播报文本，`cancel_event` 是可选取消事件。
        返回值说明：迭代返回 MP3 字节块。
        使用注意事项：当前恢复版使用百度 HTTP TTS，一句合成完成后按块下发；对外接口仍保持流式迭代。
        """

        self.config.require_baidu_tts()
        text = (text or "").strip()
        if not text:
            return
        if _is_cancelled(cancel_event):
            return
        token = self._get_token("tts")
        data = urllib.parse.urlencode(
            {
                "tok": token,
                "tex": text,
                "cuid": self.config.baidu_asr_cuid,
                "ctp": 1,
                "lan": "zh",
                "spd": self.config.baidu_tts_speed,
                "pit": self.config.baidu_tts_pitch,
                "vol": self.config.baidu_tts_volume,
                "per": self.config.baidu_tts_per,
                "aue": self.config.baidu_tts_aue,
            }
        ).encode("utf-8")
        headers = {"Content-Type": "application/x-www-form-urlencoded", "Accept": "*/*"}
        _, response_headers, body = http_request(self._TTS_URL, data=data, headers=headers, method="POST", timeout=60)
        content_type = response_headers.get("Content-Type", response_headers.get("content-type", ""))
        if "json" in content_type.lower() or body.startswith(b"{"):
            raise ProviderError(f"百度 TTS 失败: {body[:400].decode('utf-8', errors='replace')}")
        for offset in range(0, len(body), 4096):
            if _is_cancelled(cancel_event):
                print("voice_server trace stage=tts_cancelled")
                return
            yield body[offset : offset + 4096]

    def _get_token(self, usage: str) -> str:
        """功能说明：获取并缓存百度 OAuth token。"""

        now = time.time()
        if usage == "asr":
            if self._asr_token and now < self._asr_expire_at:
                return self._asr_token
            api_key = self.config.baidu_asr_api_key
            secret_key = self.config.baidu_asr_secret_key
        elif usage == "tts":
            if self._tts_token and now < self._tts_expire_at:
                return self._tts_token
            api_key = self.config.baidu_tts_api_key
            secret_key = self.config.baidu_tts_secret_key
        else:
            raise ConfigError(f"未知百度 token 用途: {usage}")

        query = urllib.parse.urlencode(
            {
                "grant_type": "client_credentials",
                "client_id": api_key,
                "client_secret": secret_key,
            }
        )
        payload = json_request(f"{self._ASR_OAUTH_URL}?{query}", method="POST", timeout=20)
        token = str(payload.get("access_token") or "")
        if not token:
            raise ProviderError(f"百度 OAuth 未返回 access_token: {payload}")
        expires_in = int(payload.get("expires_in") or 2592000)
        expire_at = now + max(60, expires_in - 600)
        if usage == "asr":
            self._asr_token = token
            self._asr_expire_at = expire_at
        else:
            self._tts_token = token
            self._tts_expire_at = expire_at
        return token


def baidu_json_asr_request(config: ServerConfig, pcm: bytes) -> dict:
    """功能说明：备用 JSON ASR 请求构造，便于后续调试不同百度接口形态。"""

    token = BaiduClient(config)._get_token("asr")
    speech = base64.b64encode(pcm).decode("ascii")
    return {
        "format": config.baidu_asr_format,
        "rate": config.baidu_asr_rate,
        "channel": 1,
        "cuid": config.baidu_asr_cuid,
        "token": token,
        "dev_pid": config.baidu_asr_dev_pid,
        "len": len(pcm),
        "speech": speech,
    }


def _is_cancelled(cancel_event: object | None) -> bool:
    """功能说明：兼容 `threading.Event` 的取消状态判断。"""

    return bool(cancel_event is not None and getattr(cancel_event, "is_set", lambda: False)())
