# ASR_LLM_TTS_Server 架构说明

> 本文档说明语音服务器的整体架构、与 ESP32 端的对接方式、动作路由（天气/音乐/闲聊）与可打断机制。  
> 适用版本：LLM 工具路由 + 本地记忆 + 天气/音乐工具的当前版本（2026-09）。  
> 语音链路固定百度 ASR/TTS；LLM 支持 ark（豆包）/qwen/deepseek 三选一。

---

## 1. 系统总体架构

```
┌──────────┐  UART1 921600   ┌─────────────┐  WebSocket    ┌─────────────────────┐   HTTPS   ┌──────────────────┐
│ CI130X   │ ◄════════════►  │  ESP32-S3   │ ◄═══════════► │  ASR_LLM_TTS_Server  │ ────────► │  云端服务          │
│ 语音芯片  │  自定义帧协议     │  主控        │  dialogue 协议 │  (本服务器)           │           │ 百度ASR/TTS·LLM·和风│
│ 采集/播放 │                 │             │               │                     │           └──────────────────┘
└──────────┘                 └─────────────┘               └─────────────────────┘
```

**三方职责**：

| 角色              | 职责                                     | 与服务器的关系                                         |
| --------------- | -------------------------------------- | ----------------------------------------------- |
| **CI130X 芯片**   | 麦克风采集、唤醒词、VAD、本地命令词、音频播放               | 通过串口把采集音频（PCM 或 speex）交给 ESP32，播放 ESP32 回传的 MP3 |
| **ESP32-S3 主控** | 网络连接、协议转换、播放调度、发送 `dialogue.cancel` 打断 | **服务器唯一的客户端**：上传音频、接收 MP3 流与控制帧                 |
| **本服务器**        | ASR / 动作路由 / LLM / TTS 编排，流式转发，对话记忆    | 被动提供服务，与云端服务交互                                  |
| **云端服务**        | 百度 ASR、LLM（豆包/千问/DeepSeek）、百度 TTS、和风天气 | 服务器主动调用                                         |

---


## 2. 服务器进程内架构

```
ASR_LLM_TTS_Server（纯 Python 标准库；imageio-ffmpeg 仅 speex 解码/音乐归一化需要）
│
├── server.py ──────────── HTTP + WebSocket 服务入口（ThreadingHTTPServer，每连接一线程）
│     ├── /v1/dialogue/ws              ← ESP32 主链路（WS 流式对话 + 打断 + 设备上线）
│     ├── /v1/dialogue、/v1/dialogue/audio、/v1/dialogue/audio-stream  ← HTTP 备选
│     ├── /v1/asr、/v1/llm、/v1/tts、/v1/tts/stream        ← 单环节测试接口
│     ├── /v1/text-dialogue、/v1/text-dialogue/audio-stream ← 文本入口（跳过 ASR）
│     ├── /v1/music/list、/v1/memory/recent、/v1/memory/reset ← 工具/记忆调试接口
│     └── /v1/config（敏感打码）、/health
│
├── pipeline.py ─────────── 编排流水线：ASR → 动作路由 → LLM/TTS（回调式流式）
│     ├── VoicePipeline(asr, tts, llm, weather, music, memory, tool_router)
│     ├── stream_text_dialogue          ← 四级路由级联（见 §4.1）
│     ├── _LlmSegmenter                 ← LLM 增量按标点切句（弱标点≥18字/上限36字）
│     └── DialogueResult(question, answer_text, audio, timings_ms, action)
│
├── weather.py ──────────── 和风天气工具：关键词意图解析 + 实时/三天预报查询 + 播报文本生成
├── music.py ────────────── 本地曲库工具：MP3 扫描/别名匹配/随机播放/分片下发（可取消）
├── tool_router.py ──────── LLM 意图路由：关键词未命中时让 LLM 输出严格 JSON 选择工具
├── memory.py ───────────── SQLite 对话记忆：按设备 SN 存轮次、注入上下文、可重置
│
├── providers/
│     ├── baidu.py   BaiduClient            ← 百度 ASR + 百度 TTS（固定语音链路）
│     ├── llm.py     LLMClient              ← OpenAI 兼容 LLM（ark/qwen/deepseek 三选一）
│     └── __init__.py build_asr_client / build_tts_client / build_llm_client
│
├── speex_decoder.py ─────── CI 格式 speex 帧流 → 16k PCM（ffmpeg 解码）
├── config.py ────────────── .env 配置加载（ServerConfig，约 30 项，全部有默认值）
├── errors.py ────────────── ConfigError / ProviderError / CancelledError / VoiceServerError
└── http_utils.py ────────── HTTP 请求工具（json_request / http_request）
```

