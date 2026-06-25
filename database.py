# database.py
import sqlite3
from typing import Dict, List, Union

MAX_MEMORY_MESSAGES = 10

class ConversationDB:
    def __init__(self, db_path: str = "amy_memory.db") -> None:
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self._create_tables()

    def _create_tables(self) -> None:
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS messages (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                server    TEXT NOT NULL,
                channel   TEXT NOT NULL,
                role      TEXT NOT NULL,
                content   TEXT NOT NULL,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_server_channel ON messages(server, channel);
        """)
        self.conn.commit()

    def store_message(self, server: Union[int, str], channel: int, role: str, content: str) -> None:
        self.conn.execute(
            "INSERT INTO messages (server, channel, role, content) VALUES (?, ?, ?, ?)",
            (str(server), str(channel), role, content)
        )
        self.conn.commit()
        # Prune old messages beyond the limit
        self.conn.execute("""
            DELETE FROM messages WHERE id IN (
                SELECT id FROM messages
                WHERE server = ? AND channel = ?
                ORDER BY id DESC
                LIMIT -1 OFFSET ?
            )
        """, (str(server), str(channel), MAX_MEMORY_MESSAGES))
        self.conn.commit()

    def pop_last_message(self, server: Union[int, str], channel: int) -> None:
        """Remove the most recent message for this channel (used to undo orphaned user turns on error)."""
        self.conn.execute("""
            DELETE FROM messages WHERE id = (
                SELECT id FROM messages
                WHERE server = ? AND channel = ?
                ORDER BY id DESC
                LIMIT 1
            )
        """, (str(server), str(channel)))
        self.conn.commit()

    def get_messages(self, server: Union[int, str], channel: int) -> List[Dict[str, str]]:
        cursor = self.conn.execute("""
            SELECT role, content FROM messages
            WHERE server = ? AND channel = ?
            ORDER BY id ASC
        """, (str(server), str(channel)))
        return [{"role": row[0], "content": row[1]} for row in cursor.fetchall()]

    def clear_channel(self, server: Union[int, str], channel: int) -> None:
        self.conn.execute(
            "DELETE FROM messages WHERE server = ? AND channel = ?",
            (str(server), str(channel))
        )
        self.conn.commit()

    def get_stats(self) -> Dict[str, int]:
        """Return total messages stored and number of unique active channels."""
        cursor = self.conn.execute(
            "SELECT COUNT(*), COUNT(DISTINCT server || '|' || channel) FROM messages"
        )
        row = cursor.fetchone()
        return {"total_messages": row[0], "active_channels": row[1]}
