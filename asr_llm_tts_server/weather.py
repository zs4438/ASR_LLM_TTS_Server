"""和风天气查询与语音播报文本生成。"""

from __future__ import annotations

import datetime as _dt
import re
import urllib.parse
from dataclasses import dataclass

from .config import ServerConfig
from .errors import ConfigError, ProviderError
from .http_utils import json_request


# 功能说明：内置常用城市到和风 Location ID 的映射，GeoAPI 不可用时仍可查询这些城市。
DEFAULT_CITY_LOCATION_IDS = {
    "北京": "101010100",
    "上海": "101020100",
    "广州": "101280101",
    "深圳": "101280601",
    "杭州": "101210101",
    "南京": "101190101",
    "苏州": "101190401",
    "成都": "101270101",
    "重庆": "101040100",
    "武汉": "101200101",
    "长沙": "101250101",
    "衡阳": "101250401",
    "岳阳": "101251001",
    "西安": "101110101",
    "天津": "101030100",
    "青岛": "101120201",
    "厦门": "101230201",
    "东莞": "101281601",
    "佛山": "101280800",
    "珠海": "101280701",
}

# 功能说明：强天气意图关键词，命中后可直接进入天气工具。
_DIRECT_WEATHER_WORDS = (
    "天气",
    "气温",
    "温度",
    "湿度",
    "下雨",
    "下不下雨",
    "会不会下雨",
    "冷不冷",
    "热不热",
    "冷吗",
    "热吗",
    "降温",
    "升温",
    "刮风",
    "风大",
    "要不要带伞",
    "需不需要带伞",
    "带伞",
    "雨伞",
    "穿什么",
    "穿衣",
    "出门穿",
    "晒不晒",
    "会不会晒",
)
# 功能说明：天气现象类关键词，需要结合时间、城市或疑问语气，避免孤立歌名误触发天气。
_CONDITION_WEATHER_WORDS = ("晴天", "晴不晴", "阴天", "多云", "太阳")
# 功能说明：天气上下文词，用于判断“晴天吗”这类没有直接说“天气”的自然问法。
_WEATHER_CONTEXT_WORDS = ("今天", "明天", "后天", "现在", "当前", "外面", "当地", "本地", "这边", "那边")
# 功能说明：城市提取清理规则，移除天气问法后剩下的短文本才作为候选城市。
_CITY_REMOVE_PATTERN = re.compile(
    r"(今天天气|明天天气|后天天气|天气预报|会不会下雨|下不下雨|下雨吗|天气|气温|温度|湿度|"
    r"冷不冷|热不热|冷吗|热吗|降温|升温|刮风|风大不大|风大|带伞|雨伞|穿什么|穿衣|出门穿|"
    r"晒不晒|会不会晒|晴天吗|晴不晴|晴天|阴天吗|阴天|多云吗|多云|太阳|怎么样|如何).*$"
)


@dataclass(frozen=True)
class WeatherIntent:
    """功能说明：保存从用户话术中解析出来的天气意图。"""

    city: str
    day_offset: int
    forecast: bool


