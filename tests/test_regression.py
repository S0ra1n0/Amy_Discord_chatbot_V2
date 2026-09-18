import io, os, sys, importlib.util
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
# Resolve the project root from this file, so the suite runs from any checkout
PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJ); os.chdir(PROJ)
spec = importlib.util.spec_from_file_location("amy", os.path.join(PROJ, "Amy_chatbot_V2.py"))
amy = importlib.util.module_from_spec(spec); spec.loader.exec_module(amy)

UID = 1234
amy.rate_limit_store.clear()
for i in range(amy.RATE_LIMIT_MAX):
    allowed, reset = amy.check_rate_limit(UID)
    assert allowed is True and reset == 0, "call %d should be allowed" % (i + 1)
allowed, reset = amy.check_rate_limit(UID)
assert allowed is False and 0 < reset <= amy.RATE_LIMIT_WINDOW
print("check_rate_limit: OK (%d allowed, next blocked, reset=%ds)" % (amy.RATE_LIMIT_MAX, reset))

amy.rate_limit_store.clear(); amy.voice_cmd_store.clear()
for _ in range(amy.RATE_LIMIT_MAX):
    amy.check_rate_limit(UID)
assert amy.check_rate_limit(UID)[0] is False
assert amy.check_voice_cooldown(UID)[0] is True, "voice bucket must be independent"
print("limiters are independent: OK")

amy.voice_cmd_store.clear()
for i in range(amy.VOICE_CMD_MAX):
    assert amy.check_voice_cooldown(UID)[0] is True
assert amy.check_voice_cooldown(UID)[0] is False
print("check_voice_cooldown: OK (%d allowed, next blocked)" % amy.VOICE_CMD_MAX)

amy.rate_limit_store.clear()
for _ in range(amy.RATE_LIMIT_MAX):
    amy.check_rate_limit(555)
assert amy.get_rate_limited_count() == 1
print("get_rate_limited_count: OK")

amy.rate_limit_store.clear(); amy.voice_cmd_store.clear()
amy.check_rate_limit(1); amy.check_voice_cooldown(2)
amy.rate_limit_store[1] = [0.0]      # far outside the window
amy.voice_cmd_store[2] = [0.0]
assert amy.prune_rate_limit_stores() == 2
assert 1 not in amy.rate_limit_store and 2 not in amy.voice_cmd_store
print("prune_rate_limit_stores: OK (expired entries dropped)")

assert amy.find_split_index("hello world", 2000) == 11
assert amy.find_split_index("x" * 2500, 1990) == 1990
assert amy.extract_model_names({"models": [{"model": "a:1"}]}) == ["a:1"]
assert amy.strip_think_tags("<think>hmm</think>Answer") == "Answer"
assert amy.get_display_text("<think>still going") == ""
print("earlier helpers (split/model-names/think-tags): OK")

# Conversation history is bounded in two places and both matter. Storage trims each
# channel to MAX_MEMORY_MESSAGES, and get_messages(limit=) bounds what is replayed to the
# model on top of that. The trim must keep the NEWEST messages but hand them back
# oldest-first, which is the easy thing to get backwards - reversed history would make Amy
# answer the wrong turn.
import tempfile
from database import MAX_MEMORY_MESSAGES, ConversationDB

_tmp = tempfile.mktemp(suffix=".db")
_db = ConversationDB(_tmp)
for i in range(MAX_MEMORY_MESSAGES + 20):
    _db.store_message(1, 99, "user" if i % 2 == 0 else "assistant", "msg-%03d" % i)

full = _db.get_messages(1, 99)
assert len(full) == MAX_MEMORY_MESSAGES,     "storage must trim to the cap: kept %d of %d" % (len(full), MAX_MEMORY_MESSAGES)
last = MAX_MEMORY_MESSAGES + 20 - 1
assert full[-1]["content"] == "msg-%03d" % last, "newest message lost: %s" % full[-1]
assert [m["content"] for m in full] == sorted(m["content"] for m in full),     "history must be chronological, not reversed"

win = _db.get_messages(1, 99, limit=4)
assert len(win) == 4, "limit not applied: %d" % len(win)
assert win == full[-4:], "limit must take the newest, in order: %s" % win
assert win[-1]["content"] == "msg-%03d" % last

