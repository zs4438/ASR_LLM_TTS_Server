# ASR_LLM_TTS_Server

这是一个纯 Python Demo 服务，用来先把 ESP32 端原来的云端链路搬到服务器侧：

CI/ESP32 上传 16 kHz、16-bit、单声道裸 PCM（或 CI speex 帧流，服务器自动解码）-> 百度 ASR -> LLM（豆包/千问/DeepSeek 可切换）-> 百度 TTS -> 返回 MP3。

## 当前能力

- `GET /health`：检查服务是否启动。
- `GET /v1/config`：查看当前配置，敏感值会打码。
- `POST /v1/asr`：请求体直接传裸 PCM，返回识别文本。
- `POST /v1/llm`：请求体传 `{"question":"..."}`，返回大模型回答。
- `POST /v1/tts`：请求体传 `{"text":"..."}`，返回 MP3 音频。
- `POST /v1/dialogue`：请求体直接传裸 PCM，返回 JSON，包含 ASR 文本、LLM 文本、MP3 base64。
- `POST /v1/dialogue/audio`：请求体直接传裸 PCM，返回 `audio/mpeg`，适合后续 ESP32 直接接音频数据。
- `POST /v1/text-dialogue`：请求体传 `{"question":"..."}`，跳过 ASR，直接测试 LLM + TTS。

## 配置

服务器读取的是 `.env`，不是 `.env.example`。

第一次使用时复制模板：

```powershell
Copy-Item .env.example .env
```

然后在 `.env` 里填写：

- `BAIDU_ASR_API_KEY`
- `BAIDU_ASR_SECRET_KEY`
- `BAIDU_TTS_API_KEY`
- `BAIDU_TTS_SECRET_KEY`
- `ARK_API_KEY`

### LLM 切换

服务器支持三家大模型，通过 `LLM_PROVIDER` 三选一：

| 取值 | 厂商 | 说明 |
|------|------|------|
| `ark` | 豆包（火山方舟） | 默认值，走 `ARK_API_URL` / `ARK_MODEL` |
| `qwen` | 千问 | 走 `QWEN_API_URL`（DashScope OpenAI 兼容）/ `QWEN_MODEL` |
| `deepseek` | DeepSeek | 走 `DEEPSEEK_API_URL`（官方 OpenAI 兼容）/ `DEEPSEEK_MODEL` |

- 三家都复用 `ARK_MAX_TOKENS`、`ARK_TEMPERATURE` 和 `VOICE_SYSTEM_PROMPT`。
- **例外**：DeepSeek 是推理模型，服务器已通过 `thinking=disabled` 关闭其思考过程（否则思考 token 会吃掉 max_tokens 导致回答被截断甚至为空），并使用独立的 `DEEPSEEK_MAX_TOKENS`（默认 1024）作为正文上限。
- 切换方法：修改 `.env` 里的 `LLM_PROVIDER`，然后重启服务器。

如果修改了 `.env`，需要重启服务器，旧进程不会自动重新读取配置。

## 启动

推荐使用项目自带虚拟环境：

```powershell
cd C:\Users\Administrator\Desktop\Voice_interact_system_project\ASR_LLM_TTS_Server
.\.venv\Scripts\python.exe -m asr_llm_tts_server.server --host 127.0.0.1 --port 8001
```

默认会同时在终端显示日志，并自动保存到 `logs\server_时间_port端口.txt`，每行日志会自动带毫秒级时间戳。如果本次不想保存文件，可以加：

```powershell
.\.venv\Scripts\python.exe -m asr_llm_tts_server.server --host 127.0.0.1 --port 8001 --no-log-file
```

健康检查：

```powershell
python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8001/health').read().decode())"
```

## 文本测试

这个模式跳过 ASR，只验证火山方舟 LLM 和百度 TTS：

```powershell
.\.venv\Scripts\python.exe tools\demo_client.py --server http://127.0.0.1:8001 --text "你是谁？" --out reply.mp3
```

## PCM 测试

`input.pcm` 只是示例占位名，必须换成真实文件路径。

```powershell
.\.venv\Scripts\python.exe tools\demo_client.py --server http://127.0.0.1:8001 --pcm test_input.pcm --out pcm_reply.mp3
```

PCM 格式要求：

- 16 kHz
- 16-bit
- 单声道
- 裸 PCM，无 WAV 文件头

如果你手里是已经符合 `16 kHz / 16-bit / 单声道` 的 WAV，可以先提取裸 PCM：

```powershell
.\.venv\Scripts\python.exe tools\wav_to_pcm.py --wav input.wav --out input.pcm
```

注意：`tools\wav_to_pcm.py` 只提取 PCM，不做重采样。如果 WAV 不是 16 kHz、16-bit、单声道，需要先用音频工具转换格式。

## 已验证结果

文本链路测试：

- 问题：`你是谁？`
- 结果：生成 `reply.mp3`
- 耗时：约 1.7-2.3 秒，取决于当次网络和云服务响应

PCM 链路测试：

