"""豆包端到端实时模式专用短期记忆。"""

from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path


class DoubaoRealtimeMemory:
    """功能说明：按设备 SN 保存豆包端到端实时模式的最近问答。
    入参含义：`db_path` 是实时模式专用 SQLite 文件路径，`max_stored_turns` 是每台设备最多保留的问答轮数。
    返回值说明：实例方法返回最近问答、豆包上下文消息或写入结果。
    使用注意事项：本类不复用级联模式的 `ConversationMemory`，避免两个链路的记忆互相污染。
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
        """功能说明：初始化实时模式专用记忆表。"""

        with self._connection() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS doubao_realtime_turns (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    device_sn TEXT NOT NULL,
                    ts REAL NOT NULL,
                    user_text TEXT NOT NULL,
                    assistant_text TEXT NOT NULL,
                    request_id TEXT NOT NULL
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_doubao_realtime_device_ts ON doubao_realtime_turns(device_sn, ts)")

    def recent_turns(self, device_sn: str, limit: int) -> list[dict]:
        """功能说明：读取指定设备最近的豆包实时问答轮次。"""

        if not device_sn or limit <= 0:
            return []
        with self._connection() as conn:
            rows = conn.execute(
                """
                SELECT user_text, assistant_text, request_id, ts
                FROM doubao_realtime_turns
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

    def context_items(self, device_sn: str, limit: int) -> list[dict[str, str]]:
        """功能说明：转换为豆包实时 `conversation.item.create` 可用的历史消息。"""

        items: list[dict[str, str]] = []
        for turn in self.recent_turns(device_sn, limit):
            items.append({"role": "user", "text": str(turn["user_text"])})
            items.append({"role": "assistant", "text": str(turn["assistant_text"])})
        return items

    def store_turn(self, device_sn: str, user_text: str, assistant_text: str, request_id: str) -> None:
        """功能说明：保存一轮豆包实时完整问答，并按设备裁剪旧记录。"""

        user_text = user_text.strip()
        assistant_text = assistant_text.strip()
        if not device_sn or not user_text or not assistant_text:
            return
        with self._connection() as conn:
            conn.execute(
                """
                INSERT INTO doubao_realtime_turns(device_sn, ts, user_text, assistant_text, request_id)
                VALUES (?, ?, ?, ?, ?)
                """,
                (device_sn, time.time(), user_text, assistant_text, request_id),
            )
            conn.execute(
                """
                DELETE FROM doubao_realtime_turns
                WHERE device_sn = ? AND id NOT IN (
                    SELECT id FROM doubao_realtime_turns
                    WHERE device_sn = ?
                    ORDER BY id DESC
                    LIMIT ?
                )
                """,
                (device_sn, device_sn, self.max_stored_turns),
            )
