"""本地音乐曲库与点歌意图解析。"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass
from pathlib import Path

from .config import ServerConfig


# 功能说明：音乐意图触发词，命中后才会进入本地音乐工具。
_MUSIC_TRIGGER_WORDS = (
    "播放",
    "放音乐",
    "放歌",
    "放一首",
    "放首",
    "听歌",
    "听音乐",
    "来首",
    "来一首",
    "点歌",
    "点一首",
    "换首",
    "唱歌",
    "唱个歌",
    "唱一首",
    "唱首",
    "我想听",
    "想听",
)
# 功能说明：明确表示随机播放的词，不需要再提取歌名。
_MUSIC_RANDOM_WORDS = ("随便", "随机", "任意")
# 功能说明：从口语命令中剔除的动作词和礼貌词，剩余内容才作为候选歌名。
_MUSIC_COMMAND_WORDS = (
    "播放一下",
    "播放",
    "帮我",
    "给我",
    "请",
    "我想听",
    "想听",
    "听一听",
    "听听",
    "听一下",
    "听",
    "放一下",
    "放一首",
    "放首",
    "放音乐",
    "放歌",
    "来一首",
    "来首",
    "点一首",
    "点歌",
    "换首",
    "唱个歌",
    "唱一首",
    "唱首",
    "唱歌",
    "你会",
    "会不会",
    "能不能",
    "能",
    "可以",
)
# 功能说明：只剩这些泛化词时表示随机听歌，而不是点播叫这个名字的歌曲。
_MUSIC_GENERIC_QUERY_WORDS = ("", "歌", "歌曲", "音乐", "一首", "首歌", "一首歌", "歌听")


@dataclass(frozen=True)
class MusicTrack:
    """功能说明：保存一首本地音乐的元数据。"""

    title: str
    path: Path
    size: int
    aliases: tuple[str, ...]


@dataclass(frozen=True)
class MusicIntent:
    """功能说明：保存用户点歌或随机播放意图。"""

    raw_query: str
    song_query: str
    random_play: bool


@dataclass(frozen=True)
class MusicSelection:
    """功能说明：保存点歌匹配结果。"""

    intent: MusicIntent
    track: MusicTrack | None
    message: str


class LocalMusicLibrary:
    """功能说明：扫描服务器本地 MP3 曲库并按语音文本匹配歌曲。

    入参含义：`config` 提供曲库目录、开关和分片大小。
    返回值说明：`select()` 返回匹配结果，`iter_track_bytes()` 迭代返回 MP3 分片。
    使用注意事项：只播放服务器目录内的 `.mp3` 文件，不访问云端音乐平台。
    """

    def __init__(self, config: ServerConfig):
        self.config = config
        self.tracks = self._scan_tracks(config.music_dir)

    def select(self, text: str) -> MusicSelection | None:
        """功能说明：识别音乐意图并选择歌曲，非音乐请求返回 None。"""

        intent = parse_music_intent(text)
        if intent is None:
            return None
        return self.select_intent(intent)

    def select_by_query(self, *, raw_query: str, song_query: str = "", random_play: bool = False) -> MusicSelection:
        """功能说明：按工具路由给出的歌名或随机播放意图选择歌曲。"""

        return self.select_intent(MusicIntent(raw_query=raw_query, song_query=song_query.strip(), random_play=random_play))

    def select_intent(self, intent: MusicIntent) -> MusicSelection:
        """功能说明：按已经确定的音乐意图选择本地歌曲。"""

        if not self.config.music_enable:
            return MusicSelection(intent, None, "音乐播放功能还没有打开。")
        if not self.tracks:
            return MusicSelection(intent, None, "本地曲库里还没有可播放的音乐。")
        if intent.random_play or not intent.song_query:
            track = random.choice(self.tracks)
            return MusicSelection(intent, track, f"随机播放《{track.title}》。")
        track = self._match_track(intent.song_query)
        if track:
            return MusicSelection(intent, track, f"播放《{track.title}》。")
        return MusicSelection(intent, None, f"我这里还没有《{intent.song_query}》这首歌。")

    def list_tracks(self) -> list[dict[str, object]]:
        """功能说明：返回曲库列表，供 HTTP 调试接口查看。"""

        return [{"title": item.title, "file": item.path.name, "bytes": item.size} for item in self.tracks]

    def iter_track_bytes(self, track: MusicTrack, cancel_event: object | None = None):
        """功能说明：按配置分片读取 MP3，供 WebSocket/HTTP 直接下发。"""

        chunk_size = max(512, self.config.music_chunk_bytes)
        with track.path.open("rb") as file:
            while True:
                if _is_cancelled(cancel_event):
                    return
                chunk = file.read(chunk_size)
                if not chunk:
                    return
                yield chunk

    def _match_track(self, query: str) -> MusicTrack | None:
        """功能说明：按歌名、文件名和别名做轻量匹配。"""

        needle = _normalize_text(query)
        if not needle:
            return None
        best: tuple[int, MusicTrack] | None = None
        for track in self.tracks:
            score = 0
            for alias in track.aliases:
                if needle == alias:
                    score = max(score, 100)
                elif needle in alias or alias in needle:
                    score = max(score, min(len(needle), len(alias)))
            if score and (best is None or score > best[0]):
                best = (score, track)
        return best[1] if best else None

    def _scan_tracks(self, music_dir: Path) -> list[MusicTrack]:
        """功能说明：扫描曲库目录下的 MP3 文件。"""

        if not music_dir.is_dir():
            return []
        tracks: list[MusicTrack] = []
        for path in sorted(music_dir.glob("*.mp3")):
            title = path.stem.strip()
            if not title:
                continue
            aliases = {_normalize_text(title), _normalize_text(path.name)}
            if " - " in title:
                singer, song = title.split(" - ", 1)
                aliases.add(_normalize_text(song))
                aliases.add(_normalize_text(singer + song))
            tracks.append(MusicTrack(title=title, path=path, size=path.stat().st_size, aliases=tuple(alias for alias in aliases if alias)))
        return tracks


def parse_music_intent(text: str) -> MusicIntent | None:
    """功能说明：从口语里识别点歌/随机播放意图。"""

    raw = (text or "").strip()
    if not raw:
        return None
    compact = re.sub(r"[，。！？?、\s]", "", raw)
    has_trigger = any(word in compact for word in _MUSIC_TRIGGER_WORDS)
    if not has_trigger:
        return None
    song_query = _extract_song_query(compact)
    random_play = any(word in compact for word in _MUSIC_RANDOM_WORDS) or _is_generic_music_query(song_query)
    if random_play:
        song_query = ""
    return MusicIntent(raw_query=raw, song_query=song_query.strip(), random_play=random_play)


def _extract_song_query(compact: str) -> str:
    """功能说明：从已经去掉标点空格的口语命令里提取候选歌名。

    入参含义：`compact` 是 ASR 文本去掉标点和空白后的短句。
    返回值说明：返回候选歌名；如果只剩“歌”“音乐”等泛化词，后续会转成随机播放。
    使用注意事项：动作词按长度从长到短剔除，避免先删“听”导致“听一听”残留成“一”。
    """

    song_query = compact
    for word in sorted(_MUSIC_COMMAND_WORDS, key=len, reverse=True):
        song_query = song_query.replace(word, "")
    song_query = (
        song_query.replace("这首歌", "")
        .replace("这首", "")
        .replace("歌曲", "歌")
        .replace("音乐", "歌")
        .strip("吗呢嘛吧呀啊了")
    )
    return song_query.strip()


def _is_generic_music_query(song_query: str) -> bool:
    """功能说明：判断候选歌名是否只是泛化听歌表达。

    入参含义：`song_query` 是 `_extract_song_query()` 得到的候选歌名。
    返回值说明：返回 `True` 表示应随机播放，返回 `False` 表示继续按歌名匹配。
    使用注意事项：只判断很短的泛化词，不删除真实歌名中的“歌”字，避免误伤点名歌曲。
    """

    normalized = _normalize_text(song_query)
    return normalized in {_normalize_text(word) for word in _MUSIC_GENERIC_QUERY_WORDS}


def _looks_named_request(compact: str) -> bool:
    """功能说明：判断是否像明确点名某首歌。"""

    for trigger in ("播放", "我想听", "想听", "听", "放", "来首", "唱首"):
        if trigger in compact:
            tail = compact.split(trigger, 1)[1]
            tail = tail.replace("一下", "").replace("这首歌", "").replace("歌曲", "").replace("音乐", "")
            return bool(tail)
    return False


def _normalize_text(value: str) -> str:
    """功能说明：归一化歌名文本，降低中英文符号和空格影响。"""

    return re.sub(r"[\s\-_.·,，。！？?、（）()《》“”\"']", "", value or "").lower()


def _is_cancelled(cancel_event: object | None) -> bool:
    """功能说明：兼容 `threading.Event` 的取消状态判断。"""

    return bool(cancel_event is not None and getattr(cancel_event, "is_set", lambda: False)())