- 使用项目中现有 WAV 提取出的 `test_input.pcm`
- ASR 识别：`你好。`
- LLM 回答：`你好呀！很高兴见到你，有什么我可以帮你的吗？`
- 耗时：ASR 753 ms，LLM 586 ms，TTS 982 ms，总计 2322 ms
- 结果：生成 `pcm_reply.mp3`

## 后续方向

这个版本先跑通完整链路。下一步建议做 ESP32 长连接和流式流水线：

- ESP32 边接收 CI PCM 边上传服务器。
- 服务器边接收边 ASR。
- LLM 流式输出稳定短句后立刻送 TTS。
- TTS 音频一出来就下发给 ESP32。
- ESP32 按 CI 的 `PLAY_DATA_GET` 节奏通过 UART1 喂给 CI 设备端。
## 慢请求定位

如果你想看一轮请求到底慢在哪里，直接看服务器终端里的这些阶段日志：

- `voice_server trace stage=asr_done`
- `voice_server trace stage=llm_first_delta`
- `voice_server trace stage=first_segment_ready`
- `voice_server trace stage=tts_first_audio`
- `voice_server trace stage=stream_done`
- `voice_server http stage=first_chunk_sent`
- `voice_server http stage=stream_done`

## ESP32 WebSocket 对接

### 端到端实时模型切换

实时路线支持豆包和千问两套独立适配器，通过 `.env` 切换，不需要修改或重新烧录 ESP32/CI 固件：

```env
REALTIME_PROVIDER=qwen
QWEN_REALTIME_API_KEY=你的百炼实时语音Key
QWEN_REALTIME_MODEL=qwen3.5-omni-flash-realtime
QWEN_REALTIME_VOICE=Tina
QWEN_REALTIME_TURN_MODE=push_to_talk
QWEN_REALTIME_WEB_SEARCH_ENABLE=1
```

千问路线固定采用 Push-to-Talk：设备在 `PCM_FINISH` 后发送 `dialogue.commit`，服务器补 200 ms 尾静音后依次发送 `input_audio_buffer.commit` 和 `response.create`。`turn_detection` 固定为 `null`；把 `QWEN_REALTIME_TURN_MODE` 配成 `server_vad` 会被拒绝。

`QWEN_REALTIME_WEB_SEARCH_ENABLE=1` 会在 `session.update` 中设置 `enable_search: true`。模型会自主判断当前问题是否需要联网搜索；该能力不使用本地天气/日历函数，也不会改变 Push-to-Talk 的提交顺序。

切回豆包只需设置 `REALTIME_PROVIDER=doubao` 并重启服务。两条路线共用现有 24 kHz PCM 到 CI 兼容 MP3 转码桥，但供应商协议和记忆数据库互相隔离。

直连 DashScope 验证（绕过本地服务与 ESP32）：

```powershell
.\.venv\Scripts\python.exe tools\qwen_realtime_direct_test.py --audio input_16k_mono.wav --out qwen_direct.mp3
```

本地服务端到端验证：

```powershell
.\.venv\Scripts\python.exe tools\realtime_ws_client.py --pcm input_16k_mono.pcm --pace --out qwen_server.mp3
```

服务器新增了 ESP32 专用 WebSocket 接口：

- 地址：`ws://电脑局域网IPv4:8001/v1/dialogue/ws`
- ESP32 发送文本帧：`{"type":"dialogue.start","pcm_bytes":实际PCM字节数}`
- ESP32 分片发送 PCM 二进制帧。
- ESP32 发送文本帧：`{"type":"dialogue.end"}`
- 服务器返回 MP3 二进制帧，ESP32 收到第一包 MP3 后启动 CI 播放。
- 服务器最后返回文本帧：`{"type":"dialogue.done",...}`

ESP32 侧切换位置：

- WebSocket URL：`ESP32-S3_Master/components/BSP/VOICE_SERVER/voice_server_client.c` 里的 `VOICE_SERVER_DIALOGUE_WS_URL`
- HTTP URL：同文件里的 `VOICE_SERVER_DIALOGUE_STREAM_URL`
- 协议宏：同文件里的 `VOICE_SERVER_TRANSPORT`

默认值现在是 `VOICE_SERVER_TRANSPORT_WEBSOCKET`。如果要回退到旧的 HTTP 流式方案，把它改成：

```c
#define VOICE_SERVER_TRANSPORT VOICE_SERVER_TRANSPORT_HTTP
```

服务器给 ESP32 用时仍然建议这样启动，让同一局域网设备可以访问：

```powershell
.\.venv\Scripts\python.exe -m asr_llm_tts_server.server --host 0.0.0.0 --port 8001
```

ESP32 联调时建议保留默认自动日志。启动后终端会打印 `Log file: ...`，测试完成后直接查看这个 txt 文件即可；时间戳可以用来对齐 ESP32 串口日志里的毫秒时间。

云服务服务器启动，让公网可以去访问 ASR_LLM_TTS_Server 服务
'''powershell
cd /opt/server/ASR_LLM_TTS_Server
# 激活项目本地 python 虚拟环境 `.venv`，隔离依赖包。
# 激活成功终端提示符前面会出现 `(.venv)`标记。
source .venv/bin/activate 
python -m asr_llm_tts_server.server --host 0.0.0.0 --port 8001
'''
 
