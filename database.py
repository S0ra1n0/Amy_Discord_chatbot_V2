# database.py
import os
import sqlite3
from typing import Any, Dict, List, Optional, Tuple, Union

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

            CREATE TABLE IF NOT EXISTS settings (
                key        TEXT PRIMARY KEY,
                value      TEXT NOT NULL,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );

            -- One row per queued track. slot 0 is whatever was playing, 1..n the queue in
            -- order, so restoring is just "read in slot order".
            CREATE TABLE IF NOT EXISTS saved_queue (
                guild_id     INTEGER NOT NULL,
                slot         INTEGER NOT NULL,
                title        TEXT    NOT NULL,
                query        TEXT    NOT NULL,
                duration     INTEGER,
                requested_by TEXT    NOT NULL DEFAULT 'unknown',
                is_local     INTEGER NOT NULL DEFAULT 0,
                thumbnail    TEXT,
                PRIMARY KEY (guild_id, slot)
            );

            CREATE TABLE IF NOT EXISTS saved_player (
                guild_id         INTEGER PRIMARY KEY,
                loop_mode        TEXT    NOT NULL DEFAULT 'off',
                volume           REAL    NOT NULL DEFAULT 1.0,
                voice_channel_id INTEGER,
                text_channel_id  INTEGER,
                resume_position  REAL    NOT NULL DEFAULT 0,
                saved_at         DATETIME DEFAULT CURRENT_TIMESTAMP
            );
        """)
        self.conn.commit()

    #----Queue persistence------
    # The queue lives in memory, so a restart used to lose it entirely - including a long
    # playlist someone had just queued up. This snapshots it per guild.

    def save_player_state(self, guild_id: int, tracks: List[Dict[str, Any]],
                          loop_mode: str, volume: float,
                          voice_channel_id: Optional[int],
                          text_channel_id: Optional[int],
                          resume_position: float = 0.0) -> None:
        """
        Replace the saved snapshot for one guild.

        `tracks` is slot-ordered: index 0 is the track that was playing (if any), the rest
        are the pending queue. Written as one transaction so a crash mid-save can't leave a
        half-written queue that restores as nonsense.
        """
        with self.conn:
            self.conn.execute("DELETE FROM saved_queue WHERE guild_id = ?", (guild_id,))
            self.conn.executemany("""
                INSERT INTO saved_queue
                    (guild_id, slot, title, query, duration, requested_by, is_local, thumbnail)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, [(guild_id, i, t["title"], t["query"], t.get("duration"),
                   t.get("requested_by", "unknown"), 1 if t.get("is_local") else 0,
                   t.get("thumbnail"))
                  for i, t in enumerate(tracks)])
            self.conn.execute("""
                INSERT INTO saved_player (guild_id, loop_mode, volume, voice_channel_id,
                                          text_channel_id, resume_position, saved_at)
                VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(guild_id) DO UPDATE SET
                    loop_mode = excluded.loop_mode,
                    volume = excluded.volume,
                    voice_channel_id = excluded.voice_channel_id,
                    text_channel_id = excluded.text_channel_id,
                    resume_position = excluded.resume_position,
                    saved_at = CURRENT_TIMESTAMP
            """, (guild_id, loop_mode, volume, voice_channel_id, text_channel_id,
                  resume_position))

    def load_player_state(self, guild_id: int) -> Optional[Dict[str, Any]]:
        """The saved snapshot for one guild, or None if nothing was saved."""
        row = self.conn.execute("""
            SELECT loop_mode, volume, voice_channel_id, text_channel_id, resume_position
            FROM saved_player WHERE guild_id = ?
        """, (guild_id,)).fetchone()
        if row is None:
            return None
        tracks = [
            {"title": r[0], "query": r[1], "duration": r[2], "requested_by": r[3],
             "is_local": bool(r[4]), "thumbnail": r[5]}
            for r in self.conn.execute("""
                SELECT title, query, duration, requested_by, is_local, thumbnail
                FROM saved_queue WHERE guild_id = ? ORDER BY slot ASC
            """, (guild_id,))
        ]
        return {
            "loop_mode": row[0],
            "volume": row[1],
            "voice_channel_id": row[2],
            "text_channel_id": row[3],
            "resume_position": row[4],
            "tracks": tracks,
        }

    def saved_guild_ids(self) -> List[int]:
        """Guilds with a saved snapshot, for restoring on startup."""
        return [r[0] for r in self.conn.execute("SELECT guild_id FROM saved_player")]

    def clear_player_state(self, guild_id: int) -> None:
        """Forget a guild's snapshot - used once it has been restored or deliberately cleared."""
        with self.conn:
            self.conn.execute("DELETE FROM saved_queue WHERE guild_id = ?", (guild_id,))
            self.conn.execute("DELETE FROM saved_player WHERE guild_id = ?", (guild_id,))

    #----Settings------
    # Runtime choices an admin makes with /toggle and /model. Without these they live only
    # in module globals, so a restart silently reverts them - you switch to a bigger model,
    # restart, and you are back on the default with nothing to say so.

    def get_setting(self, key: str, default: Optional[str] = None) -> Optional[str]:
        """Stored value for `key`, or `default` when it was never set."""
        row = self.conn.execute(
            "SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row[0] if row else default

    def set_setting(self, key: str, value: str) -> None:
        """Store `value` under `key`, replacing any previous value."""
        self.conn.execute("""
            INSERT INTO settings (key, value, updated_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value,
                                           updated_at = CURRENT_TIMESTAMP
        """, (key, value))
        self.conn.commit()

    def get_bool_setting(self, key: str, default: bool) -> bool:
        """Boolean form of get_setting. Anything unrecognised falls back to `default`."""
        raw = self.get_setting(key)
        if raw is None:
            return default
        return raw.strip().lower() in ("1", "true", "yes", "on")

    def set_bool_setting(self, key: str, value: bool) -> None:
        self.set_setting(key, "true" if value else "false")

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
