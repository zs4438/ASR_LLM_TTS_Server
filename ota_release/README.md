# OTA 发布服务

该目录替代 `C:\tmp\esp32-ota\slow_ota_server.py` 的日常发布职责。它与语音 WebSocket 服务是两个独立进程：语音服务继续运行在原端口，OTA 服务只暴露 manifest 和已登记固件的下载接口。

## 发布一份 ESP32 固件

先在 ESP-IDF 环境构建固件，确认 `PROJECT_VER` 与准备发布的版本一致。然后在 `ASR_LLM_TTS_Server` 目录执行：

```powershell
.\.venv\Scripts\python.exe -m ota_release.publish `
  --file ..\ESP32-S3_Master\build\Wifi_Test.bin `
  --target esp32 `
  --version 1.0.13 `
  --channel stable `
  --model esp32-s3-master
```

发布工具会执行以下操作：

- 把固件复制到 `ota_release\artifacts\esp32\1.0.13\`。
- 流式计算文件大小和 SHA-256。
- 原子更新 `ota_release\catalog.json`，并把 `stable` 通道指向该版本。

不要手动编辑 `size` 或 `sha256`。同一个 target/version 默认不可覆盖，发布修复固件应使用更高版本号。`--replace` 只用于开发环境重新做实验。

## 启动服务

先停止旧的 `slow_ota_server.py`，否则它会占用 8000 端口。再执行：

```powershell
.\.venv\Scripts\python.exe -m ota_release.service `
  --host 0.0.0.0 `
  --port 8000 `
  --public-base-url http://192.168.0.160:8000
```

设备读取：

```text
GET /v1/ota/manifest?channel=stable
```

为兼容已经烧录旧测试固件的设备，服务也保留固定的 `GET /manifest.json` 路由，它等价于 stable 通道的 manifest。新构建的固件使用 `/v1/ota/manifest?channel=stable`；旧路由不提供任意文件访问，可在全部设备完成迁移后再单独评估是否移除。

manifest 中的 `esp32.version`、`url`、`sha256`、`size` 与现有 ESP32 OTA 解析逻辑完全兼容。固件下载地址固定为：

```text
GET /v1/ota/artifacts/esp32/{version}
```

设备会在 manifest 和 artifact 请求中带上公开请求头 `X-Device-SN`。服务端日志会输出 `device_sn=<SN>`，用于区分多台设备的检查、下载和中断记录；该字段仅用于追踪，不能作为设备鉴权依据。

服务支持单段 `Range`、`ETag`、`If-None-Match`、`HEAD` 和下载日志。它只会服务 `catalog.json` 已登记且在启动时通过大小/SHA-256 复核的文件，不能通过 URL 读取任意目录文件。

## HTTPS 与鉴权

开发板内网阶段可使用 HTTP。正式部署应传入 PEM 证书和私钥，并用 HTTPS URL 配置 ESP32：

```powershell
.\.venv\Scripts\python.exe -m ota_release.service `
  --host 0.0.0.0 --port 8443 `
  --public-base-url https://ota.example.com `
  --tls-cert C:\certs\ota-fullchain.pem `
  --tls-key C:\certs\ota-private.key
```

服务可设置环境变量 `OTA_RELEASE_TOKEN`，要求 manifest 和 artifact 请求带 `Authorization: Bearer <token>`。当前 ESP32 OTA 客户端尚未发送该请求头，因此在没有先增加设备端鉴权支持时，必须保持该变量为空；不要把 token 直接硬编码进固件源码。生产环境建议在反向代理层实施设备身份、mTLS 或短期签名 URL。

## 本地检查

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -p "test_ota_release.py" -v
```

发布完新版本后，需要重启发布服务以重新加载并复核 catalog。旧 artifact 保留在 catalog 中，可供审计和必要时回退；发布服务不会自动删除旧包。