## 天气查询

服务器支持和风天气查询。命中天气问题时会先调用和风天气接口拿到事实，再由 LLM 润色成更自然的一句短播报，最后交给百度 TTS 播放。

`.env` 配置：

```env
WEATHER_ENABLE=1
WEATHER_LLM_POLISH=1
WEATHER_DEFAULT_CITY=深圳
QWEATHER_API_HOST=https://你的和风天气专属Host
QWEATHER_API_KEY=你的和风天气APIKey
QWEATHER_GEO_ENABLE=1
QWEATHER_TIMEOUT_SEC=8
```

第一版默认城市是深圳；用户说“今天天气怎么样”时查深圳。常用城市已内置 Location ID，所以支持“明天北京会下雨吗”“后天上海天气怎么样”等问法。

当前用户提供的专属 Host 已可访问 `/v7/weather/now`、`/v7/weather/3d` 和 `/geo/v2/city/lookup`，所以可以打开 `QWEATHER_GEO_ENABLE=1`。这样内置表没有的城市会先走 GeoAPI 自动解析 Location ID。

Host 建议填写带 `https://` 的完整地址；如果只填裸域名，服务器启动时也会自动补成 HTTPS 地址。

可选高级配置通常不用写。若 GeoAPI 专属 Host 和天气 Host 不同，可额外配置 `QWEATHER_GEO_API_HOST`；若只想补少量固定城市或处理城市重名，可额外配置：

```env
QWEATHER_CITY_IDS=城市名:和风LocationID,城市名2:和风LocationID2
```

## LLM 工具路由

服务器支持第一版 LLM 工具路由。明确命中关键词时仍然走原来的快速规则；如果关键词没有命中，服务器会先让 LLM 判断用户是不是想查天气或听音乐。LLM 只返回 `chat`、`weather`、`music` 这三类动作和少量参数，真正的和风天气查询、本地 MP3 选择和播放仍然由服务器执行。

`.env` 配置：

```env
TOOL_ROUTER_ENABLE=1
TOOL_ROUTER_TIMEOUT_SEC=4
TOOL_ROUTER_MIN_CONFIDENCE=0.65
TOOL_ROUTER_MUSIC_MIN_CONFIDENCE=0.75
```

这个路由用于覆盖更自然的说法，例如“今天是晴天吗”“出门要不要带伞”“首歌听一听”“来点音乐”。置信度太低时不会强行调用工具，会继续走普通聊天，避免把一句含糊的话误触发成播放音乐。

测试时可以看服务器日志里的 `tool_router_decision`。如果后面出现 `weather_tool_answer`，说明是 LLM 判断后调用了天气工具；如果出现 `music_tool_selected` 和 `action_sent action=music`，说明是 LLM 判断后调用了本地音乐播放。

## 本地音乐播放

服务器已支持第一版本地 MP3 音乐播放。用户说“播放晴天”“我想听跳楼机”“播放太阳之子”时，服务器会先在本地曲库匹配歌曲；命中后直接把 MP3 二进制流通过现有 WebSocket 下发给 ESP32，ESP32 再按原来的 CI 拉流播放协议转发给 CI 芯片。

配置项：

```env
MUSIC_ENABLE=1
MUSIC_DIR=music
MUSIC_CHUNK_BYTES=4096
```

曲库目录默认是：

```text
ASR_LLM_TTS_Server/music
```

当前测试曲库包含：

- `太阳之子.mp3`
- `晴天.mp3`
- `跳楼机.mp3`

调试接口：

```powershell
python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8001/v1/music/list').read().decode('utf-8'))"
```

文本测试：

```powershell
.\.venv\Scripts\python.exe tools\demo_client.py --server http://127.0.0.1:8001 --text "播放晴天" --out music_reply.mp3
```

注意：第一版命中歌曲后不会先合成“马上播放”这类提示音，而是直接播放歌曲，避免把 TTS MP3 和歌曲 MP3 拼接到同一条下行流里影响 CI MP3 解码稳定性。

WebSocket 硬件链路里，服务器会在音乐 MP3 第一包之前发送一条 `dialogue.action` 文本帧，内容包含 `action=music`、歌名和建议超时时间。ESP32 收到后会只把本轮音乐播放超时放宽到 10 分钟，普通聊天和天气回复仍保持原来的短超时。

新增歌曲建议先用 `tools\normalize_music.py` 归一化为 16 kHz、单声道、CBR、无 ID3 元数据的 MP3。该工具默认会在歌曲开头补 300ms 静音，并修正首帧里会被 CI 精简播放器误判为 ID3 长度的 4 个字节：

```powershell
.\.venv\Scripts\python.exe tools\normalize_music.py --src music_raw --out music --overwrite
```

如果硬件仍然播放不稳定，可以先试更低码率：

```powershell
.\.venv\Scripts\python.exe tools\normalize_music.py --src music_raw --out music --overwrite --bitrate 32k
```

