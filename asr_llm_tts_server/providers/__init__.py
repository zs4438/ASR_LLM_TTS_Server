"""云端 ASR/TTS/LLM 客户端构造入口。"""

from __future__ import annotations

from ..config import ServerConfig
from .baidu import BaiduClient
from .llm import LLMClient


def build_asr_client(config: ServerConfig) -> BaiduClient:
    """功能说明：构造固定的百度 ASR 客户端。"""

    return BaiduClient(config)


def build_tts_client(config: ServerConfig) -> BaiduClient:
    """功能说明：构造固定的百度 TTS 客户端。"""

    return BaiduClient(config)


def build_llm_client(config: ServerConfig) -> LLMClient:
    """功能说明：按 `LLM_PROVIDER` 构造当前启用的大模型客户端。"""

    return LLMClient(config)
