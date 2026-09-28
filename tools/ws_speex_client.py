"""WebSocket 音频上传测试客户端（模拟 ESP32 端）。

零依赖：手写最小 WebSocket 客户端（握手 + 带 mask 的帧收发），
按 ESP32 端 voice_server_client.c 的协议模拟上传：
    dialogue.start（带 audio_format 字段）-> 二进制帧 -> dialogue.end
    接收 dialogue.ready / MP3 二进制帧 / dialogue.done

用法示例：
    # 上传 CI 格式 speex（服务器自动解码成 PCM 再识别）
    python tools/ws_speex_client.py --server ws://127.0.0.1:8001 --file test_input.ci.speex --format speex --out speex_reply.mp3

    # 上传原始 PCM（直传）
    python tools/ws_speex_client.py --server ws://127.0.0.1:8001 --file test_input.pcm --format pcm --out pcm_reply.mp3
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import socket
import ssl
import struct
import sys
import urllib.parse
from pathlib import Path


WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class WsClient:
    """功能说明：最小 WebSocket 客户端（仅支持文本/二进制帧，无分片）。"""

    def __init__(self, url: str, timeout: float = 30.0):
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in ("ws", "wss"):
            raise SystemExit(f"不支持的协议: {parsed.scheme}（仅支持 ws/wss）")
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or (443 if parsed.scheme == "wss" else 80)
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query

        raw = socket.create_connection((host, port), timeout=timeout)
        if parsed.scheme == "wss":
            raw = ssl.create_default_context().wrap_socket(raw, server_hostname=host)
        self.sock = raw

        key = base64.b64encode(os.urandom(16)).decode("ascii")
        request = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "User-Agent: ws_speex_client/0.1\r\n\r\n"
        )
        self.sock.sendall(request.encode("ascii"))
        response = self._recv_until(b"\r\n\r\n")
        header_text = response.decode("iso-8859-1", errors="replace")
        if " 101 " not in header_text.split("\r\n", 1)[0]:
            self.sock.close()
            raise SystemExit(f"WebSocket 握手失败: {header_text[:300]}")
        expected = base64.b64encode(
            hashlib.sha1((key + WS_GUID).encode("ascii")).digest()
        ).decode("ascii")
        if expected not in header_text:
            self.sock.close()
            raise SystemExit("WebSocket 握手 Accept key 不匹配")

    def _recv_until(self, marker: bytes) -> bytes:
        data = bytearray()
        while marker not in data:
            chunk = self.sock.recv(4096)
            if not chunk:
                break
            data.extend(chunk)
        return bytes(data)

    def send_frame(self, opcode: int, payload: bytes) -> None:
        """功能说明：发送一个掩码帧（客户端必须加 mask）。"""

        first = 0x80 | (opcode & 0x0F)
        length = len(payload)
        mask = os.urandom(4)
        if length < 126:
            header = bytes([first, 0x80 | length])
        elif length <= 0xFFFF:
            header = bytes([first, 0x80 | 126]) + struct.pack("!H", length)
        else:
            header = bytes([first, 0x80 | 127]) + struct.pack("!Q", length)
        masked = bytes(payload[i] ^ mask[i % 4] for i in range(length))
        self.sock.sendall(header + mask + masked)

    def send_text(self, data: dict) -> None:
        self.send_frame(0x1, json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))

    def send_binary(self, payload: bytes) -> None:
        self.send_frame(0x2, payload)

    def recv_frame(self, timeout: float = 120.0) -> tuple[int, bytes]:
        """功能说明：接收一帧（服务端帧无 mask）。超时返回 (None, b"")。"""

        self.sock.settimeout(timeout)
        try:
            header = self._recv_exact(2)
        except (socket.timeout, OSError):
            return None, b""
        if not header:
            return None, b""
        b1, b2 = header
        opcode = b1 & 0x0F
        length = b2 & 0x7F
        if length == 126:
            length = struct.unpack("!H", self._recv_exact(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", self._recv_exact(8))[0]
        payload = self._recv_exact(length) if length else b""
        return opcode, payload

    def _recv_exact(self, size: int) -> bytes:
        data = bytearray()
        while len(data) < size:
            chunk = self.sock.recv(size - len(data))
            if not chunk:
                break
            data.extend(chunk)
        return bytes(data)

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


def main() -> int:
    """功能说明：模拟 ESP32 通过 WebSocket 上传音频并保存回复 MP3。

    入参含义：命令行参数指定服务器地址、音频文件、格式与输出文件。
    返回值说明：成功返回 0。
    使用注意事项：--format 支持 pcm / speex，缺省 pcm。
    """

    parser = argparse.ArgumentParser(description="WebSocket 音频上传测试客户端（模拟 ESP32）")
    parser.add_argument("--server", default="ws://127.0.0.1:8001/v1/dialogue/ws", help="服务器 WS 地址")
    parser.add_argument("--file", required=True, help="要上传的音频文件（裸 PCM 或 CI 格式 speex）")
    parser.add_argument("--format", default="pcm", choices=["pcm", "speex"], help="音频格式")
    parser.add_argument("--out", default="ws_reply.mp3", help="输出 MP3 文件")
    args = parser.parse_args()

    audio_path = Path(args.file)
    if not audio_path.is_file():
        raise SystemExit(f"音频文件不存在: {audio_path}")
    audio_data = audio_path.read_bytes()
    if not audio_data:
        raise SystemExit(f"音频文件为空: {audio_path}")

    ws = WsClient(args.server)
    try:
        ws.send_text(
            {
                "type": "dialogue.start",
                "audio_format": args.format,
                "audio_bytes": len(audio_data),
                "sample_rate": 16000,
            }
        )
        ws.send_binary(audio_data)
        ws.send_text({"type": "dialogue.end"})

        mp3_parts: list[bytes] = []
        done = False
        while True:
            opcode, payload = ws.recv_frame()
            if opcode is None:
                print("连接超时/关闭，未收到 dialogue.done")
                return 1
            if opcode == 0x1:
                message = json.loads(payload.decode("utf-8"))
                msg_type = message.get("type")
                if msg_type == "dialogue.ready":
                    print(f"收到 dialogue.ready req={message.get('req')}")
                elif msg_type == "dialogue.done":
                    print(
                        f"收到 dialogue.done: audio_bytes={message.get('audio_bytes')} "
                        f"chunks={message.get('audio_chunks')} first_chunk_ms={message.get('first_chunk_ms')}"
                    )
                    done = True
                    break
                elif msg_type == "dialogue.error":
                    print(f"服务器错误: {message.get('error')}")
                    return 1
            elif opcode == 0x2:
                mp3_parts.append(payload)
        if not done or not mp3_parts:
            print("未收到任何 MP3 音频数据")
            return 1
        Path(args.out).write_bytes(b"".join(mp3_parts))
        print(f"OK: 已保存 {args.out}（{len(b''.join(mp3_parts))} 字节）")
        return 0
    finally:
        ws.close()


if __name__ == "__main__":
    raise SystemExit(main())