assert _db.get_messages(1, 99, limit=999) == full, "limit larger than history"
assert _db.get_messages(1, 99, limit=0) == full, "limit=0 means no limit, not empty"
assert len(_db.get_messages(1, 99, limit=1)) == 1
assert _db.get_messages(1, 77, limit=4) == [], "unknown channel"
assert _db.get_stats()["total_messages"] >= MAX_MEMORY_MESSAGES
_db.conn.close(); os.remove(_tmp)
print("history window: OK (cap=%d, newest kept, chronological)" % MAX_MEMORY_MESSAGES)

assert amy.HISTORY_LIMIT == MAX_MEMORY_MESSAGES,     "one knob only - the bot and the database must not disagree about history depth"
assert amy.HISTORY_LIMIT >= 2, "a window below 2 cannot hold one exchange"
assert isinstance(amy.OLLAMA_KEEP_ALIVE, str) and amy.OLLAMA_KEEP_ALIVE,     "keep_alive must be a non-empty string for the ollama client"
print("speed settings: history=%d, keep_alive=%r"
      % (amy.HISTORY_LIMIT, amy.OLLAMA_KEEP_ALIVE))

# The startup warm-up must never be able to stop the bot from coming online. Ollama not
# running is a normal state (it is a separate service), and the only cost of a failed
# warm-up is that the first reply loads the model itself, the way it always did.
import asyncio as _asyncio

_real_chat = amy.ollama.chat
_calls = []

def _fake_chat(*a, **kw):
    _calls.append(kw)
    return {"done": True}

amy.ollama.chat = _fake_chat
try:
    _asyncio.run(amy.warm_model())
    assert len(_calls) == 1, "warm_model should issue exactly one request"
    assert _calls[0].get("messages") == [],         "preload must send no messages, or it generates a throwaway reply"
    assert _calls[0].get("keep_alive") == amy.OLLAMA_KEEP_ALIVE,         "the preload must carry keep_alive, else it unloads again in 5 minutes"

    def _boom(*a, **kw):
        raise ConnectionError("Ollama is not running")
    amy.ollama.chat = _boom
    _asyncio.run(amy.warm_model())          # must log and return, not raise
    print("model warm-up: OK (empty preload, carries keep_alive, survives a dead Ollama)")
finally:
    amy.ollama.chat = _real_chat

# ---- Settings persist across restarts -------------------------------------------------
# /toggle and /model were plain globals, so a restart silently reverted an admin's choice.
_stmp = tempfile.mktemp(suffix=".db")
_sdb = ConversationDB(_stmp)
assert _sdb.get_setting("model") is None, "unset key must be None"
assert _sdb.get_setting("model", "fallback") == "fallback", "default not honoured"
assert _sdb.get_bool_setting("bot_enabled", True) is True, "bool default not honoured"
assert _sdb.get_bool_setting("bot_enabled", False) is False

_sdb.set_setting("model", "qwen3.5:7b")
assert _sdb.get_setting("model") == "qwen3.5:7b"
_sdb.set_setting("model", "llama3:8b")
assert _sdb.get_setting("model") == "llama3:8b", "second write must overwrite"
assert _sdb.conn.execute("SELECT COUNT(*) FROM settings WHERE key='model'").fetchone()[0] == 1,     "upsert must not leave duplicate rows"

for stored, expected in [("true", True), ("True", True), ("1", True), ("yes", True),
                         ("on", True), ("false", False), ("0", False), ("", False),
                         ("nonsense", False)]:
    _sdb.set_setting("flag", stored)
    assert _sdb.get_bool_setting("flag", True) is expected,         "%r should read back as %s" % (stored, expected)

_sdb.set_bool_setting("bot_enabled", False)
assert _sdb.get_bool_setting("bot_enabled", True) is False

# Reopening is what a restart actually does.
_sdb.conn.close()
_sdb2 = ConversationDB(_stmp)
assert _sdb2.get_setting("model") == "llama3:8b", "model lost across reopen"
assert _sdb2.get_bool_setting("bot_enabled", True) is False, "toggle lost across reopen"
_sdb2.conn.close(); os.remove(_stmp)
print("settings persistence: OK (upsert, bool parsing, survives reopen)")

assert amy.DEFAULT_MODEL, "a fallback model must exist for when a saved one is uninstalled"

# ---- Slash command errors reach the user ----------------------------------------------
# All 25 command callbacks are unguarded, so without tree.error an unexpected exception
# leaves the interaction unanswered and the user sees "application did not respond".
from discord import app_commands as _ac
import discord as _dc

class _Resp:
    def __init__(self, done): self._d = done; self.sent = []
    def is_done(self): return self._d
    async def send_message(self, **kw): self._d = True; self.sent.append(("response", kw))
