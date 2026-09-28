"""本地短期对话记忆。"""

from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path


class ConversationMemory:
    """功能说明：按设备 SN 保存最近若干轮对话。

    入参含义：`db_path` 是 SQLite 文件路径，`max_stored_turns` 是每台设备最多保留轮数。
    返回值说明：实例方法返回最近记忆或写入结果。
    使用注意事项：每次数据库操作都显式关闭连接，避免 Windows 下文件句柄长期占用。
    """

    def __init__(self, db_path: Path, max_stored_turns: int = 80):
        self.db_path = db_path
        self.max_stored_turns = max(1, max_stored_turns)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    @contextmanager
    def _connection(self):
        """功能说明：创建一次性数据库连接并确保提交关闭。"""

        conn = sqlite3.connect(str(self.db_path))
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_db(self) -> None:
        """功能说明：初始化记忆表。"""

        with self._connection() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS conversation_turns (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    device_sn TEXT NOT NULL,
                    ts REAL NOT NULL,
                    user_text TEXT NOT NULL,
                    assistant_text TEXT NOT NULL,
                    request_id TEXT NOT NULL
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_turns_device_ts ON conversation_turns(device_sn, ts)")

    def recent_turns(self, device_sn: str, limit: int) -> list[dict]:
        """功能说明：读取某台设备最近的对话轮次。"""

        if not device_sn or limit <= 0:
            return []
        with self._connection() as conn:
            rows = conn.execute(
                """
                SELECT user_text, assistant_text, request_id, ts
                FROM conversation_turns
                WHERE device_sn = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (device_sn, limit),
            ).fetchall()
        rows.reverse()
        return [
            {"user_text": user_text, "assistant_text": assistant_text, "request_id": request_id, "ts": ts}
            for user_text, assistant_text, request_id, ts in rows
        ]

    def context_messages(self, device_sn: str, limit: int) -> list[dict]:
        """功能说明：把最近记忆转换成可注入 LLM 的消息列表。"""

        messages: list[dict] = []
        for item in self.recent_turns(device_sn, limit):
            messages.append({"role": "user", "content": item["user_text"]})
            messages.append({"role": "assistant", "content": item["assistant_text"]})
        return messages

    def store_turn(self, device_sn: str, user_text: str, assistant_text: str, request_id: str) -> None:
        """功能说明：保存一轮完整对话，并裁剪过旧记录。"""

        if not device_sn or not user_text or not assistant_text:
            return
        with self._connection() as conn:
            conn.execute(
                """
                INSERT INTO conversation_turns(device_sn, ts, user_text, assistant_text, request_id)
                VALUES (?, ?, ?, ?, ?)
                """,
                (device_sn, time.time(), user_text, assistant_text, request_id),
            )
            conn.execute(
                """
                DELETE FROM conversation_turns
                WHERE device_sn = ? AND id NOT IN (
                    SELECT id FROM conversation_turns
                    WHERE device_sn = ?
                    ORDER BY id DESC
                    LIMIT ?
                )
                """,
                (device_sn, device_sn, self.max_stored_turns),
            )

    def reset(self, device_sn: str | None = None) -> int:
        """功能说明：清空指定设备或全部设备的记忆。"""

        with self._connection() as conn:
            if device_sn:
                cursor = conn.execute("DELETE FROM conversation_turns WHERE device_sn = ?", (device_sn,))
            else:
                cursor = conn.execute("DELETE FROM conversation_turns")
            return int(cursor.rowcount or 0)