**依赖方向单向**：`server → pipeline → 工具层(weather/music/tool_router/memory) → providers → config/errors/http_utils`。`tool_router` 复用 pipeline 同一个 `LLMClient` 实例。

---

## 3. 与 ESP32 的对接（WebSocket 主链路）

### 3.1 连接

- 地址：`ws://<服务器IP>:8001/v1/dialogue/ws`
- ESP32 端实现：`ESP32-S3_Master/components/BSP/VOICE_SERVER/voice_server_client.c`
- 服务器实现：`server.py` 的 `_handle_dialogue_websocket`（手写 WS 握手 + 帧收发）
- **连接复用**：一轮对话成功保留连接，下轮复用；失败/断开/超时销毁，下轮重新建连


### 3.2 一轮对话的协议时序

```
ESP32 ──────────────────────────────► 服务器
 {"type":"device.online","device_sn":...}  ──►  ← 连接建立后上报（每轮对话帧也带 SN）
 {"type":"dialogue.start","client_req":N,
  "device_sn":...,"audio_format":"speex"}  ──►  ← pcm 或 speex
 [二进制帧] 音频数据（可多片）              ──►
 {"type":"dialogue.end"}                   ──►
 {"type":"dialogue.cancel","reason":...}   ──►  ← 打断（任意时刻，见 §3.3）
                                                │ 服务器 worker 线程处理：
                                                │  ① speex → 16k PCM（如需要）
                                                │  ② 百度 ASR → 文本
                                                │  ③ 动作路由（音乐/天气/闲聊，见 §4.1）
                                                │  ④ TTS/本地 MP3 逐片下发
◄── {"type":"device.online.ack","req":...}
◄── {"type":"dialogue.ready","req":...,"client_req":...}   （收到 start 后立即回）
◄── {"type":"dialogue.action","action":"music",...}        ← 仅音乐命中时，首包前发送
◄── [二进制帧] MP3 音频分片（逐句/逐片下发）
◄── {"type":"dialogue.done","action":...,"first_chunk_ms":...,"total_ms":...}
（取消回 {"type":"dialogue.cancelled",...}；出错回 {"type":"dialogue.error",...}）
```

- 每轮服务器生成 6 位 `req`，与 ESP32 的 `client_req` 在日志与回执中成对出现，便于对齐。
- `dialogue.done` 新增 `action` 字段：`chat` / `weather` / `music` / `music_not_found` / `weather_error`。

### 3.3 可打断（barge-in）机制

- ESP32 发 `dialogue.cancel` → 服务器置位本轮 `cancel_event`，回 `dialogue.cancelled`，主线程继续收帧。
- `cancel_event` 在流水线各阶段都有检查点：LLM 流式循环、TTS 分片循环、音乐分片循环、WS 发送锁等待——任一处命中即抛 `CancelledError`，worker 记 `stage=upstream_cancelled` 后结束，不再回 `dialogue.done`。
- **线程模型**：`dialogue.end` 后由 `_start_ws_worker` 启动独立 worker 线程跑完整流水线，WS 主线程保持收帧才能响应 cancel。发送方向用 `send_lock` 互斥（`_ws_send_frame`，等待锁期间每 50ms 检查一次取消）。

### 3.4 speex 自动解码

CI 端配置 `AIOT_AUDIO_COMPRESS_TYPE=1`（speex）时，上传数据是 **43 字节/帧**（`[1字节长度头][42字节 speex 帧]`，20ms/帧，16k wideband，quality=5）。

