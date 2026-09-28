"""LLM 工具路由：在关键词未命中时判断是否调用天气或音乐工具。"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from .config import ServerConfig
from .errors import ProviderError


@dataclass(frozen=True)
class ToolRouteDecision:
    """功能说明：保存 LLM 对用户意图的工具选择结果。"""

    action: str
    confidence: float
    city: str = ""
    day_offset: int = 0
    forecast: bool = False
    song_query: str = ""
    random_play: bool = False
    reason: str = ""


@dataclass(frozen=True)
class ToolRouteAttempt:
    """功能说明：保存一次 LLM 工具路由尝试的完整判断，便于日志说明采用或跳过原因。"""

    decision: ToolRouteDecision | None
    selected: bool
    skip_reason: str = ""
    threshold: float = 0.0
    raw_answer: str = ""


class LLMToolRouter:
    """功能说明：让 LLM 只判断工具，不直接执行查询或播放。"""

    def __init__(self, config: ServerConfig, llm: object):
        self.config = config
        self.llm = llm

    def decide(
        self,
        text: str,
        *,
        weather_enabled: bool,
        music_enabled: bool,
        music_titles: list[str] | None = None,
    ) -> ToolRouteDecision | None:
        """功能说明：返回 chat/weather/music 三选一决策，低置信度返回 None。"""

        attempt = self.decide_with_details(
            text,
            weather_enabled=weather_enabled,
            music_enabled=music_enabled,
            music_titles=music_titles,
        )
        return attempt.decision if attempt.selected else None

    def decide_with_details(
        self,
        text: str,
        *,
        weather_enabled: bool,
        music_enabled: bool,
        music_titles: list[str] | None = None,
    ) -> ToolRouteAttempt:
        """功能说明：返回工具路由的采用结果和跳过原因，供运行日志排查误判。"""

        raw = (text or "").strip()
        if not raw:
            return ToolRouteAttempt(None, False, skip_reason="empty_text")
        if not self.config.tool_router_enable:
            return ToolRouteAttempt(None, False, skip_reason="disabled")
        prompt = self._system_prompt(weather_enabled, music_enabled, music_titles or [])
        answer = str(self.llm.ask(raw, system_prompt=prompt, timeout=self.config.tool_router_timeout_sec)).strip()
        payload = _extract_json_object(answer)
        decision = _decision_from_payload(payload, self.config.weather_default_city)
        if decision.action == "weather" and not weather_enabled:
            return ToolRouteAttempt(decision, False, skip_reason="weather_disabled", raw_answer=answer)
        if decision.action == "music" and not music_enabled:
            return ToolRouteAttempt(decision, False, skip_reason="music_disabled", raw_answer=answer)
        if decision.action == "chat":
            return ToolRouteAttempt(
                decision,
                False,
                skip_reason="chat",
                threshold=self.config.tool_router_min_confidence,
                raw_answer=answer,
            )
        if decision.action == "music":
            threshold = self.config.tool_router_music_min_confidence
        else:
            threshold = self.config.tool_router_min_confidence
        if decision.confidence < threshold:
            return ToolRouteAttempt(
                decision,
                False,
                skip_reason="low_confidence",
                threshold=threshold,
                raw_answer=answer,
            )
        return ToolRouteAttempt(decision, True, threshold=threshold, raw_answer=answer)

    def _system_prompt(self, weather_enabled: bool, music_enabled: bool, music_titles: list[str]) -> str:
        """功能说明：构造严格 JSON 工具选择提示词。"""

        titles = "、".join(music_titles[:20]) if music_titles else "无"
        return (
            "你是语音助手的工具路由器，只判断是否需要调用工具，不要回答用户问题。\n"
            "必须只输出一个 JSON 对象，不要 Markdown，不要解释。\n"
            "JSON 格式固定为："
            '{"action":"chat|weather|music","confidence":0.0,"city":"","day_offset":0,'
            '"forecast":false,"song_query":"","random_play":false,"reason":""}\n'
            f"天气工具可用：{weather_enabled}；默认城市：{self.config.weather_default_city}。"
            "weather 用于实时天气、明后天预报、冷不冷、热不热、晒不晒、要不要带伞、会不会下雨、出门穿衣等问题。"
            "没有城市时 city 填默认城市；今天 day_offset=0，明天=1，后天=2；预报类 forecast=true。\n"
            f"音乐工具可用：{music_enabled}；本地曲库：{titles}。"
            "music 用于用户想听歌、放歌、放音乐、来一首、点歌、换首、首歌听一听、你会唱歌吗、唱歌给我听等播放意图。"
            "用户指定歌名时 song_query 填用户说出的歌名；没有指定歌名但想听歌时 random_play=true。\n"
            "其它闲聊、讲故事、百科、控制不了的请求都选 chat。"
            "如果只是孤立短词、可能同时是歌名和普通词，例如只说“晴天”，不要直接调用工具，选 chat 且 confidence 不超过 0.5。"
            "不要编造天气事实，不要编造曲库里不存在的路径。"
        )


def _extract_json_object(text: str) -> dict:
    """功能说明：从模型输出中解析第一个 JSON 对象。"""

    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?", "", cleaned, flags=re.IGNORECASE).strip()
        cleaned = re.sub(r"```$", "", cleaned).strip()
    try:
        payload = json.loads(cleaned)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
        if not match:
            raise ProviderError(f"工具路由返回不是 JSON: {text[:200]}")
        try:
            payload = json.loads(match.group(0))
        except json.JSONDecodeError as exc:
            raise ProviderError(f"工具路由 JSON 解析失败: {text[:200]}") from exc
    if not isinstance(payload, dict):
        raise ProviderError(f"工具路由返回不是对象: {payload!r}")
    return payload


def _decision_from_payload(payload: dict, default_city: str) -> ToolRouteDecision:
    """功能说明：校验并归一化工具路由 JSON。"""

    action = str(payload.get("action") or "chat").strip().lower()
    if action not in ("chat", "weather", "music"):
        action = "chat"
    try:
        confidence = float(payload.get("confidence") or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = min(max(confidence, 0.0), 1.0)
    try:
        day_offset = int(payload.get("day_offset") or 0)
    except (TypeError, ValueError):
        day_offset = 0
    day_offset = min(max(day_offset, 0), 2)
    city = str(payload.get("city") or "").strip()[:16]
    song_query = str(payload.get("song_query") or "").strip()[:64]
    return ToolRouteDecision(
        action=action,
        confidence=confidence,
        city=city or default_city,
        day_offset=day_offset,
        forecast=_as_bool(payload.get("forecast")) or day_offset > 0,
        song_query=song_query,
        random_play=_as_bool(payload.get("random_play")),
        reason=str(payload.get("reason") or "").strip()[:120],
    )


def _as_bool(value: object) -> bool:
    """功能说明：兼容模型把布尔值写成字符串的情况。"""

    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return False