class QWeatherClient:
    """功能说明：封装和风天气实时、预报和城市 Geo 查询。

    入参含义：`config` 提供和风 Host、API Key、默认城市和 Geo 开关。
    返回值说明：`answer()` 返回适合语音播报的确定性天气文本。
    使用注意事项：GeoAPI 只在内置表找不到城市时调用，避免每次都多一次网络请求。
    """

    def __init__(self, config: ServerConfig):
        self.config = config
        self.city_ids = dict(DEFAULT_CITY_LOCATION_IDS)
        self.city_ids.update(_parse_city_ids(config.qweather_city_ids))

    def answer(self, text: str) -> str | None:
        """功能说明：若命中天气意图则查询并返回播报文本，未命中返回 None。"""

        intent = parse_weather_intent(text, self.config.weather_default_city)
        if intent is None:
            return None
        return self.answer_intent(intent)

    def answer_intent(self, intent: WeatherIntent) -> str:
        """功能说明：按已经确定的天气意图查询并返回播报文本。"""

        self._require_config()
        location_id = self._resolve_city(intent.city)
        if intent.forecast or intent.day_offset > 0:
            return self._forecast_answer(intent, location_id)
        return self._now_answer(intent, location_id)

    def _require_config(self) -> None:
        """功能说明：检查和风天气必要配置。"""

        if not self.config.qweather_api_host or not self.config.qweather_api_key:
            raise ConfigError("缺少 QWEATHER_API_HOST 或 QWEATHER_API_KEY")

    def _resolve_city(self, city: str) -> str:
        """功能说明：解析城市为和风 Location ID。"""

        if city in self.city_ids:
            return self.city_ids[city]
        if not self.config.qweather_geo_enable:
            raise ProviderError(f"city not found in local map and GeoAPI disabled: {city}")
        host = self.config.qweather_geo_api_host or self.config.qweather_api_host
        query = urllib.parse.urlencode({"location": city, "range": "cn", "number": 3})
        payload = json_request(
            f"{host}/geo/v2/city/lookup?{query}",
            headers={"X-QW-Api-Key": self.config.qweather_api_key},
            timeout=self.config.qweather_timeout_sec,
        )
        if str(payload.get("code")) != "200":
            raise ProviderError(f"和风 GeoAPI 查询失败: {payload}")
        locations = payload.get("location") or []
        if not locations:
            raise ProviderError(f"和风 GeoAPI 找不到城市: {city}")
        location_id = str(locations[0].get("id") or "")
        if not location_id:
            raise ProviderError(f"和风 GeoAPI 返回缺少 Location ID: {payload}")
        self.city_ids[city] = location_id
        return location_id

    def _now_answer(self, intent: WeatherIntent, location_id: str) -> str:
        """功能说明：查询实时天气并格式化为一句话。"""

        payload = json_request(
            f"{self.config.qweather_api_host}/v7/weather/now?location={urllib.parse.quote(location_id)}",
            headers={"X-QW-Api-Key": self.config.qweather_api_key},
            timeout=self.config.qweather_timeout_sec,
        )
        if str(payload.get("code")) != "200":
            raise ProviderError(f"和风实时天气失败: {payload}")
        now = payload.get("now") or {}
        text = now.get("text", "")
        temp = now.get("temp", "")
        feels = now.get("feelsLike", "")
        humidity = now.get("humidity", "")
        wind_dir = now.get("windDir", "")
        wind_scale = now.get("windScale", "")
        parts = [f"{intent.city}现在{text}"]
        if temp:
            parts.append(f"气温{temp}度")
        if feels:
            parts.append(f"体感{feels}度")
        if humidity:
            parts.append(f"湿度{humidity}%")
        if wind_dir or wind_scale:
            parts.append(f"{wind_dir}{wind_scale}级")
        return "，".join(parts) + "。"

    def _forecast_answer(self, intent: WeatherIntent, location_id: str) -> str:
        """功能说明：查询三天天气预报并格式化为一句话。"""

        payload = json_request(
            f"{self.config.qweather_api_host}/v7/weather/3d?location={urllib.parse.quote(location_id)}",
            headers={"X-QW-Api-Key": self.config.qweather_api_key},
            timeout=self.config.qweather_timeout_sec,
        )
        if str(payload.get("code")) != "200":
            raise ProviderError(f"和风天气预报失败: {payload}")
        daily = payload.get("daily") or []
        index = min(max(intent.day_offset, 0), max(len(daily) - 1, 0))
        if not daily:
            raise ProviderError("和风天气预报为空")
        item = daily[index]
        date_label = _day_label(index)
        text_day = item.get("textDay", "")
        text_night = item.get("textNight", "")
        temp_min = item.get("tempMin", "")
        temp_max = item.get("tempMax", "")
        wind = item.get("windDirDay", "") or item.get("windDirNight", "")
        wind_scale = item.get("windScaleDay", "") or item.get("windScaleNight", "")
        weather = text_day if text_day == text_night or not text_night else f"白天{text_day}，夜间{text_night}"
        answer = f"{intent.city}{date_label}{weather}，气温{temp_min}到{temp_max}度"
        if wind or wind_scale:
            answer += f"，{wind}{wind_scale}级"
        hint = _rain_hint(text_day + text_night, item.get("precip", ""))
        return answer + ("。" if not hint else f"，{hint}")


