"""
Schema versioning in database.py, against throwaway database files.

The database had no version number, so the first column change would have needed an
improvised migration run against operators' existing amy_memory.db files. These checks pin
the rules that make migrations safe: an unversioned database upgrades without losing rows,
a failed migration changes nothing, a database from a newer Amy is refused rather than
rewritten, and a migration that has shipped is never edited.
"""
import hashlib
import io
import os
import sqlite3
import sys
import tempfile

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJ)

import database
from database import MIGRATIONS, SCHEMA_VERSION, ConversationDB, SchemaTooNewError
from _check import check, finish

TABLES = {"messages", "bot_voice_channels", "settings", "saved_queue", "saved_player",
          "guild_settings"}


_tmp = tempfile.TemporaryDirectory()
_count = [0]


def temp_path():
    # sqlite creates the file; every connection is closed before the directory is removed.
    _count[0] += 1
    return os.path.join(_tmp.name, "schema%d.db" % _count[0])


def tables(conn):
    return {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}


def version_of(path):
    conn = sqlite3.connect(path)
    try:
        return database.schema_version(conn)
    finally:
        conn.close()


print("=== a new database is created at the current version ===")
path = temp_path()
db = ConversationDB(path)
check("version is SCHEMA_VERSION", database.schema_version(db.conn) == SCHEMA_VERSION,
      database.schema_version(db.conn))
check("every table exists", TABLES <= tables(db.conn), tables(db.conn))
db.conn.close()

print("=== an unversioned database from before versioning upgrades in place ===")
# Build exactly what an existing amy_memory.db looks like: the baseline tables, rows in
# them, and user_version 0 because nothing ever set it.
path = temp_path()
legacy = sqlite3.connect(path)
legacy.executescript(MIGRATIONS[0])
legacy.execute("INSERT INTO messages (server, channel, role, content) VALUES ('s','c','user','hi')")
legacy.execute("INSERT INTO settings (key, value) VALUES ('model', 'qwen3:4b')")
legacy.execute("INSERT INTO saved_queue (guild_id, slot, title, query) VALUES (1, 0, 'Song', 'https://x')")
legacy.commit()
check("starts at version 0", database.schema_version(legacy) == 0)
legacy.close()

db = ConversationDB(path)
check("stamped as the current version", database.schema_version(db.conn) == SCHEMA_VERSION,
      database.schema_version(db.conn))
check("messages kept", db.conn.execute("SELECT content FROM messages").fetchall() == [("hi",)])
check("settings kept", db.get_setting("model") == "qwen3:4b", db.get_setting("model"))
check("saved queue kept",
      db.conn.execute("SELECT title FROM saved_queue").fetchall() == [("Song",)])
db.conn.close()

print("=== migration 2: a version-1 database with a saved queue upgrades and loads ===")
v1path = temp_path()
v1 = sqlite3.connect(v1path)
v1.executescript(MIGRATIONS[0])
v1.execute("PRAGMA user_version = 1")
v1.execute("INSERT INTO saved_player (guild_id) VALUES (7)")
v1.execute("INSERT INTO saved_queue (guild_id, slot, title, query, requested_by) "
           "VALUES (7, 0, 'Mine', 'https://x/a', 'alice')")
v1.execute("INSERT INTO saved_queue (guild_id, slot, title, query, requested_by) "
           "VALUES (7, 1, 'Mix', 'https://x/b', 'Autoplay')")
v1.commit()
v1.close()
db = ConversationDB(v1path)
check("upgraded to the current version", database.schema_version(db.conn) == SCHEMA_VERSION)
old = db.load_player_state(7)["tracks"]
check("old rows keep their data", [t["title"] for t in old] == ["Mine", "Mix"]
      and [t["requested_by"] for t in old] == ["alice", "Autoplay"], old)
check("old rows have no requester id", [t["requester_id"] for t in old] == [None, None], old)
check("old rows are not flagged autoplay (the loader decides from the name)",
      [t["is_autoplay"] for t in old] == [False, False], old)