服务器 `speex_decoder.decode_ci_speex_to_pcm()`：

```
CI 帧流 → 解析帧（校验长度头）→ 构造 Ogg Speex（硬编码 Speex 头 + 帧）
       → ffmpeg 解码 → 16k/16bit/mono PCM
```

- 耗时约 **15ms**（ffmpeg 路径缓存后）
- `audio_format=pcm` 时直传不解码（兼容旧固件/未压缩模式）

### 3.5 下行播放（MP3 流）

- 服务器把 TTS/音乐 MP3 按 WS 二进制帧逐片下发，不做播放节流（ESP32 端按 CI 的 `PLAY_DATA_GET` 一问一答转发）
- **音乐播放**：服务器在首包前发 `dialogue.action`（含歌名与 `timeout_hint_ms=600000`），ESP32 收到后把本轮播放超时放宽到 10 分钟；普通聊天/天气仍用短超时
- **CI 播放器格式兼容性**：CI 的 `mp3_decoder_api.c` 按 `ci_mp3_header_t` 解析 MP3 头。百度 TTS 输出 64kbps CBR 16k MP3 可正常播放；**本地曲库歌曲必须先用 `tools/normalize_music.py` 归一化**（16k/单声道/CBR/无 ID3/开头补 300ms 静音/修正首帧 4 字节），否则 CI 报 `bad frame` 停止拉流

### 3.6 HTTP 备选链路

ESP32 的 HTTP 模式（`VOICE_SERVER_TRANSPORT_HTTP`）走 `POST /v1/dialogue/audio-stream`，同样支持 speex：

- 请求头 `X-Voice-Audio-Format: speex` 或 `Content-Type: audio/speex` → 服务器先解码再走流水线
- 响应为 chunked MP3 流

---

## 4. 云端 AI 链路（响应流程）

### 4.1 动作路由级联（`pipeline.stream_text_dialogue`）

ASR 得到文本后按**四级短路级联**路由，命中即停：

```
ASR 文本
  │
  ├─ ① 音乐关键词命中？ ──是──► 本地曲库选歌 → 直接下发 MP3 分片（不走 LLM/TTS）
  │                            找不到歌 → TTS 播报"我这里还没有《xx》这首歌"
  ├─ ② 天气关键词命中？ ──是──► 和风查询 →（可选 LLM 润色）→ TTS 播报
  │                            查询失败 → TTS 播报兜底话术
  ├─ ③ LLM 工具路由 ≥阈值？─是─► 按 LLM 返回的 action 执行天气/音乐（同上两条路径）
  │                            （严格 JSON：action/confidence/city/song_query...）
  └─ ④ 普通闲聊 ────────────► 注入记忆上下文 → LLM 流式 → 分句 → 逐句 TTS → 存记忆
```

- ①② 的关键词表分别在 `music.py`（触发词/随机词/剔除词）和 `weather.py`（强意图词/现象词/上下文词）顶部常量，直接改代码（需重启）。
- ③ 的行为由 `tool_router.py` 的系统提示词与 `.env` 双阈值控制（`TOOL_ROUTER_MIN_CONFIDENCE=0.65`、`TOOL_ROUTER_MUSIC_MIN_CONFIDENCE=0.75`）。
- 天气/音乐命中**不写入记忆**；只有闲聊路径的完整问答会存。

### 4.2 全链路数据流（闲聊路径）

```
PCM(16k/mono) ──► 百度 ASR ──► 文本
                     │            │
                     │            ▼
                     │      记忆上下文注入（最近 N 轮，默认 4）
                     │            ▼
                     │      LLM 流式（SSE 增量）
                     │            ▼
                     │    _LlmSegmenter 按标点分句
                     │            ▼
                     └──► 百度 TTS（HTTP 整句合成）──► MP3 分片 ──► 下发 ──► 存记忆
```

### 4.3 百度 ASR（`BaiduClient.recognize_pcm`）

1. OAuth 换取 `access_token`（本地缓存，提前刷新）
2. `POST vop.baidu.com/server_api`，body 为裸 PCM，`dev_pid=1537`（普通话 16k）
3. 解析 `result[0]` 返回识别文本