def parse_weather_intent(text: str, default_city: str = "深圳") -> WeatherIntent | None:
    """功能说明：从中文口语中识别天气查询意图。"""

    raw = (text or "").strip()
    if not _has_weather_intent(raw):
        return None
    day_offset = 0
    if "后天" in raw:
        day_offset = 2
    elif "明天" in raw:
        day_offset = 1
    forecast = day_offset > 0 or "预报" in raw or "未来" in raw
    city = _extract_city(raw, default_city)
    return WeatherIntent(city=city, day_offset=day_offset, forecast=forecast)


def _has_weather_intent(text: str) -> bool:
    """功能说明：判断一句口语是否像天气查询，避免只说“晴天”时误触发天气工具。

    入参含义：`text` 是 ASR 识别出的原始中文文本。
    返回值说明：返回 `True` 表示应进入天气工具，返回 `False` 表示交给后续音乐或普通聊天流程。
    使用注意事项：天气现象词需要搭配疑问、时间、地点等上下文，减少歌曲名和普通短词误判。
    """

    if not text:
        return False
    if any(word in text for word in _DIRECT_WEATHER_WORDS):
        return True
    if not any(word in text for word in _CONDITION_WEATHER_WORDS):
        return False
    compact = re.sub(r"[，。！？?、\s]", "", text)
    if compact in _CONDITION_WEATHER_WORDS:
        return False
    has_question_mark = text.endswith(("吗", "呢", "么", "？", "?"))
    has_context = any(word in text for word in _WEATHER_CONTEXT_WORDS)
    has_city = any(city in text for city in DEFAULT_CITY_LOCATION_IDS)
    return has_question_mark or has_context or has_city


def _extract_city(text: str, default_city: str) -> str:
    """功能说明：提取城市名，优先匹配内置城市，再走口语规则。"""

    for city in sorted(DEFAULT_CITY_LOCATION_IDS, key=len, reverse=True):
        if city in text:
            return city
    cleaned = re.sub(r"[，。！？?、\s]", "", text)
    cleaned = re.sub(r"^(帮我|给我|查询|查一下|查查|看一下|看看|我想知道|请问|一下)+", "", cleaned)
    cleaned = _CITY_REMOVE_PATTERN.sub("", cleaned)
    cleaned = re.sub(r"(今天|明天|后天|未来三天|未来|现在|当前|外面|当地|本地|这边|那边)", "", cleaned)
    cleaned = re.sub(r"^(是不是|是|会不会|会|有没有|有|要不要|需不需要)+", "", cleaned)
    cleaned = re.sub(r"(是不是|是|会不会|会|有没有|有|要不要|需不需要|吗|呢|么)+$", "", cleaned)
    return cleaned[:8] or default_city


def _parse_city_ids(raw: str) -> dict[str, str]:
    """功能说明：解析可选的手工城市 ID 表。"""

    result: dict[str, str] = {}
    for item in (raw or "").split(","):
        if not item.strip():
            continue
        if ":" in item:
            city, location_id = item.split(":", 1)
        elif "=" in item:
            city, location_id = item.split("=", 1)
        else:
            continue
        if city.strip() and location_id.strip():
            result[city.strip()] = location_id.strip()
    return result


def _day_label(offset: int) -> str:
    """功能说明：把日期偏移转换为口语标签。"""

    if offset == 1:
        return "明天"
    if offset == 2:
        return "后天"
    return "今天"


def _rain_hint(weather_text: str, precip: str) -> str:
    """功能说明：根据降雨事实生成短提醒，没有有效提醒时返回空字符串。"""

    if any(word in weather_text for word in ("雨", "雪", "雷", "阵雨")):
        return "出门记得带伞。"
    try:
        if float(precip or "0") > 0:
            return "出门记得带伞。"
    except ValueError:
        pass
    return ""