print("=== an upgraded database round-trips the new fields ===")
db.save_player_state(7, [
    {"title": "Human", "query": "https://x/c", "requested_by": "bob", "requester_id": 4242},
    {"title": "Auto", "query": "https://x/d", "requested_by": "Autoplay", "is_autoplay": True},
    {"title": "Plain", "query": "https://x/e"},
], "off", 1.0, None, None)
new = db.load_player_state(7)["tracks"]
check("requester id saved and loaded", new[0]["requester_id"] == 4242, new)
check("a human track is not autoplay", new[0]["is_autoplay"] is False, new)
check("autoplay flag saved and loaded", new[1]["is_autoplay"] is True and new[1]["requester_id"] is None, new)
check("missing fields default to no id, not autoplay",
      new[2]["requester_id"] is None and new[2]["is_autoplay"] is False, new)
db.conn.close()

print("=== migration 3: a version-2 database upgrades with nothing lost ===")
v2path = temp_path()
v2 = sqlite3.connect(v2path)
v2.executescript(MIGRATIONS[0] + MIGRATIONS[1])
v2.execute("PRAGMA user_version = 2")
v2.execute("INSERT INTO settings (key, value) VALUES ('autoplay', 'false')")
v2.execute("INSERT INTO settings (key, value) VALUES ('up_next', 'false')")
v2.execute("INSERT INTO settings (key, value) VALUES ('model', 'qwen3:4b')")
v2.execute("INSERT INTO saved_player (guild_id) VALUES (7)")
v2.execute("INSERT INTO saved_queue (guild_id, slot, title, query, requester_id) "
           "VALUES (7, 0, 'Mine', 'https://x/a', 55)")
v2.execute("INSERT INTO messages (server, channel, role, content) VALUES ('s','c','user','hi')")
v2.commit()
v2.close()
db = ConversationDB(v2path)
check("upgraded to the current version", database.schema_version(db.conn) == SCHEMA_VERSION)
check("guild_settings exists and is empty",
      db.conn.execute("SELECT COUNT(*) FROM guild_settings").fetchone() == (0,))
check("bot-wide rows untouched",
      (db.get_setting("autoplay"), db.get_setting("up_next"), db.get_setting("model"))
      == ("false", "false", "qwen3:4b"))
check("queue and messages kept",
      db.load_player_state(7) is not None and db.load_player_state(7)["tracks"][0]["requester_id"] == 55
      and db.conn.execute("SELECT content FROM messages").fetchall() == [("hi",)])
check("an existing server with no row inherits the bot-wide value (no behaviour change)",
      db.guild_bool(7, "autoplay", True) is False and db.guild_bool(7, "up_next", True) is False)
db.conn.close()

print("=== per-server settings ===")
gpath = temp_path()
db = ConversationDB(gpath)
check("nothing stored -> None", db.get_guild_setting(1, "autoplay") is None)
check("nothing anywhere -> the code default", db.guild_bool(1, "autoplay", True) is True
      and db.guild_bool(1, "autoplay", False) is False)
db.set_setting("autoplay", "false")                 # the old bot-wide row
check("a server with no row inherits the bot-wide row", db.guild_bool(2, "autoplay", True) is False)
db.set_guild_bool(1, "autoplay", True)
check("its own value wins over the bot-wide row", db.guild_bool(1, "autoplay", False) is True)
check("...and does not leak to another server",
      db.guild_bool(2, "autoplay", True) is False and db.get_guild_setting(2, "autoplay") is None)
check("the bot-wide row was not copied or changed",
      db.get_setting("autoplay") == "false" and db.get_guild_setting(2, "autoplay") is None)
db.set_guild_bool(1, "autoplay", False)
db.set_guild_bool(2, "autoplay", True)
check("two servers hold different values at once",
      (db.guild_bool(1, "autoplay", True), db.guild_bool(2, "autoplay", False)) == (False, True))
check("the setting is per key", db.guild_bool(1, "up_next", True) is True)
db.set_guild_setting(3, "autoplay", "banana")       # a hand-edited row
db.set_setting("autoplay", "true")
check("an invalid stored value falls through to the bot-wide one",
      db.guild_bool(3, "autoplay", False) is True)
db.set_setting("autoplay", "banana")
check("...and an invalid bot-wide one to the default", db.guild_bool(3, "autoplay", False) is False)
check("a cached read is refreshed by a write to the same server",
      db.guild_bool(6, "up_next", True) is True
      and (db.set_guild_bool(6, "up_next", False), db.guild_bool(6, "up_next", True))[1] is False)