class _Follow:
    def __init__(self, sent): self.sent = sent
    async def send(self, **kw): self.sent.append(("followup", kw))
class _It:
    def __init__(self, deferred=False):
        self.response = _Resp(deferred)
        self.followup = _Follow(self.response.sent)
        self.command = type("C", (), {"name": "play"})()
        self.user = "tester"
        self.guild = type("G", (), {"name": "guild"})()

# @tree.error replaces CommandTree.on_error with the plain function, so the registered
# handler should be ours. Without it, unhandled errors are invisible to the user.
assert amy.tree.on_error is amy.on_app_command_error,     "tree.error is not registered: %r" % amy.tree.on_error

# A failed permission check already replied via deny(); re-reporting would double up.
_quiet = _It()
_asyncio.run(amy.on_app_command_error(_quiet, _ac.CheckFailure("denied")))
assert _quiet.response.sent == [], "CheckFailure must stay quiet: %s" % _quiet.response.sent

_boom = _ac.CommandInvokeError(amy.tree.get_command("play"), RuntimeError("kaboom"))

_fresh = _It()
_asyncio.run(amy.on_app_command_error(_fresh, _boom))
assert len(_fresh.response.sent) == 1, "user must be told something went wrong"
assert _fresh.response.sent[0][0] == "response", "should respond directly when not deferred"
assert _fresh.response.sent[0][1].get("ephemeral") is True, "errors stay private"

_deferred = _It(deferred=True)
_asyncio.run(amy.on_app_command_error(_deferred, _boom))
assert _deferred.response.sent[0][0] == "followup",     "after defer() it must follow up, not respond twice"

# An expired interaction token is normal; the handler must swallow it, having logged.
_dead = _It()
async def _raise(**kw):
    raise _dc.HTTPException(type("R", (), {"status": 404, "reason": "Not Found"})(), "gone")
_dead.response.send_message = _raise
_asyncio.run(amy.on_app_command_error(_dead, _boom))
print("slash error handler: OK (quiet on CheckFailure, replies once, survives a dead token)")

# ---- Queue survives a restart ---------------------------------------------------------
# The queue only ever lived in memory, so restarting threw away whatever was lined up.
import music as _music

_qtmp = tempfile.mktemp(suffix=".db")
_qdb = ConversationDB(_qtmp)
assert _qdb.load_player_state(1) is None, "nothing saved yet"
assert _qdb.saved_guild_ids() == []

_tracks = [_music.Track("T%d" % i, "https://youtu.be/%d" % i, 200 + i, "asker") for i in range(3)]
_qdb.save_player_state(1, [_music.track_to_dict(t) for t in _tracks],
                       loop_mode="queue", volume=0.4, voice_channel_id=555,
                       text_channel_id=777, resume_position=42.5)

_st = _qdb.load_player_state(1)
assert _st is not None
assert [_music.track_from_dict(x) for x in _st["tracks"]] == _tracks, "order or content changed"
assert _st["loop_mode"] == "queue" and abs(_st["volume"] - 0.4) < 1e-9
assert _st["voice_channel_id"] == 555 and _st["text_channel_id"] == 777
assert abs(_st["resume_position"] - 42.5) < 1e-9, "mid-track position must survive"

# Saving again replaces the snapshot; appending would grow the queue on every tick.
_qdb.save_player_state(1, [_music.track_to_dict(_tracks[0])], "off", 1.0, None, None, 0.0)
assert len(_qdb.load_player_state(1)["tracks"]) == 1, "second save must replace"
assert _qdb.conn.execute(
    "SELECT COUNT(*) FROM saved_queue WHERE guild_id=1").fetchone()[0] == 1, "stale rows left"

# Guilds must not bleed into each other.
_qdb.save_player_state(2, [_music.track_to_dict(_tracks[2])], "track", 0.9, 1, 2, 3.0)
assert sorted(_qdb.saved_guild_ids()) == [1, 2]
_qdb.clear_player_state(1)
assert _qdb.saved_guild_ids() == [2], "clearing one guild must not touch another"
assert _qdb.load_player_state(1) is None
assert len(_qdb.load_player_state(2)["tracks"]) == 1

# An empty queue clears rather than saving an empty snapshot to restore later.
_qdb.save_player_state(2, [], "off", 1.0, None, None, 0.0)
assert _qdb.load_player_state(2)["tracks"] == []
_qdb.clear_player_state(2)
assert _qdb.saved_guild_ids() == []