### 4.4 LLM（`LLMClient`）

- OpenAI 兼容 chat/completions；`ask()` 支持自定义 `system_prompt`/`context_messages`/`timeout`（工具路由与天气润色复用），`stream_deltas()` 支持上下文注入与取消，总时长 120s 保护
- 三家切换：`LLM_PROVIDER=ark|qwen|deepseek`（.env）
  - ark：火山方舟，`thinking` 按 `ARK_THINKING_TYPE`（默认 disabled）
  - qwen：DashScope 兼容模式，`enable_thinking=false`
  - deepseek：官方接口，`thinking=disabled` + 独立 `DEEPSEEK_MAX_TOKENS`
- 流式解析容错：`choices` 为空的 chunk（厂商 usage 帧）直接跳过；`finish_reason` 打日志

### 4.5 分句器（`_LlmSegmenter`）

把 LLM 增量文本切成适合 TTS 的短句，降低首包延迟：

- 强标点（`。！？!?`）立即切
- 弱标点（`，,；;、`）累积满 **18** 字符才切
- 最长 **36** 字符强制切

### 4.6 百度 TTS（`BaiduClient.stream_synthesize_mp3`）

- **当前为 HTTP 整句合成（伪流式）**：一次 POST 合成完整 MP3（60s 超时），拿到结果后按 **4096 字节**切块 yield。句子内部没有流式首包，**首包时延包含第一句的完整合成时间**；句与句之间仍是流水线并行
- 空文本不合成；`cancel_event` 已置位时不合成/中途停止
- 恢复真流式（WSS）是压首包延迟的候选优化项

### 4.7 天气工具（`weather.py`）

- 数据源：和风天气 `/v7/weather/now`（实时）与 `/v7/weather/3d`（三天预报），`X-QW-Api-Key` 鉴权
- 城市解析：内置 20 城常用 LocationID 表（`DEFAULT_CITY_LOCATION_IDS`，可用 `QWEATHER_CITY_IDS` 追加），未命中且 `QWEATHER_GEO_ENABLE=1` 时走 GeoAPI `/geo/v2/city/lookup` 并缓存
- 意图识别：关键词规则（强意图词直接命中；"晴天/阴天/多云"等现象词需搭配疑问/时间/城市，防歌名误触发）；LLM 路由路径由 `WeatherIntent(city, day_offset, forecast)` 直接构造
- 输出：确定性中文播报文本（含温度/体感/湿度/风力/带伞提醒），`WEATHER_LLM_POLISH=1` 时再由 LLM 润色成口语（润色失败自动回落原始文本）

### 4.8 音乐工具（`music.py`）

- 曲库：扫描 `MUSIC_DIR`（默认 `music/`）下 `*.mp3`；匹配按歌名/文件名/"歌手 - 歌名"拆分别名，归一化后子串匹配取最高分
- 意图：触发词命中（播放/点歌/想听/唱歌…）→ 剔除动作词提取歌名；只剩泛化词或含"随便/随机"→ 随机播放
- 下发：`iter_track_bytes` 按 `MUSIC_CHUNK_BYTES`（默认 4096）分片读取，支持取消

### 4.9 LLM 工具路由（`tool_router.py`）