db.clear_guild_setting(1, "autoplay")
check("clearing a server's value returns it to inheriting",
      db.get_guild_setting(1, "autoplay") is None and db.guild_bool(1, "autoplay", True) is True)
db.set_guild_bool(5, "up_next", False)
db.conn.close()
db = ConversationDB(gpath)
check("a restart keeps the per-server values",
      db.get_guild_setting(2, "autoplay") == "true" and db.guild_bool(2, "autoplay", False) is True
      and db.guild_bool(5, "up_next", True) is False)
db.conn.close()

print("=== reopening an up-to-date database changes nothing ===")
db = ConversationDB(path)
check("still the current version", database.schema_version(db.conn) == SCHEMA_VERSION)
check("rows still there", db.get_setting("model") == "qwen3:4b")
db.conn.close()

print("=== a database from a newer Amy is refused, not rewritten ===")
conn = sqlite3.connect(path)
conn.execute("PRAGMA user_version = %d" % (SCHEMA_VERSION + 5))
conn.close()
try:
    ConversationDB(path)
    refused, message = False, ""
except SchemaTooNewError as e:
    refused, message = True, str(e)
check("opening raises SchemaTooNewError", refused)
check("the message names both versions",
      str(SCHEMA_VERSION + 5) in message and str(SCHEMA_VERSION) in message, message)
try:
    os.rename(path, path + ".moved")      # Windows refuses this while a handle is open
    os.rename(path + ".moved", path)
    released = True
except OSError:
    released = False
check("the refused database's file is released", released)
check("the version is left as it was", version_of(path) == SCHEMA_VERSION + 5, version_of(path))
conn = sqlite3.connect(path)
check("and the data untouched",
      conn.execute("SELECT value FROM settings WHERE key='model'").fetchone() == ("qwen3:4b",))
conn.close()

print("=== a new migration applies once, in order ===")
path = temp_path()
conn = sqlite3.connect(path)
added = MIGRATIONS + ["ALTER TABLE settings ADD COLUMN note TEXT;"]
check("returns the new version", database.apply_migrations(conn, added) == len(added))
check("version advanced", database.schema_version(conn) == len(added))
check("the column exists",
      "note" in [r[1] for r in conn.execute("PRAGMA table_info(settings)")])
# A second run must not re-apply it - ALTER ... ADD COLUMN would fail on a duplicate.
try:
    database.apply_migrations(conn, added)
    rerun_ok = True
except sqlite3.Error:
    rerun_ok = False
check("running again is a no-op", rerun_ok)
conn.close()

print("=== a failing migration leaves the database exactly as it was ===")
path = temp_path()
conn = sqlite3.connect(path)
database.apply_migrations(conn, MIGRATIONS)
# First statement succeeds, second fails: without the transaction the half table would stay
# behind and the version might still advance.
broken = MIGRATIONS + ["CREATE TABLE half_done (x INTEGER);\nINSERT INTO no_such_table VALUES (1);"]
try:
    database.apply_migrations(conn, broken)
    raised = False
except sqlite3.Error:
    raised = True
check("the error is raised, not swallowed", raised)
check("the version did not advance", database.schema_version(conn) == SCHEMA_VERSION,
      database.schema_version(conn))
check("the first statement was rolled back", "half_done" not in tables(conn), tables(conn))
check("the connection is usable afterwards", not conn.in_transaction)
conn.close()

print("=== shipped migrations are never edited ===")
# Operators' databases have already run these, so changing one does nothing for them and
# silently forks their schema from a fresh install's. Change the schema by APPENDING a
# migration, then add its hash here.
SHIPPED = [
    "6a04fdafdd6183f1",   # 1: baseline (the schema as it stood before versioning)
    "2836c127dc5d5236",   # 2: saved_queue.requester_id, saved_queue.is_autoplay
    "e423afb401aaeff9",   # 3: guild_settings (per-server settings)
]
got = [hashlib.sha256(m.encode("utf-8")).hexdigest()[:16] for m in MIGRATIONS]
check("no shipped migration has changed", got[:len(SHIPPED)] == SHIPPED, got)
check("every migration is pinned here", len(got) == len(SHIPPED),
      "add the new migration's hash to SHIPPED: %s" % got[len(SHIPPED):])

finish("ALL SCHEMA TESTS PASSED")
