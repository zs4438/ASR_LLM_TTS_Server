"""读取 `.env` 并生成服务器运行配置。"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .errors import ConfigError


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _load_env_file(path: Path) -> dict[str, str]:
    """功能说明：读取简单的 KEY=VALUE 配置文件。

    入参含义：`path` 是 `.env` 文件路径。
    返回值说明：返回配置键值表，未找到文件时返回空表。
    使用注意事项：只解析普通单行键值，不执行 shell 语法，避免配置文件影响程序逻辑。
    """

    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _env(values: dict[str, str], key: str, default: str = "") -> str:
    """功能说明：按环境变量优先级读取配置项。"""

    return os.environ.get(key, values.get(key, default))


def _int_env(values: dict[str, str], key: str, default: int) -> int:
    """功能说明：读取整数配置，格式错误时抛出配置异常。"""

    raw = _env(values, key, str(default))
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{key} 必须是整数，当前值: {raw}") from exc


def _float_env(values: dict[str, str], key: str, default: float) -> float:
    """功能说明：读取浮点数配置，格式错误时抛出配置异常。"""

    raw = _env(values, key, str(default))
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigError(f"{key} 必须是数字，当前值: {raw}") from exc


def _bool_env(values: dict[str, str], key: str, default: bool) -> bool:
    """功能说明：读取开关配置，支持 1/true/yes/on。"""

    raw = _env(values, key, "1" if default else "0").strip().lower()
    return raw in ("1", "true", "yes", "on", "enable", "enabled")


def _url_env(values: dict[str, str], key: str, default: str = "") -> str:
    """功能说明：读取服务地址配置，并为裸域名自动补齐 HTTPS 协议头。

    入参含义：`values` 是 `.env` 解析出的键值表，`key` 是配置名，`default` 是默认地址。
    返回值说明：返回去掉末尾斜杠后的 URL；如果用户只填域名或域名加路径，会自动补成 HTTPS URL。
    使用注意事项：仅用于外部 HTTP 服务地址，避免 `urllib` 因缺少 `http://` 或 `https://` 报 unknown url type。
    """

    raw = _env(values, key, default).strip().rstrip("/")
    if raw and "://" not in raw:
        return "https://" + raw
    return raw


@dataclass(frozen=True)
class ServerConfig:
    """功能说明：保存语音服务器所有运行配置。

    入参含义：各字段来自 `.env` 或进程环境变量。
    返回值说明：数据类本身无独立返回值，通过 `load()` 创建实例。
    使用注意事项：敏感字段只在服务器内部使用，对外展示时必须调用 `masked_dict()`。
    """

    host: str
    port: int
    baidu_asr_api_key: str
    baidu_asr_secret_key: str
    baidu_asr_cuid: str
    baidu_asr_rate: int
    baidu_asr_format: str
    baidu_asr_dev_pid: int
    baidu_tts_api_key: str
    baidu_tts_secret_key: str
    baidu_tts_per: int
    baidu_tts_aue: int
    baidu_tts_speed: int
    baidu_tts_pitch: int
    baidu_tts_volume: int
    llm_provider: str
    ark_api_url: str
    ark_api_key: str
    ark_model: str
    ark_thinking_type: str
    ark_max_tokens: int
    ark_temperature: float
    qwen_api_url: str
    qwen_api_key: str
    qwen_model: str
    deepseek_api_url: str
    deepseek_api_key: str
    deepseek_model: str
    deepseek_max_tokens: int
    voice_system_prompt: str
    memory_enable: bool
    memory_db_path: Path
    memory_max_recent_turns: int
    memory_max_stored_turns: int
    weather_enable: bool
    weather_llm_polish: bool
    weather_default_city: str
    qweather_api_host: str
    qweather_api_key: str
    qweather_geo_enable: bool
    qweather_geo_api_host: str
    qweather_city_ids: str
    qweather_timeout_sec: float
    music_enable: bool
    music_dir: Path
    music_chunk_bytes: int
    tool_router_enable: bool
    tool_router_timeout_sec: float
    tool_router_min_confidence: float
    tool_router_music_min_confidence: float
    voice_route: str
    realtime_provider: str
    doubao_realtime_api_key: str
    doubao_realtime_url: str
    doubao_realtime_model: str
    doubao_realtime_voice: str
    doubao_realtime_asr_format: str
    doubao_realtime_tts_format: str
    doubao_realtime_system_prompt: str
    doubao_realtime_memory_enable: bool
    doubao_realtime_memory_db_path: Path
    doubao_realtime_memory_max_recent_turns: int
    doubao_realtime_memory_max_stored_turns: int
    qwen_realtime_api_key: str
    qwen_realtime_url: str
    qwen_realtime_model: str
    qwen_realtime_voice: str
    qwen_realtime_transcription_model: str
    qwen_realtime_system_prompt: str
    qwen_realtime_turn_mode: str
    qwen_realtime_web_search_enable: bool
    qwen_realtime_tail_silence_ms: int
    qwen_realtime_memory_enable: bool
    qwen_realtime_memory_db_path: Path
    qwen_realtime_memory_max_recent_turns: int
    qwen_realtime_memory_max_stored_turns: int
    realtime_connect_timeout_sec: float
    realtime_first_response_timeout_sec: float
    realtime_output_idle_timeout_sec: float  # 首段供应商音频后，持续无音频且未完成时的回收上限。
    realtime_endpoint_no_event_max_sec: float
    realtime_response_done_grace_sec: float
    realtime_endpoint_silence_max_sec: float
    realtime_command_queue_size: int
    realtime_pcm_bridge_queue_size: int
    realtime_pcm_bridge_put_timeout_sec: float
    realtime_pcm_bridge_close_timeout_sec: float
    realtime_output_sample_rate: int
    realtime_mp3_bitrate: str

    @classmethod
    def load(cls, env_path: Path | None = None) -> "ServerConfig":
        """功能说明：从 `.env` 和环境变量加载服务器配置。"""

        values = _load_env_file(env_path or (PROJECT_ROOT / ".env"))
        memory_db = Path(_env(values, "MEMORY_DB_PATH", "data/voice_memory.sqlite3"))
        doubao_realtime_memory_db = Path(_env(values, "DOUBAO_REALTIME_MEMORY_DB_PATH", "data/doubao_realtime_memory.sqlite3"))
        qwen_realtime_memory_db = Path(_env(values, "QWEN_REALTIME_MEMORY_DB_PATH", "data/qwen_realtime_memory.sqlite3"))
        music_dir = Path(_env(values, "MUSIC_DIR", "music"))
        qweather_host = _url_env(values, "QWEATHER_API_HOST")
        return cls(
            host=_env(values, "VOICE_SERVER_HOST", "0.0.0.0"),
            port=_int_env(values, "VOICE_SERVER_PORT", 8000),
            baidu_asr_api_key=_env(values, "BAIDU_ASR_API_KEY"),
            baidu_asr_secret_key=_env(values, "BAIDU_ASR_SECRET_KEY"),
            baidu_asr_cuid=_env(values, "BAIDU_ASR_CUID", "esp32s3-ci1306-server"),
            baidu_asr_rate=_int_env(values, "BAIDU_ASR_RATE", 16000),
            baidu_asr_format=_env(values, "BAIDU_ASR_FORMAT", "pcm"),
            baidu_asr_dev_pid=_int_env(values, "BAIDU_ASR_DEV_PID", 1537),
            baidu_tts_api_key=_env(values, "BAIDU_TTS_API_KEY"),
            baidu_tts_secret_key=_env(values, "BAIDU_TTS_SECRET_KEY"),
            baidu_tts_per=_int_env(values, "BAIDU_TTS_PER", 4146),
            baidu_tts_aue=_int_env(values, "BAIDU_TTS_AUE", 3),
            baidu_tts_speed=_int_env(values, "BAIDU_TTS_SPEED", 6),
            baidu_tts_pitch=_int_env(values, "BAIDU_TTS_PITCH", 5),
            baidu_tts_volume=_int_env(values, "BAIDU_TTS_VOLUME", 5),
            llm_provider=_env(values, "LLM_PROVIDER", "ark").lower(),
            ark_api_url=_env(values, "ARK_API_URL", "https://ark.cn-beijing.volces.com/api/v3/chat/completions"),
            ark_api_key=_env(values, "ARK_API_KEY"),
            ark_model=_env(values, "ARK_MODEL", "doubao-seed-2-0-mini-260428"),
            ark_thinking_type=_env(values, "ARK_THINKING_TYPE", "disabled"),
            ark_max_tokens=_int_env(values, "ARK_MAX_TOKENS", 300),
            ark_temperature=_float_env(values, "ARK_TEMPERATURE", 0.7),
            qwen_api_url=_env(values, "QWEN_API_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"),
            qwen_api_key=_env(values, "QWEN_API_KEY"),
            qwen_model=_env(values, "QWEN_MODEL", "qwen-plus"),
            deepseek_api_url=_env(values, "DEEPSEEK_API_URL", "https://api.deepseek.com/chat/completions"),
            deepseek_api_key=_env(values, "DEEPSEEK_API_KEY"),
            deepseek_model=_env(values, "DEEPSEEK_MODEL", "deepseek-chat"),
            deepseek_max_tokens=_int_env(values, "DEEPSEEK_MAX_TOKENS", 1024),
            voice_system_prompt=_env(
                values,
                "VOICE_SYSTEM_PROMPT",
                "你叫小梦，是深圳市创梦龙科技有限公司研发的智能机器人语音助手。回答要简短、自然、适合语音播报。",
            ),
            memory_enable=_bool_env(values, "MEMORY_ENABLE", False),
            memory_db_path=memory_db if memory_db.is_absolute() else PROJECT_ROOT / memory_db,
            memory_max_recent_turns=_int_env(values, "MEMORY_MAX_RECENT_TURNS", 4),
            memory_max_stored_turns=_int_env(values, "MEMORY_MAX_STORED_TURNS", 80),
            weather_enable=_bool_env(values, "WEATHER_ENABLE", False),
            weather_llm_polish=_bool_env(values, "WEATHER_LLM_POLISH", True),
            weather_default_city=_env(values, "WEATHER_DEFAULT_CITY", "深圳"),
            qweather_api_host=qweather_host,
            qweather_api_key=_env(values, "QWEATHER_API_KEY"),
            qweather_geo_enable=_bool_env(values, "QWEATHER_GEO_ENABLE", False),
            qweather_geo_api_host=_url_env(values, "QWEATHER_GEO_API_HOST", qweather_host),
            qweather_city_ids=_env(values, "QWEATHER_CITY_IDS", ""),
            qweather_timeout_sec=_float_env(values, "QWEATHER_TIMEOUT_SEC", 8.0),
            music_enable=_bool_env(values, "MUSIC_ENABLE", False),
            music_dir=music_dir if music_dir.is_absolute() else PROJECT_ROOT / music_dir,
            music_chunk_bytes=max(512, _int_env(values, "MUSIC_CHUNK_BYTES", 4096)),
            tool_router_enable=_bool_env(values, "TOOL_ROUTER_ENABLE", False),
            tool_router_timeout_sec=_float_env(values, "TOOL_ROUTER_TIMEOUT_SEC", 4.0),
            tool_router_min_confidence=_float_env(values, "TOOL_ROUTER_MIN_CONFIDENCE", 0.65),
            tool_router_music_min_confidence=_float_env(values, "TOOL_ROUTER_MUSIC_MIN_CONFIDENCE", 0.75),
            voice_route=_env(values, "VOICE_ROUTE", "cascade").lower(),
            realtime_provider=_env(values, "REALTIME_PROVIDER", "doubao").lower(),
            doubao_realtime_api_key=_env(values, "DOUBAO_REALTIME_API_KEY"),
            doubao_realtime_url=_url_env(values, "DOUBAO_REALTIME_URL", "wss://openspeech.bytedance.com/api/v3/duplex/realtime/dialogue"),
            doubao_realtime_model=_env(values, "DOUBAO_REALTIME_MODEL", "1.2.6.1"),
            doubao_realtime_voice=_env(values, "DOUBAO_REALTIME_VOICE", "zh_female_xiaohe_jupiter_bigtts"),
            doubao_realtime_asr_format=_env(values, "DOUBAO_REALTIME_ASR_FORMAT", "pcm"),
            doubao_realtime_tts_format=_env(values, "DOUBAO_REALTIME_TTS_FORMAT", "pcm_s16le"),
            doubao_realtime_system_prompt=_env(
                values,
                "DOUBAO_REALTIME_SYSTEM_PROMPT",
                "你的名字叫小梦，是深圳市创梦龙科技有限公司研发的智能机器人端到端实时语音助手。回答要自然口语化，普通问题优先一句话，最多两句话；用户要求背诵、讲故事或完整读出内容时要保证完整；用户要求唱歌、哼唱、来一首时，不要只唱一句，未指定歌名时直接唱一段原创短歌，指定现成歌曲时给很短的代表性片段后用原创内容续唱，总长度通常6到10句或80到160个中文字符；不要使用列表、换行或特殊符号。",
            ),
            doubao_realtime_memory_enable=_bool_env(values, "DOUBAO_REALTIME_MEMORY_ENABLE", False),
            doubao_realtime_memory_db_path=(
                doubao_realtime_memory_db
                if doubao_realtime_memory_db.is_absolute()
                else PROJECT_ROOT / doubao_realtime_memory_db
            ),
            doubao_realtime_memory_max_recent_turns=_int_env(values, "DOUBAO_REALTIME_MEMORY_MAX_RECENT_TURNS", 4),
            doubao_realtime_memory_max_stored_turns=_int_env(values, "DOUBAO_REALTIME_MEMORY_MAX_STORED_TURNS", 80),
            qwen_realtime_api_key=_env(values, "QWEN_REALTIME_API_KEY", _env(values, "QWEN_API_KEY")),
            qwen_realtime_url=_url_env(
                values,
                "QWEN_REALTIME_URL",
                "wss://dashscope.aliyuncs.com/api-ws/v1/realtime?model=qwen3.5-omni-flash-realtime",
            ),
            qwen_realtime_model=_env(values, "QWEN_REALTIME_MODEL", "qwen3.5-omni-flash-realtime"),
            qwen_realtime_voice=_env(values, "QWEN_REALTIME_VOICE", "Tina"),
            qwen_realtime_transcription_model=_env(
                values,
                "QWEN_REALTIME_TRANSCRIPTION_MODEL",
                "gummy-realtime-v1",
            ),
            qwen_realtime_system_prompt=_env(
                values,
                "QWEN_REALTIME_SYSTEM_PROMPT",
                "你的名字叫小梦，是深圳市创梦龙科技有限公司研发的智能机器人端到端实时语音助手。回答要自然口语化，普通问题优先一句话，最多两句话；用户要求背诵、讲故事或完整读出内容时要保证完整；不要使用列表、换行或特殊符号。",
            ),
            qwen_realtime_turn_mode=_env(values, "QWEN_REALTIME_TURN_MODE", "push_to_talk").lower(),
            qwen_realtime_web_search_enable=_bool_env(
                values,
                "QWEN_REALTIME_WEB_SEARCH_ENABLE",
                False,
            ),
            qwen_realtime_tail_silence_ms=max(0, _int_env(values, "QWEN_REALTIME_TAIL_SILENCE_MS", 200)),
            qwen_realtime_memory_enable=_bool_env(values, "QWEN_REALTIME_MEMORY_ENABLE", False),
            qwen_realtime_memory_db_path=(
                qwen_realtime_memory_db
                if qwen_realtime_memory_db.is_absolute()
                else PROJECT_ROOT / qwen_realtime_memory_db
            ),
            qwen_realtime_memory_max_recent_turns=_int_env(values, "QWEN_REALTIME_MEMORY_MAX_RECENT_TURNS", 4),
            qwen_realtime_memory_max_stored_turns=_int_env(values, "QWEN_REALTIME_MEMORY_MAX_STORED_TURNS", 80),
            realtime_connect_timeout_sec=_float_env(values, "REALTIME_CONNECT_TIMEOUT_SEC", 12.0),
            realtime_first_response_timeout_sec=_float_env(values, "REALTIME_FIRST_RESPONSE_TIMEOUT_SEC", 15.0),
            realtime_output_idle_timeout_sec=max(1.0, _float_env(values, "REALTIME_OUTPUT_IDLE_TIMEOUT_SEC", 6.0)),
            realtime_endpoint_no_event_max_sec=max(0.5, _float_env(values, "REALTIME_ENDPOINT_NO_EVENT_MAX_SEC", 2.0)),
            realtime_response_done_grace_sec=max(0.2, _float_env(values, "REALTIME_RESPONSE_DONE_GRACE_SEC", 1.2)),
            realtime_endpoint_silence_max_sec=max(0.5, _float_env(values, "REALTIME_ENDPOINT_SILENCE_MAX_SEC", 5.0)),
            realtime_command_queue_size=max(8, _int_env(values, "REALTIME_COMMAND_QUEUE_SIZE", 512)),
            realtime_pcm_bridge_queue_size=max(8, _int_env(values, "REALTIME_PCM_BRIDGE_QUEUE_SIZE", 512)),
            realtime_pcm_bridge_put_timeout_sec=max(0.0, _float_env(values, "REALTIME_PCM_BRIDGE_PUT_TIMEOUT_SEC", 0.2)),
            realtime_pcm_bridge_close_timeout_sec=max(1.0, _float_env(values, "REALTIME_PCM_BRIDGE_CLOSE_TIMEOUT_SEC", 60.0)),
            realtime_output_sample_rate=_int_env(values, "REALTIME_OUTPUT_SAMPLE_RATE", 24000),
            realtime_mp3_bitrate=_env(values, "REALTIME_MP3_BITRATE", "64k"),
        )

    def masked_dict(self) -> dict[str, object]:
        """功能说明：生成可对外展示的配置摘要，隐藏密钥。"""

        result = dict(self.__dict__)
        for key in list(result):
            if "key" in key or "secret" in key:
                value = str(result[key] or "")
                result[key] = (value[:4] + "***") if value else ""
        result["memory_db_path"] = str(self.memory_db_path)
        result["doubao_realtime_memory_db_path"] = str(self.doubao_realtime_memory_db_path)
        result["qwen_realtime_memory_db_path"] = str(self.qwen_realtime_memory_db_path)
        result["music_dir"] = str(self.music_dir)
        return result

    def require_baidu_asr(self) -> None:
        """功能说明：确认百度 ASR 必要配置存在。"""

        if not self.baidu_asr_api_key or not self.baidu_asr_secret_key:
            raise ConfigError("缺少 BAIDU_ASR_API_KEY 或 BAIDU_ASR_SECRET_KEY")

    def require_baidu_tts(self) -> None:
        """功能说明：确认百度 TTS 必要配置存在。"""

        if not self.baidu_tts_api_key or not self.baidu_tts_secret_key:
            raise ConfigError("缺少 BAIDU_TTS_API_KEY 或 BAIDU_TTS_SECRET_KEY")

    def require_doubao_realtime(self) -> None:
        """功能说明：确认豆包实时双工模式的必要配置存在。"""

        if self.realtime_provider != "doubao":
            raise ConfigError(f"暂不支持实时提供方: {self.realtime_provider}")
        if not self.doubao_realtime_api_key:
            raise ConfigError("缺少 DOUBAO_REALTIME_API_KEY")
        if self.doubao_realtime_asr_format != "pcm":
            raise ConfigError("首版实时路线仅支持 PCM 上行")

    def require_qwen_realtime(self) -> None:
        """功能说明：确认千问实时 PTT 模式的必要配置存在并拒绝 VAD 配置。"""

        if self.realtime_provider != "qwen":
            raise ConfigError(f"当前实时提供方不是千问: {self.realtime_provider}")
        if not self.qwen_realtime_api_key:
            raise ConfigError("缺少 QWEN_REALTIME_API_KEY 或 QWEN_API_KEY")
        if self.qwen_realtime_model != "qwen3.5-omni-flash-realtime":
            raise ConfigError("千问实时路线当前固定使用 qwen3.5-omni-flash-realtime")
        if self.qwen_realtime_turn_mode != "push_to_talk":
            raise ConfigError("千问实时路线仅允许 push_to_talk，禁止 server_vad")

    def require_realtime(self) -> None:
        """功能说明：按 REALTIME_PROVIDER 分派实时模型配置校验。"""

        if self.realtime_provider == "doubao":
            self.require_doubao_realtime()
            return
        if self.realtime_provider == "qwen":
            self.require_qwen_realtime()
            return
        raise ConfigError(f"暂不支持实时提供方: {self.realtime_provider}")
