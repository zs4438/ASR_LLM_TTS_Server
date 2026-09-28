# 本地短期记忆说明

服务器已支持第一版本地短期记忆。记忆只保存在运行 `ASR_LLM_TTS_Server` 的服务器本机，不会写入 CI 芯片或 ESP32。

## 存储位置

默认 SQLite 文件：

```text
ASR_LLM_TTS_Server/data/voice_memory.sqlite3
```

如果服务器跑在本地电脑，记忆就在本地电脑；如果服务器跑在腾讯云，记忆就在腾讯云服务器。

## 配置

`.env` 中可以配置：

```text
MEMORY_ENABLE=1
MEMORY_DB_PATH=data/voice_memory.sqlite3
MEMORY_MAX_RECENT_TURNS=4
MEMORY_MAX_STORED_TURNS=80
```

- `MEMORY_ENABLE`：是否启用短期记忆。
- `MEMORY_DB_PATH`：SQLite 数据库路径。
- `MEMORY_MAX_RECENT_TURNS`：每轮注入给大模型的最近对话轮数。
- `MEMORY_MAX_STORED_TURNS`：每台设备最多保留多少轮短期记忆。

## 设备隔离

真机 WebSocket 对话会自动使用 ESP32 在 `dialogue.start` 中上报的 `device_sn` 做隔离。不同 ESP32 设备不会共用记忆。

HTTP 文本测试可以手动传 `device_sn`：

```powershell
curl -X POST http://127.0.0.1:8001/v1/text-dialogue `
  -H "Content-Type: application/json" `
  -d "{\"device_sn\":\"test-device-01\",\"question\":\"我叫小明，喜欢喝咖啡\"}"

curl -X POST http://127.0.0.1:8001/v1/text-dialogue `
  -H "Content-Type: application/json" `
  -d "{\"device_sn\":\"test-device-01\",\"question\":\"我刚才说我喜欢喝什么？\"}"
```

## 查看记忆

```powershell
curl.exe -X POST "http://127.0.0.1:8001/v1/memory/recent" `
  -H "Content-Type: application/json" `
  --% -d "{\"device_sn\":\"VI-ESP32S3-AC276EC71A00\",\"top_k\":10}"
```

## 清空记忆

清空某台设备：

```powershell
Invoke-RestMethod -Method Post `
  -Uri "http://127.0.0.1:8001/v1/memory/reset" `
  -ContentType "application/json" `
  -Body '{"device_sn":"VI-ESP32S3-AC276EC71A00"}'
```

清空全部设备：

```powershell
curl -X POST http://127.0.0.1:8001/v1/memory/reset `
  -H "Content-Type: application/json" `
  -d "{\"all\":true}"
```

## 日志

开启记忆后，服务器启动时会打印：

```text
voice_server memory enabled db=...
```

每轮对话中会出现：

```text
voice_server trace stage=memory_loaded ...
voice_server trace stage=memory_stored ...
```

`memory_loaded` 的 `turns` 字段表示本轮给大模型带了几轮历史。
