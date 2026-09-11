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

print()
print("ALL REGRESSION TESTS PASSED")
