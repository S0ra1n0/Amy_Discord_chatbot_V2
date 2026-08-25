# database.py
import sqlite3
from typing import Dict, List, Tuple, Union

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

            CREATE TABLE IF NOT EXISTS bot_voice_channels (
                channel_id INTEGER PRIMARY KEY,
                guild_id   INTEGER NOT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );
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

    #----Bot-created voice channels (survive restarts so orphans can be cleaned up)------
    def add_bot_voice_channel(self, channel_id: int, guild_id: int) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO bot_voice_channels (channel_id, guild_id) VALUES (?, ?)",
            (int(channel_id), int(guild_id))
        )
        self.conn.commit()

    def remove_bot_voice_channel(self, channel_id: int) -> None:
        self.conn.execute(
            "DELETE FROM bot_voice_channels WHERE channel_id = ?",
            (int(channel_id),)
        )
        self.conn.commit()

    def get_bot_voice_channels(self) -> List[Tuple[int, int]]:
        """Return [(channel_id, guild_id), ...] for every voice channel Amy created."""
        cursor = self.conn.execute("SELECT channel_id, guild_id FROM bot_voice_channels")
        return [(row[0], row[1]) for row in cursor.fetchall()]
    #--------------------------------------

    def prune_old_messages(self, days: int) -> int:
        """Delete messages older than the given number of days. Returns rows deleted."""
        cursor = self.conn.execute(
            "DELETE FROM messages WHERE timestamp < datetime('now', ?)",
            (f'-{days} days',)
        )
        self.conn.commit()
        return cursor.rowcount
