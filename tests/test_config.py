"""
Reading settings from .env: parsing, bad values, and the order things load in.

The parsers in config.py are pure and tested directly. The loading behaviour is tested by
importing the bot in a fresh process per scenario (tests/_config_probe.py), because the
bot reads its settings at import time - which is exactly where the bug lived: database.py
read HISTORY_LIMIT before the bot had loaded .env, so the setting never worked.
"""
import io
import json
import os
import subprocess
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TESTS = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJ)
os.chdir(PROJ)

import config

fails = []


def check(label, ok, got=""):
    print(("  PASS " if ok else "  FAIL ") + label)
    if not ok:
        fails.append(label)
        print("        got:", str(got)[:160])


# ---- pure parsers ----------------------------------------------------------------------
print("=== parse_int ===")
for raw, want, warns in [(None, 10, False), ("", 10, False), ("  ", 10, False), ("4", 4, False),
                         (" 25 ", 25, False), ("abc", 10, True), ("4.5", 10, True),
                         ("1", 2, True), ("-3", 2, True)]:
    value, problem = config.parse_int("HISTORY_LIMIT", raw, default=10, minimum=2)
    check("%-6r -> %d%s" % (raw, want, " (warns)" if warns else ""),
          value == want and bool(problem) == warns, (value, problem))
_, msg = config.parse_int("HISTORY_LIMIT", "abc", default=10, minimum=2)
check("the warning names the setting and the fallback", "HISTORY_LIMIT" in msg and "10" in msg, msg)

print()
print("=== parse_bool ===")
for raw, want, warns in [(None, True, False), ("", True, False), ("true", True, False),
                         ("True", True, False), ("1", True, False), ("yes", True, False),
                         ("on", True, False), ("false", False, False), ("0", False, False),
                         ("no", False, False), ("OFF", False, False), ("ture", True, True),
                         ("enabled", True, True)]:
    value, problem = config.parse_bool("WEB_SEARCH", raw, default=True)
    check("%-9r -> %s%s" % (raw, want, " (warns)" if warns else ""),
          value is want and bool(problem) == warns, (value, problem))

print()
print("=== shadowed_settings ===")
before = {"OLLAMA_KEEP_ALIVE": "5m", "PATH": "x", "SAME": "1"}
in_file = {"OLLAMA_KEEP_ALIVE": "30m", "SAME": "1", "ONLY_FILE": "v", "BLANK": None}
check("names a setting the system environment overrides",
      config.shadowed_settings(before, in_file) == ["OLLAMA_KEEP_ALIVE"],
      config.shadowed_settings(before, in_file))
check("identical values are not reported", "SAME" not in config.shadowed_settings(before, in_file))
check("nothing set twice -> nothing reported", config.shadowed_settings({}, in_file) == [])


# ---- loading, end to end ----------------------------------------------------------------
def probe(dotenv_values, extra_env=None):
    env = {k: v for k, v in os.environ.items()
           if k not in ("HISTORY_LIMIT", "DB_PRUNE_DAYS", "WEB_SEARCH", "OLLAMA_THINK",
                        "OLLAMA_KEEP_ALIVE", "DISCORD_TOKEN")}
    env.update(extra_env or {})
    env["AMY_PROBE_SCENARIO"] = json.dumps({"dotenv": dotenv_values})
    env["PYTHONIOENCODING"] = "utf-8"
    r = subprocess.run([sys.executable, os.path.join(TESTS, "_config_probe.py")], env=env,
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    result = None
    for line in r.stdout.splitlines():
        if line.startswith("PROBE_RESULT "):
            result = json.loads(line[len("PROBE_RESULT "):])
    return r.returncode, result, r.stdout + r.stderr


print()
print("=== HISTORY_LIMIT in .env is honoured ===")
# database.py used to read it at import - before the bot had loaded .env - so the bot
# always ran with the default 10 whatever .env said.
code, res, out = probe({"HISTORY_LIMIT": "4"})
check("the bot imports", code == 0 and res is not None, out[-300:])
if res:
    check("the bot uses HISTORY_LIMIT=4 from .env", res["HISTORY_LIMIT"] == 4, res)
    check("and so does the database's storage cap", res["db_max_messages"] == 4, res)

print()
print("=== bad values warn and fall back instead of crashing or silently misreading ===")
code, res, out = probe({"HISTORY_LIMIT": "abc", "DB_PRUNE_DAYS": "", "WEB_SEARCH": "ture",
                        "OLLAMA_THINK": "maybe"})
check("the bot still imports", code == 0 and res is not None, out[-400:])
if res:
    check("HISTORY_LIMIT=abc -> 10", res["HISTORY_LIMIT"] == 10 and res["db_max_messages"] == 10, res)
    check("DB_PRUNE_DAYS blank -> 30", res["DB_PRUNE_DAYS"] == 30, res)
    check("WEB_SEARCH=ture keeps search ON (the old parser silently turned it off)",
          res["WEB_SEARCH"] is True, res)
    check("OLLAMA_THINK=maybe -> false", res["OLLAMA_THINK"] == "false", res)
for name in ("HISTORY_LIMIT", "WEB_SEARCH", "OLLAMA_THINK"):
    check("a warning names %s" % name,
          any("[WARNING]" in l and name in l for l in out.splitlines()), out[-400:])

code, res, out = probe({"WEB_SEARCH": "off"})
check("WEB_SEARCH=off turns search off", res is not None and res["WEB_SEARCH"] is False, res)
code, res, out = probe({"WEB_SEARCH": "on"})
check("WEB_SEARCH=on keeps it on", res is not None and res["WEB_SEARCH"] is True, res)

print()
print("=== a setting shadowed by the system environment is reported ===")
# python-dotenv never overrides a variable that is already set, so a system-wide value
# silently wins. OLLAMA_KEEP_ALIVE is the realistic case: Ollama reads the same name.
code, res, out = probe({"OLLAMA_KEEP_ALIVE": "30m"}, extra_env={"OLLAMA_KEEP_ALIVE": "5m"})
check("the system value is the one in effect (documented precedence)",
      res is not None and res["OLLAMA_KEEP_ALIVE"] == "5m", res)
warning = [l for l in out.splitlines() if "[WARNING]" in l and "OLLAMA_KEEP_ALIVE" in l]
check("a warning names the shadowed setting", bool(warning), out[-400:])
check("without printing either value (it could be a token)",
      warning and "5m" not in warning[0] and "30m" not in warning[0], warning)

code, res, out = probe({"OLLAMA_KEEP_ALIVE": "30m"})
check("no warning when nothing is shadowed",
      not any("[WARNING]" in l and "OLLAMA_KEEP_ALIVE" in l for l in out.splitlines()))

print()
if fails:
    print("%d CHECK(S) FAILED" % len(fails))
    sys.exit(1)
print("ALL CONFIG TESTS PASSED")
