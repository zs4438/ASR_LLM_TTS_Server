"""ASR -> 动作路由 -> LLM -> TTS 的主流水线。"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, Iterable

from .errors import CancelledError
from .memory import ConversationMemory
from .music import LocalMusicLibrary, MusicSelection
from .speex_decoder import decode_ci_speex_to_pcm
from .tool_router import LLMToolRouter, ToolRouteDecision
from .weather import QWeatherClient, WeatherIntent


TraceFn = Callable[[str, dict], None]
AudioFn = Callable[[bytes], None]
ActionFn = Callable[[dict[str, object]], None]


@dataclass
class DialogueResult:
    """功能说明：保存一轮对话结果和耗时。"""

    question: str
    answer_text: str
    audio: bytes = b""
    timings_ms: dict[str, object] = field(default_factory=dict)
    action: str = "chat"


class _LlmSegmenter:
    """功能说明：把 LLM 增量文本切成适合 TTS 的短句。"""

    strong_marks = set("。！？!?")
    weak_marks = set("，,；;、")

    def __init__(self, weak_min_chars: int = 18, max_chars: int = 36):
        self.buffer = ""
        self.weak_min_chars = weak_min_chars
        self.max_chars = max_chars

    def feed(self, delta: str) -> list[str]:
        """功能说明：喂入一段增量文本，返回已经可以合成的句子。"""

        self.buffer += delta
        segments: list[str] = []
        while self.buffer:
            index = self._split_index()
            if index <= 0:
                break
            segment = self.buffer[:index].strip()
            self.buffer = self.buffer[index:].lstrip()
            if segment:
                segments.append(segment)
        return segments

    def flush(self) -> list[str]:
        """功能说明：把剩余文本全部输出为最后一段。"""

        text = self.buffer.strip()
        self.buffer = ""
        return [text] if text else []

    def _split_index(self) -> int:
        """功能说明：寻找当前缓冲区中适合切句的位置。"""

        for index, char in enumerate(self.buffer, start=1):
            if char in self.strong_marks:
                return index
            if char in self.weak_marks and index >= self.weak_min_chars:
                return index
        if len(self.buffer) >= self.max_chars:
            return self.max_chars
        return 0


class VoicePipeline:
    """功能说明：编排语音服务器的一轮请求。

    入参含义：`asr`、`tts`、`llm` 是供应商客户端，可选注入 `weather`、`music`、`memory`、`tool_router`。
    返回值说明：HTTP JSON 路径返回 `DialogueResult`；流式路径通过回调下发 MP3 分片并返回结果摘要。
    使用注意事项：音乐和天气属于确定性工具，命中后不会进入普通闲聊 LLM。
    """

    def __init__(
        self,
        asr: object,
        tts: object,
        llm: object,
        *,
        weather: QWeatherClient | None = None,
        music: LocalMusicLibrary | None = None,
        memory: ConversationMemory | None = None,
        tool_router: LLMToolRouter | None = None,
        memory_recent_turns: int = 4,
    ):
        self.asr = asr
        self.tts = tts
        self.llm = llm
        self.weather = weather
        self.music = music
        self.memory = memory
        self.tool_router = tool_router
        self.memory_recent_turns = max(0, memory_recent_turns)

    def dialogue(self, audio: bytes, *, audio_format: str = "pcm", device_sn: str = "", request_id: str = "") -> DialogueResult:
        """功能说明：非流式音频对话，返回完整 MP3。"""

        pcm = self._decode_audio(audio, audio_format)
        question = self.asr.recognize_pcm(pcm)
        return self.text_dialogue(question, device_sn=device_sn, request_id=request_id)

    def text_dialogue(self, question: str, *, device_sn: str = "", request_id: str = "") -> DialogueResult:
        """功能说明：非流式文本对话，主要供本地调试。"""

        chunks: list[bytes] = []
        result = self.stream_text_dialogue(
            question,
            chunks.append,
            device_sn=device_sn,
            request_id=request_id,
            trace=lambda _stage, _fields: None,
            cancel_event=None,
        )
        result.audio = b"".join(chunks)
        return result

    def stream_audio_dialogue(
        self,
        audio: bytes,
        *,
        audio_format: str,
        send_audio: AudioFn,
        device_sn: str = "",
        request_id: str = "",
        trace: TraceFn | None = None,
        cancel_event: object | None = None,
        notify_action: ActionFn | None = None,
    ) -> DialogueResult:
        """功能说明：流式音频对话，ASR 后按动作路由下发音频。"""

        start = time.perf_counter()
        pcm = self._decode_audio(audio, audio_format)
        if audio_format.lower() == "speex":
            self._trace(trace, "speex_decoded", {"speex_bytes": len(audio), "pcm_bytes": len(pcm)})
        question = self.asr.recognize_pcm(pcm)
        asr_ms = _elapsed_ms(start)
        self._trace(trace, "asr_done", {"asr_ms": asr_ms, "pcm_bytes": len(pcm), "asr_text": question[:120]})
        if _is_cancelled(cancel_event):
            raise CancelledError("cancelled after ASR")
        result = self.stream_text_dialogue(
            question,
            send_audio,
            device_sn=device_sn,
            request_id=request_id,
            trace=trace,
            cancel_event=cancel_event,
            notify_action=notify_action,
            base_timings={"asr": asr_ms},
            start_time=start,
        )
        return result

    def stream_text_dialogue(
        self,
        question: str,
        send_audio: AudioFn,
        *,
        device_sn: str = "",
        request_id: str = "",
        trace: TraceFn | None = None,
        cancel_event: object | None = None,
        notify_action: ActionFn | None = None,
        base_timings: dict[str, object] | None = None,
        start_time: float | None = None,
    ) -> DialogueResult:
        """功能说明：文本入口的流式处理，统一承载音乐、天气和普通对话。"""

        start = start_time or time.perf_counter()
        timings: dict[str, object] = dict(base_timings or {})
        question = (question or "").strip()

        music_selection = self._select_music(question)
        if music_selection is not None:
            return self._stream_music(question, music_selection, send_audio, timings, start, trace, cancel_event, notify_action)

        try:
            weather_answer = self._weather_answer(question, trace)
        except Exception as exc:  # noqa: BLE001
            return self._stream_weather_error(question, exc, send_audio, timings, start, trace, cancel_event)
        if weather_answer is not None:
            answer = self._polish_weather_answer(question, weather_answer, trace)
            timings["llm"] = 0 if answer == weather_answer else "weather_polish"
            audio_bytes, chunks = self._stream_tts(answer, send_audio, trace, cancel_event, start)
            timings.update({"first_audio": _elapsed_ms(start) if audio_bytes else None, "total": _elapsed_ms(start), "segments": chunks})
            self._trace(trace, "stream_done", {"asr_text": question, "answer_chars": len(answer), "answer_preview": answer[:120], "timings_ms": timings})
            return DialogueResult(question=question, answer_text=answer, timings_ms=timings, action="weather")

        tool_decision = self._tool_route(question, trace)
        if tool_decision is not None:
            timings["tool_router"] = tool_decision.action
            timings["tool_router_confidence"] = round(tool_decision.confidence, 2)
            if tool_decision.action == "music":
                routed_music = self._select_music_from_tool(question, tool_decision, trace)
                if routed_music is not None:
                    return self._stream_music(question, routed_music, send_audio, timings, start, trace, cancel_event, notify_action)
            elif tool_decision.action == "weather":
                try:
                    routed_weather = self._weather_answer_from_tool(tool_decision, trace)
                except Exception as exc:  # noqa: BLE001
                    return self._stream_weather_error(question, exc, send_audio, timings, start, trace, cancel_event)
                if routed_weather is not None:
                    answer = self._polish_weather_answer(question, routed_weather, trace)
                    timings["llm"] = 0 if answer == routed_weather else "weather_polish"
                    audio_bytes, chunks = self._stream_tts(answer, send_audio, trace, cancel_event, start)
                    timings.update({"first_audio": _elapsed_ms(start) if audio_bytes else None, "total": _elapsed_ms(start), "segments": chunks})
                    self._trace(trace, "stream_done", {"asr_text": question, "answer_chars": len(answer), "answer_preview": answer[:120], "timings_ms": timings})
                    return DialogueResult(question=question, answer_text=answer, timings_ms=timings, action="weather")

        return self._stream_chat(question, send_audio, device_sn, request_id, timings, start, trace, cancel_event)

    def _stream_music(
        self,
        question: str,
        selection: MusicSelection,
        send_audio: AudioFn,
        timings: dict[str, object],
        start: float,
        trace: TraceFn | None,
        cancel_event: object | None,
        notify_action: ActionFn | None,
    ) -> DialogueResult:
        """功能说明：音乐命中时直接下发本地 MP3，找不到时 TTS 一句提示。"""

        if selection.track is None:
            self._trace(
                trace,
                "music_not_found",
                {
                    "raw_query": selection.intent.raw_query,
                    "song_query": selection.intent.song_query,
                    "answer": selection.message,
                },
            )
            audio_bytes, chunks = self._stream_tts(selection.message, send_audio, trace, cancel_event, start)
            timings.update({"music": "not_found", "first_audio": _elapsed_ms(start) if audio_bytes else None, "total": _elapsed_ms(start), "segments": chunks})
            self._trace(trace, "stream_done", {"asr_text": question, "answer_chars": len(selection.message), "answer_preview": selection.message, "timings_ms": timings})
            return DialogueResult(question=question, answer_text=selection.message, timings_ms=timings, action="music_not_found")

        total = 0
        chunks = 0
        first_audio_ms: int | None = None
        self._trace(trace, "music_selected", {"title": selection.track.title, "bytes": selection.track.size})
        if notify_action is not None:
            notify_action(
                {
                    "action": "music",
                    "title": selection.track.title,
                    "bytes": selection.track.size,
                    "timeout_hint_ms": 600000,
                }
            )
        for chunk in self.music.iter_track_bytes(selection.track, cancel_event=cancel_event):  # type: ignore[union-attr]
            if _is_cancelled(cancel_event):
                raise CancelledError("cancelled during music")
            if first_audio_ms is None:
                first_audio_ms = _elapsed_ms(start)
                self._trace(trace, "music_first_audio", {"first_audio_ms": first_audio_ms, "title": selection.track.title})
            send_audio(chunk)
            total += len(chunk)
            chunks += 1
        timings.update({"music": selection.track.title, "first_audio": first_audio_ms, "total": _elapsed_ms(start), "segments": chunks})
        self._trace(trace, "stream_done", {"asr_text": question, "answer_chars": len(selection.message), "answer_preview": selection.message, "timings_ms": timings})
        return DialogueResult(question=question, answer_text=selection.message, timings_ms=timings, action="music")

    def _stream_chat(
        self,
        question: str,
        send_audio: AudioFn,
        device_sn: str,
        request_id: str,
        timings: dict[str, object],
        start: float,
        trace: TraceFn | None,
        cancel_event: object | None,
    ) -> DialogueResult:
        """功能说明：普通闲聊路径，LLM 增量切句后逐句 TTS。"""

        context_messages = self._memory_context(device_sn, trace)
        segmenter = _LlmSegmenter()
        answer_parts: list[str] = []
        first_delta_recorded = False
        first_segment_recorded = False
        first_audio_recorded = False
        segment_count = 0
        audio_chunks = 0
        audio_bytes = 0
        for delta in self.llm.stream_deltas(question, context_messages=context_messages, cancel_event=cancel_event):
            if _is_cancelled(cancel_event):
                raise CancelledError("cancelled during LLM")
            if delta and not first_delta_recorded:
                timings["llm_first_delta"] = _elapsed_ms(start) - int(timings.get("asr", 0) or 0)
                first_delta_recorded = True
                self._trace(trace, "llm_first_delta", {"delta_ms": timings["llm_first_delta"], "asr_ms": timings.get("asr", 0)})
            answer_parts.append(delta)
            for segment in segmenter.feed(delta):
                if not first_segment_recorded:
                    timings["first_segment"] = _elapsed_ms(start) - int(timings.get("asr", 0) or 0)
                    first_segment_recorded = True
                    self._trace(trace, "first_segment_ready", {"segment_ms": timings["first_segment"], "answer_chars": len(segment)})
                chunk_bytes, chunk_count = self._stream_tts(segment, send_audio, trace, cancel_event, start, first_audio_recorded)
                if chunk_bytes and not first_audio_recorded:
                    first_audio_recorded = True
                    timings["first_audio"] = _elapsed_ms(start)
                    self._trace(trace, "tts_first_audio", {"first_audio_ms": timings["first_audio"], "segment_count": segment_count + 1})
                audio_bytes += chunk_bytes
                audio_chunks += chunk_count
                segment_count += 1
        for segment in segmenter.flush():
            chunk_bytes, chunk_count = self._stream_tts(segment, send_audio, trace, cancel_event, start, first_audio_recorded)
            if chunk_bytes and not first_audio_recorded:
                first_audio_recorded = True
                timings["first_audio"] = _elapsed_ms(start)
                self._trace(trace, "tts_first_audio", {"first_audio_ms": timings["first_audio"], "segment_count": segment_count + 1})
            audio_bytes += chunk_bytes
            audio_chunks += chunk_count
            segment_count += 1
        answer = "".join(answer_parts).strip() or "我刚才没有听清楚，可以再说一遍吗？"
        if not audio_bytes and answer:
            chunk_bytes, chunk_count = self._stream_tts(answer, send_audio, trace, cancel_event, start, first_audio_recorded)
            audio_bytes += chunk_bytes
            audio_chunks += chunk_count
            segment_count += 1 if chunk_count else 0
        timings.update(
            {
                "llm_stream": _elapsed_ms(start) - int(timings.get("asr", 0) or 0),
                "total": _elapsed_ms(start),
                "segments": segment_count,
                "audio_chunks": audio_chunks,
            }
        )
        if self.memory and device_sn and answer:
            self.memory.store_turn(device_sn, question, answer, request_id)
            self._trace(trace, "memory_stored", {"device_sn": device_sn, "request_id": request_id})
        self._trace(trace, "stream_done", {"asr_text": question, "answer_chars": len(answer), "answer_preview": answer[:120], "timings_ms": timings})
        return DialogueResult(question=question, answer_text=answer, timings_ms=timings, action="chat")

    def _stream_tts(
        self,
        text: str,
        send_audio: AudioFn,
        trace: TraceFn | None,
        cancel_event: object | None,
        start: float,
        first_audio_already_sent: bool = False,
    ) -> tuple[int, int]:
        """功能说明：合成一段 TTS 并通过回调下发所有音频块。"""

        total = 0
        chunks = 0
        for chunk in self.tts.stream_synthesize_mp3(text, cancel_event=cancel_event):
            if _is_cancelled(cancel_event):
                raise CancelledError("cancelled during TTS")
            if chunk:
                if not first_audio_already_sent and total == 0:
                    self._trace(trace, "tts_segment_first_audio", {"first_audio_ms": _elapsed_ms(start), "text_chars": len(text)})
                send_audio(chunk)
                total += len(chunk)
                chunks += 1
        return total, chunks

    def _weather_answer(self, question: str, trace: TraceFn | None) -> str | None:
        """功能说明：尝试走天气工具，未命中时返回 None。"""

        if not self.weather:
            return None
        answer = self.weather.answer(question)
        if answer is not None:
            self._trace(trace, "weather_answer", {"answer": answer})
        return answer

    def _stream_weather_error(
        self,
        question: str,
        exc: Exception,
        send_audio: AudioFn,
        timings: dict[str, object],
        start: float,
        trace: TraceFn | None,
        cancel_event: object | None,
    ) -> DialogueResult:
        """功能说明：天气工具异常时生成可播报的兜底回复，避免 WebSocket 对话直接失败。

        入参含义：`question` 是 ASR 文本，`exc` 是天气查询异常，`send_audio` 负责下发 MP3，
        `timings` 和 `start` 用于累计耗时，`trace` 写入服务器日志，`cancel_event` 用于打断检查。
        返回值说明：返回一轮天气失败结果，`action` 固定为 `weather_error`。
        使用注意事项：这里只播报短失败提示，详细异常只写日志，避免把配置或接口细节读给用户听。
        """

        answer = "天气查询暂时失败，请稍后再试。"
        self._trace(trace, "weather_failed", {"asr_text": question, "error": f"{type(exc).__name__}: {str(exc)[:200]}"})
        audio_bytes, chunks = self._stream_tts(answer, send_audio, trace, cancel_event, start)
        timings.update({"weather": "failed", "first_audio": _elapsed_ms(start) if audio_bytes else None, "total": _elapsed_ms(start), "segments": chunks})
        self._trace(trace, "stream_done", {"asr_text": question, "answer_chars": len(answer), "answer_preview": answer, "timings_ms": timings})
        return DialogueResult(question=question, answer_text=answer, timings_ms=timings, action="weather_error")

    def _weather_answer_from_tool(self, decision: ToolRouteDecision, trace: TraceFn | None) -> str | None:
        """功能说明：按 LLM 工具路由结果执行天气查询。"""

        if not self.weather:
            return None
        intent = WeatherIntent(city=decision.city, day_offset=decision.day_offset, forecast=decision.forecast)
        answer = self.weather.answer_intent(intent)
        self._trace(
            trace,
            "weather_tool_answer",
            {
                "city": intent.city,
                "day_offset": intent.day_offset,
                "forecast": intent.forecast,
                "answer": answer,
            },
        )
        return answer

    def _polish_weather_answer(self, question: str, weather_answer: str, trace: TraceFn | None) -> str:
        """功能说明：用当前 LLM 把确定性天气事实润色成自然口语。"""

        config = getattr(self.llm, "config", None)
        if not config or not getattr(config, "weather_llm_polish", False):
            return weather_answer
        prompt = (
            "你把天气事实改写成一句自然中文口语。必须保留城市、天气、温度、风力、降雨提醒等事实，"
            "不要添加天气信息仅供参考、仅供参考之类免责声明，不要编造事实，控制在 60 字以内。"
        )
        question_text = f"用户问：{question}\n天气事实：{weather_answer}"
        try:
            answer = self.llm.ask(question_text, system_prompt=prompt).strip()
        except Exception as exc:  # noqa: BLE001
            self._trace(trace, "weather_polish_failed", {"error": str(exc)[:200]})
            return weather_answer
        if not answer or "仅供参考" in answer or "天气信息仅供参考" in answer:
            return weather_answer
        self._trace(trace, "weather_polished", {"answer": answer})
        return answer

    def _memory_context(self, device_sn: str, trace: TraceFn | None) -> list[dict]:
        """功能说明：读取当前设备最近记忆并转换为 LLM 上下文。"""

        if not self.memory or not device_sn:
            return []
        messages = self.memory.context_messages(device_sn, self.memory_recent_turns)
        self._trace(trace, "memory_loaded", {"device_sn": device_sn, "turns": len(messages) // 2})
        return messages

    def _select_music(self, question: str) -> MusicSelection | None:
        """功能说明：尝试走本地音乐工具，未命中时返回 None。"""

        return self.music.select(question) if self.music else None

    def _select_music_from_tool(self, question: str, decision: ToolRouteDecision, trace: TraceFn | None) -> MusicSelection | None:
        """功能说明：按 LLM 工具路由结果选择本地音乐。"""

        if not self.music:
            return None
        selection = self.music.select_by_query(
            raw_query=question,
            song_query=decision.song_query,
            random_play=decision.random_play or not decision.song_query,
        )
        self._trace(
            trace,
            "music_tool_selected",
            {
                "song_query": decision.song_query,
                "random_play": decision.random_play or not decision.song_query,
                "track": selection.track.title if selection.track else None,
            },
        )
        return selection

    def _tool_route(self, question: str, trace: TraceFn | None) -> ToolRouteDecision | None:
        """功能说明：关键词未命中时让 LLM 判断是否需要天气或音乐工具。"""

        if not self.tool_router:
            return None
        try:
            attempt = self.tool_router.decide_with_details(
                question,
                weather_enabled=self.weather is not None,
                music_enabled=self.music is not None,
                music_titles=[track.title for track in self.music.tracks] if self.music else [],
            )
        except Exception as exc:  # noqa: BLE001
            self._trace(trace, "tool_router_failed", {"error": str(exc)[:200]})
            return None
        if not attempt.selected or attempt.decision is None:
            fields: dict[str, object] = {"asr_text": question, "skip_reason": attempt.skip_reason}
            if attempt.decision is not None:
                fields.update(
                    {
                        "action": attempt.decision.action,
                        "confidence": attempt.decision.confidence,
                        "threshold": attempt.threshold,
                        "city": attempt.decision.city,
                        "day_offset": attempt.decision.day_offset,
                        "forecast": attempt.decision.forecast,
                        "song_query": attempt.decision.song_query,
                        "random_play": attempt.decision.random_play,
                        "reason": attempt.decision.reason,
                    }
                )
            if attempt.raw_answer:
                fields["raw_answer"] = attempt.raw_answer[:200]
            self._trace(trace, "tool_router_skip", fields)
            return None
        decision = attempt.decision
        self._trace(
            trace,
            "tool_router_decision",
            {
                "action": decision.action,
                "confidence": decision.confidence,
                "city": decision.city,
                "day_offset": decision.day_offset,
                "forecast": decision.forecast,
                "song_query": decision.song_query,
                "random_play": decision.random_play,
                "reason": decision.reason,
            },
        )
        return decision

    def _decode_audio(self, audio: bytes, audio_format: str) -> bytes:
        """功能说明：按 ESP32 上报格式转换为 ASR 可用 PCM。"""

        if audio_format.lower() == "speex":
            return decode_ci_speex_to_pcm(audio)
        return audio

    @staticmethod
    def _trace(trace: TraceFn | None, stage: str, fields: dict) -> None:
        """功能说明：统一输出流水线阶段日志。"""

        if trace:
            trace(stage, fields)


def collect_audio(generator: Iterable[bytes]) -> bytes:
    """功能说明：把音频迭代器收集成完整字节串。"""

    return b"".join(generator)


def _elapsed_ms(start: float) -> int:
    """功能说明：计算从起点到当前的毫秒耗时。"""

    return int((time.perf_counter() - start) * 1000)


def _is_cancelled(cancel_event: object | None) -> bool:
    """功能说明：兼容 `threading.Event` 的取消状态判断。"""

    return bool(cancel_event is not None and getattr(cancel_event, "is_set", lambda: False)())
