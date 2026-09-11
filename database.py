# database.py
import os
import sqlite3
from typing import Dict, List, Optional, Tuple, Union

# How many messages are kept per channel, and therefore how far back Amy can remember.
# Every one of these is replayed to the model on each reply, so it is also a latency knob:
# a real message averages ~98 tokens against a 4096-token context window, so the default 10
# costs ~1k tokens and about 0.15s of prompt processing. Raising it lengthens her memory and
# slows each reply slightly; roughly 30 is the practical ceiling before the context window
# starts squeezing the reply itself.
MAX_MEMORY_MESSAGES: int = max(2, int(os.getenv("HISTORY_LIMIT", "10")))

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

    def get_messages(self, server: Union[int, str], channel: int,
                     limit: Optional[int] = None) -> List[Dict[str, str]]:
        """
        Conversation history for a channel, oldest first.

        `limit` keeps only the most recent N messages. Nothing is deleted - this bounds what
        gets replayed to the model, which otherwise grows until it overflows the context
        window (about 35 real messages against a 4096-token window) and starts pushing the
        oldest turns out silently.

        The newest rows are selected with DESC + LIMIT so the database does the trimming,
        then reversed, because the model needs them in chronological order.
        """
        if limit is not None and limit > 0:
            cursor = self.conn.execute("""
                SELECT role, content FROM messages
                WHERE server = ? AND channel = ?
                ORDER BY id DESC
                LIMIT ?
            """, (str(server), str(channel), limit))
            rows = cursor.fetchall()
            rows.reverse()
        else:
            cursor = self.conn.execute("""
                SELECT role, content FROM messages
                WHERE server = ? AND channel = ?
                ORDER BY id ASC
            """, (str(server), str(channel)))
            rows = cursor.fetchall()
        return [{"role": row[0], "content": row[1]} for row in rows]

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
