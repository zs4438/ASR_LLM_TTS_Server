"""OpenAI 兼容大模型客户端。"""

from __future__ import annotations

import json
import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

from ..config import ServerConfig
from ..errors import ConfigError, ProviderError


@dataclass(frozen=True)
class LLMProviderSettings:
    """功能说明：保存当前 LLM 供应商请求参数。"""

    name: str
    api_url: str
    api_key: str
    model: str
    max_tokens: int
    temperature: float


class LLMClient:
    """功能说明：封装 OpenAI 兼容 chat/completions 调用。

    入参含义：`config` 决定使用 ark、qwen 或 deepseek。
    返回值说明：`ask()` 返回完整文本，`stream_deltas()` 逐段返回增量文本。
    使用注意事项：天气润色和普通对话共用同一个客户端，但提示词由 pipeline 控制。
    """

    def __init__(self, config: ServerConfig):
        self.config = config
        self.settings = self._settings_from_config(config)

    def ask(
        self,
        question: str,
        context_messages: list[dict] | None = None,
        system_prompt: str | None = None,
        timeout: float = 90,
    ) -> str:
        """功能说明：非流式请求大模型，返回完整回答。"""

        payload = self._build_payload(question, context_messages, system_prompt, stream=False)
        data = self._request_json(payload, timeout=timeout)
        try:
            return str(data["choices"][0]["message"]["content"]).strip()
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderError(f"LLM 响应缺少 message.content: {data}") from exc

    def stream_deltas(
        self,
        question: str,
        context_messages: list[dict] | None = None,
        cancel_event: object | None = None,
        system_prompt: str | None = None,
    ):
        """功能说明：流式请求大模型，逐块返回文本增量。"""

        payload = self._build_payload(question, context_messages, system_prompt, stream=True)
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            self.settings.api_url,
            data=body,
            headers={
                "Authorization": f"Bearer {self.settings.api_key}",
                "Content-Type": "application/json; charset=utf-8",
                "Accept": "text/event-stream",
            },
            method="POST",
        )
        start = time.perf_counter()
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                for raw_line in response:
                    if _is_cancelled(cancel_event):
                        print("voice_server trace stage=llm_cancelled")
                        return
                    if time.perf_counter() - start > 120:
                        raise ProviderError("LLM 流式响应超过 120 秒")
                    line = raw_line.decode("utf-8", errors="replace").strip()
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        return
                    delta = self._delta_from_sse(data)
                    if delta:
                        yield delta
        except (urllib.error.URLError, socket.timeout) as exc:
            raise ProviderError(f"LLM 流式请求失败: {exc}") from exc

    def _build_payload(
        self,
        question: str,
        context_messages: list[dict] | None,
        system_prompt: str | None,
        *,
        stream: bool,
    ) -> dict:
        """功能说明：构造 OpenAI 兼容请求体。"""

        if not self.settings.api_key:
            raise ConfigError(f"缺少 {self.settings.name} API Key")
        messages = [{"role": "system", "content": system_prompt or self.config.voice_system_prompt}]
        messages.extend(context_messages or [])
        messages.append({"role": "user", "content": question})
        payload: dict[str, object] = {
            "model": self.settings.model,
            "messages": messages,
            "stream": stream,
            "temperature": self.settings.temperature,
            "max_tokens": self.settings.max_tokens,
        }
        if self.settings.name == "ark":
            payload["thinking"] = {"type": self.config.ark_thinking_type}
        elif self.settings.name == "qwen":
            payload["enable_thinking"] = False
        elif self.settings.name == "deepseek":
            payload["thinking"] = {"type": "disabled"}
        return payload

    def _request_json(self, payload: dict, timeout: float) -> dict:
        """功能说明：发送非流式 JSON 请求并解析响应。"""

        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            self.settings.api_url,
            data=body,
            headers={
                "Authorization": f"Bearer {self.settings.api_key}",
                "Content-Type": "application/json; charset=utf-8",
                "Accept": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
            raise ProviderError(f"LLM HTTP {exc.code}: {detail}") from exc
        except (urllib.error.URLError, json.JSONDecodeError) as exc:
            raise ProviderError(f"LLM 请求失败: {exc}") from exc

    def _delta_from_sse(self, data: str) -> str:
        """功能说明：从 OpenAI 兼容 SSE 帧里取出 delta.content。"""

        try:
            payload = json.loads(data)
        except json.JSONDecodeError:
            return ""
        choices = payload.get("choices") or []
        if not choices:
            return ""
        finish = choices[0].get("finish_reason")
        if finish:
            print(f"voice_server trace stage=llm_finish_reason reason={finish}")
        delta = choices[0].get("delta") or {}
        return str(delta.get("content") or "")

    @staticmethod
    def _settings_from_config(config: ServerConfig) -> LLMProviderSettings:
        """功能说明：根据 `LLM_PROVIDER` 选择供应商参数。"""

        if config.llm_provider == "qwen":
            return LLMProviderSettings("qwen", config.qwen_api_url, config.qwen_api_key, config.qwen_model, config.ark_max_tokens, config.ark_temperature)
        if config.llm_provider == "deepseek":
            return LLMProviderSettings("deepseek", config.deepseek_api_url, config.deepseek_api_key, config.deepseek_model, config.deepseek_max_tokens, config.ark_temperature)
        return LLMProviderSettings("ark", config.ark_api_url, config.ark_api_key, config.ark_model, config.ark_max_tokens, config.ark_temperature)


def _is_cancelled(cancel_event: object | None) -> bool:
    """功能说明：兼容 `threading.Event` 的取消状态判断。"""

    return bool(cancel_event is not None and getattr(cancel_event, "is_set", lambda: False)())