- 仅在①②关键词未命中时触发（`TOOL_ROUTER_ENABLE=1`）：用独立系统提示词让 LLM **只判断不执行**，输出严格 JSON（容错解析：\`\`\`json 围栏 + 正则兜底）
- 置信度低于阈值则回落闲聊；音乐阈值单独更高（点歌误判代价高）
- 路由命中天气时支持 `city/day_offset/forecast` 参数直达天气工具
- 日志：`tool_router_decision`（采用）/ `tool_router_skip`（含 skip_reason 与原始回答，便于排查误判）

### 4.10 流水线时序（服务器日志可观测）

```
stage=asr_done              ASR 完成
stage=tool_router_decision  LLM 路由采用（或 tool_router_skip）
stage=weather_answer        天气确定性文本（或 weather_tool_answer）
stage=music_selected        曲库命中（或 music_not_found）
stage=memory_loaded         注入记忆轮数（仅闲聊）
stage=llm_first_delta       LLM 第一个增量
stage=first_segment_ready   第一句切出
stage=tts_first_audio       第一段 TTS 音频产出（音乐路径为 music_first_audio）
stage=first_binary_sent     服务器下发第一个 WS 二进制帧（含 first_chunk_ms）
stage=memory_stored         问答写入记忆（仅闲聊）
stage=stream_done           一轮完成（含各阶段 timings_ms）
stage=upstream_cancelled    被客户端打断取消
```

---

## 5. 并发与线程模型

### 实时提供方会话模型

- `REALTIME_PROVIDER=qwen` 时，`server.py` 创建独立的 `QwenRealtimeSession`；豆包适配器不参与本轮，稳定基线不被改写。
- 上行保持设备协议不变：`dialogue.start(route=realtime)` → 16 kHz PCM 二进制帧 → `dialogue.commit`。
- 千问会话配置固定发送 `turn_detection: null`，由 `dialogue.commit` 映射为 `input_audio_buffer.commit` + `response.create`，不使用 `server_vad`。
- 千问下行 24 kHz PCM 复用 `PcmToCiMp3Bridge`，转换为 16 kHz 单声道 CBR MP3，并保留 CI 首帧兼容修正。
- 取消映射为 `response.cancel` + `session.finish`；提供方完成后仍通过统一的 `dialogue.done` 返回指标。
- 豆包和千问使用独立 SQLite 路径。千问历史通过 `session.update.instructions` 注入，因为普通文本 `conversation.item.create` 不适合作为该模型的首版记忆通道。

- `ThreadingHTTPServer`：**每个连接一个线程**，多台 ESP32 可同时对话互不干扰
- WS 一轮对话：主线程收帧，`dialogue.end` 后另起 **worker 线程**跑流水线（可被 `dialogue.cancel` 打断）；同一连接的帧发送用 `send_lock` 串行化
- `pipeline`/各客户端无共享可变状态（百度 token 缓存除外；天气城市 ID 表、音乐曲库列表启动时构建后只读；SQLite 每次操作独立开关连接）
- 单台设备一轮对话各阶段串行，多台设备间并行；记忆写入按设备 SN 隔离

---

## 6. 配置（.env 关键项）

| 配置                                | 说明                                       |
| --------------------------------- | ---------------------------------------- |
| `VOICE_SERVER_HOST/PORT`          | 监听地址/端口（默认 0.0.0.0:8000，部署用 8001）        |
| `BAIDU_ASR_*` / `BAIDU_TTS_*`     | 百度语音（dev_pid=1537；aue=3 MP3，per=4146 音色） |
| `LLM_PROVIDER`                    | `ark`/`qwen`/`deepseek` 三选一              |
| `ARK_*` / `QWEN_*` / `DEEPSEEK_*` | 各家 LLM 的 URL/Key/Model/参数                |
| `VOICE_SYSTEM_PROMPT`             | LLM 系统提示词（当前为"小梦"人设）                     |
| `MEMORY_ENABLE` 等                 | 对话记忆开关/库路径/上下文轮数(4)/存储轮数(80)             |
| `WEATHER_ENABLE` 等                | 天气开关/默认城市/润色开关/和风 Host·Key/Geo 开关        |
| `MUSIC_ENABLE` 等                  | 音乐开关/曲库目录/分片大小                           |
| `TOOL_ROUTER_ENABLE` 等            | LLM 路由开关/超时(4s)/双置信度阈值(0.65/0.75)        |

- 新功能开关**默认全部关闭**（MEMORY/WEATHER/MUSIC/TOOL_ROUTER_ENABLE=0），按需在 `.env` 打开。
- 修改 `.env` 或代码内关键词/提示词后需重启服务器生效。

---

## 7. 测试与验证

| 工具/命令                                                                            | 用途                       |
| -------------------------------------------------------------------------------- | ------------------------ |
| `tools/ws_speex_client.py --file test_input.ci.speex --format speex --out x.mp3` | 模拟 ESP32 上传 speex，收 MP3  |
| `tools/pcm_to_speex.py --pcm in.pcm --out out.ci.speex --ci`                     | PCM → CI 格式 speex（43B/帧） |
| `tools/demo_client.py --text "你是谁？"`                                             | 文本链路测试（跳过 ASR）           |
| `tools/demo_client.py --text "播放晴天" --out music_reply.mp3`                       | 音乐工具链路                   |
| `tools/normalize_music.py --src music_raw --out music --overwrite`               | 新歌归一化（CI 播放器兼容）          |
| `tools/batch_tts_from_excel.py --xlsx x.xlsx --out-dir out --dry-run`            | Excel 批量 TTS 合成          |
| `POST /v1/memory/recent` / `/v1/memory/reset`                                    | 查看/清空指定设备记忆              |
| `GET /v1/music/list`                                                             | 查看服务器已识别的曲库              |
| 服务器日志（终端 + `logs/server_*.txt` 自动留存）                                             | `stage=*` 阶段耗时与路由决策      |

---

## 8. 常见问题排查

| 现象           | 排查方向                                                                                                                       |
| ------------ | -------------------------------------------------------------------------------------------------------------------------- |
| ESP32 连不上    | 防火墙/安全组 8001 端口；`curl /health`                                                                                             |
| 无声音          | 日志看 `asr_done` → `tts_first_audio` / `music_first_audio` 是否出现；CI 端 `bad frame` → 检查 TTS/音乐 MP3 是否兼容 CI 播放器（音乐需先 normalize） |
| 该放歌却报"没有这首歌" | 看 `music_not_found` 日志里的 `song_query`；ASR 同音字会导致匹配失败，可在文件名加别名                                                              |
| 该查天气却走了闲聊    | 看 `tool_router_skip` 的 `skip_reason`（low_confidence/weather_disabled）；或关键词没命中且 LLM 判了 chat                                 |
| 闲聊变慢         | 确认是否开了 `TOOL_ROUTER_ENABLE`（每条非关键词语句多一次 LLM 调用）；看 `llm_first_delta` 与 `tool_router` 耗时                                     |
| 一轮卡住无响应      | 看是否有 `upstream_cancelled`（客户端打断）；或 LLM/TTS 超时（120s/60s）                                                                    |
| 记忆不生效        | `MEMORY_ENABLE` 是否打开；只有闲聊路径写入记忆（天气/音乐不存）                                                                                   |
| ASR 识别不准     | 检查拾音环境；口语说法可补充到 music/weather 关键词表或 tool_router 提示词                                                                        |
| 多设备并发        | 服务器天然支持；瓶颈在云端 API 配额                                                                                                       |

---

## 9. 已知限制与注意事项

1. **音乐关键词优先于天气**：路由顺序是 音乐 → 天气 → LLM 路由，"播放一下今天的天气"这类混合语句会先被音乐分支截获。给音乐加宽泛触发词时需评估冲突。
2. **TTS 为 HTTP 整句合成**：句内无流式，首包时延含第一句完整合成时间（见 §4.6）。
3. **WS 帧无大小上限**：`_ws_recv_frame` 按帧头声明长度读取，音频分片列表也不设上限；对接的 ESP32 固件需保证分片合理，不建议暴露公网。
4. **连续 `dialogue.start` 的窗口**：上一轮 worker 未结束又收到新一轮 start 时，旧 worker 可能仍在下发音频（未先发 cancel 的异常时序）。ESP32 端应保证"先 cancel 或等 done 再开新一轮"。
5. **SQLite 未开 WAL**：极高并发写入时可能偶发 `database is locked`，该轮对话报 worker_error；必要时可开启 WAL 或重试。
6. **记忆仅覆盖闲聊**：天气/音乐应答不进记忆，"那明天呢"这类追问依赖关键词再次命中天气。
7. **`dialogue.action` 为新协议帧**：旧固件会收到未知 type，需确认 ESP32 端对未识别帧的忽略行为。

测试github同步测试
测试github同步测试