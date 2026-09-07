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

print()
print("ALL REGRESSION TESTS PASSED")