_qdb.conn.close(); os.remove(_qtmp)
print("queue persistence: OK (round trip, replace-not-append, per-guild isolation)")

assert amy.SNAPSHOT_INTERVAL > 0, "snapshots need a positive interval"
assert hasattr(amy, "snapshot_player") and hasattr(amy, "restore_player")
assert amy.snapshot_queues_task.seconds == amy.SNAPSHOT_INTERVAL,     "the task interval and the documented constant must agree"
print("snapshot task: OK (every %ds)" % amy.SNAPSHOT_INTERVAL)

# ---- Per-model reasoning mode ---------------------------------------------------------
# The right `think` setting is NOT the same for every model. qwen3.5:2b needs think=False
# (2.3s, clean); the identical setting makes qwen3:4b write 3,800 characters of its own
# reasoning into the reply. /model probes and records the right mode per model.
assert amy.think_value("false") is False
assert amy.think_value("true") is True
assert amy.think_value("auto") is None, "auto must omit the argument, which ollama spells None"
for junk in ("banana", "", None, 5, "  AUTO  "):
    got = amy.think_value(junk)
    assert got in (False, True, None), "%r -> %r" % (junk, got)
assert amy.think_value("  AUTO  ") is None, "whitespace and case must not matter"

assert amy.normalise_think_mode("1") == "true"
assert amy.normalise_think_mode("off") == "false"
assert amy.normalise_think_mode("nonsense") == "false", "unknown falls back"
assert amy.normalise_think_mode("nonsense", fallback="auto") == "auto"
assert amy.normalise_think_mode("auto") == "auto"
assert amy.OLLAMA_THINK in amy.THINK_MODES, "the configured default must be a valid mode"
print("think modes: OK (false/true/auto, junk falls back)")

# The detector decides whether /model switches a model's mode, so both directions matter.
LEAKS = [
    'Hmm, the user is asking "What is 2 + 2?" and wants the answer in one short sentence.',
    "Okay, the user wants a brief explanation of the difference between lists and tuples.",
    "Okay, let's see. I need to figure out what 17 times 23 is.",
    "The user wants a two-line birthday message for their friend Minh.",
]
ANSWERS = [
    "In Python, **lists** and **tuples** are both ordered collections.",
    "2 plus 2 equals 4.",
    "We are comparing two data structures in Python: lists and tuples.",
    "Happy Birthday, Minh! Wishing you a day filled with laughter.",
    "Hello there! It is my pleasure to assist you with your programming endeavors.",
    "Let me know if you'd like me to explain any part in more detail.",
    "",
]
for t in LEAKS:
    assert amy.looks_like_reasoning(t), "missed a real leak: %r" % t[:60]
for t in ANSWERS:
    assert not amy.looks_like_reasoning(t), "false positive on a real answer: %r" % t[:60]
print("looks_like_reasoning: OK (%d leaks caught, %d answers cleared)" % (len(LEAKS), len(ANSWERS)))

assert 0 < amy.PROBE_MAX_TOKENS <= 1024, "the probe must stay cheap"
assert 0 < amy.PROBE_TIMEOUT <= 120, "the probe must stay bounded - /model used to be instant"
print("probe bounds: OK (<=%d tokens, <=%.0fs)" % (amy.PROBE_MAX_TOKENS, amy.PROBE_TIMEOUT))

# ---- The prompt must not promise tools that aren't offered -----------------------------
# With search off, the old prompt still said she had a working search tool - and she acted
# on it, answering current-events questions from memory in 9 of 9 runs without once saying
# she couldn't check.
assert "Knowledge:" not in amy.BASE_SYSTEM_PROMPT, "the base must not carry a knowledge block"
_on = amy.build_system_prompt(amy.BASE_SYSTEM_PROMPT, True)
_off = amy.build_system_prompt(amy.BASE_SYSTEM_PROMPT, False)
assert "working web_search" in _on
assert "working web_search" not in _off, "must not promise a tool that isn't offered"
assert "Never claim to have searched" in _off
assert _on.count("Knowledge:") == 1 and _off.count("Knowledge:") == 1
for _p in (_on, _off):
    assert "Remember: You are here" in _p, "the persona must survive assembly"
    assert _p.startswith("You are Amy"), "the opening must stay put"
assert amy.system_prompt == (_on if amy.WEB_SEARCH else _off),     "the live prompt must match the running configuration"
print("system prompt: OK (claims match the tools actually offered)")

print()
print("ALL REGRESSION TESTS PASSED")
