"""Cross-check README claims and config against the actual code."""
import io, os, re, sys, importlib.util

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
# Resolve the project root from this file, so the suite runs from any checkout
PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(PROJ)
sys.path.insert(0, PROJ)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _fakes import assert_isolated_db, isolate_db
isolate_db()            # before importing: the bot reads its DB at import time
spec = importlib.util.spec_from_file_location("amy", os.path.join(PROJ, "Amy_chatbot_V2.py"))
amy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(amy)
assert_isolated_db(amy)

readme = open("README.md", encoding="utf-8").read()
helptxt = open("commands_help.py", encoding="utf-8").read()
envex = open(".env.example", encoding="utf-8").read()
reqs = open("requirements.txt", encoding="utf-8").read()

from _check import finish, record


def check(label, ok, detail=""):
    print(("  PASS " if ok else "  FAIL ") + label + (("  -> " + detail) if detail and not ok else ""))
    record(label, ok)

print("=== constants: code vs README ===")
check("model name in README matches code (%s)" % amy.model,
      amy.model in readme, "README still references an old model")
check("rate limit %d/hour documented" % amy.RATE_LIMIT_MAX,
      "5 chat messages per hour" in readme or "5 messages per hour" in readme)
check("voice cooldown %d/min documented" % amy.VOICE_CMD_MAX,
      "3 per minute" in readme or "3 voice commands per minute" in readme)
check("auto-leave %ds documented" % amy.voice.EMPTY_DISCONNECT_DELAY,
      "60 second" in readme or "60s" in readme)
check("prune default %d days documented" % amy.DB_PRUNE_DAYS,
      "30 days" in readme)
import database
check("memory depth %d documented" % database.DEFAULT_MAX_MESSAGES,
      "last %d messages" % database.DEFAULT_MAX_MESSAGES in readme)
check("Discord 2000-char limit documented",
      str(amy.MAX_DISCORD_LEN) in readme)

print()
src = open("Amy_chatbot_V2.py", encoding="utf-8").read()
print("=== every registered command is documented ===")
# The command tree is the source of truth now, rather than scraping the source for string
# literals. A command that exists but isn't documented fails here.
sys.path.insert(0, os.path.join(PROJ, "tests"))
from _fakes import load_bot
_bot = load_bot()
for c in sorted(_bot.tree.walk_commands(), key=lambda x: x.name):
    check("/%-12s in help text" % c.name, "`/" + c.name in helptxt)
    check("/%-12s in README table" % c.name, "`/" + c.name in readme)

print()
print("=== config consistency ===")
env_keys = set(re.findall(r'os\.getenv\("([A-Z_]+)"', src))
for k in sorted(env_keys):
    check("%s in .env.example" % k, k in envex)

print()
print("=== music constants documented ===")
import music
check("idle disconnect %ds documented" % music.IDLE_DISCONNECT_DELAY,
      "5 minutes" in readme or "5 min" in readme)
check("max queue %d documented" % music.MAX_QUEUE_SIZE,
      str(music.MAX_QUEUE_SIZE) in readme)
check("queue page size %d documented" % music.QUEUE_PAGE_SIZE,
      "%d per page" % music.QUEUE_PAGE_SIZE in readme
      or "%d at a time" % music.QUEUE_PAGE_SIZE in readme)
check("playlist cap %d documented" % music.MAX_PLAYLIST_TRACKS,
      str(music.MAX_PLAYLIST_TRACKS) in readme)
check("all loop modes documented",
      all(m.value in readme for m in music.LoopMode))
check("FFMPEG_PATH documented", "FFMPEG_PATH" in readme)
check("yt-dlp update advice present", "pip install -U yt-dlp" in readme)

print()
print("=== requirements ===")
check("uses discord.py[voice] extra", "discord.py[voice]" in reqs,
      "voice will break at runtime without the extra")
for pkg in ("python-dotenv", "ollama", "httpx"):
    check("%s pinned" % pkg, re.search(pkg + r"==", reqs) is not None)

print()
print("=== runtime gates ===")
import discord.voice_client as vc
check("has_nacl", vc.has_nacl)
check("has_dave", vc.has_dave)
check("intents.members enabled", amy.intents.members)
check("intents.message_content enabled", amy.intents.message_content)

print()
print("=== ARCHITECTURE.md still describes the code ===")
# The design rules used to live in a gitignored notes file. Now they're committed, they can
# rot instead: a renamed helper leaves a rule pointing at nothing. Every snake_case or
# CamelCase name in backticks must still exist somewhere in the source.
import glob
arch = open("ARCHITECTURE.md", encoding="utf-8").read()
all_src = "".join(open(f, encoding="utf-8").read()
                  for f in glob.glob("*.py") + glob.glob(os.path.join("tests", "*.py")))
named = set()
for tok in re.findall(r"`([^`\n]+)`", arch):
    for part in re.findall(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*", tok):
        last = part.split(".")[-1]
        if "_" in last or re.match(r"^[A-Z][a-z]+[A-Z]", last):
            named.add(last)
gone = sorted(n for n in named if not re.search(r"\b%s\b" % re.escape(n), all_src))
check("every code name it mentions exists (%d checked)" % len(named), not gone,
      "no longer in the source: %s" % gone)
check("the scan found names at all", len(named) > 50, "found %d" % len(named))
for mod in sorted(glob.glob("*.py")):
    check("module %s is in its table" % mod, "| `%s` |" % mod in arch)
check("README links to it", "ARCHITECTURE.md" in readme)

print()
finish("ALL AUDIT CHECKS PASSED")
