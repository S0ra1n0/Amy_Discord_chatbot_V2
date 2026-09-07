"""Cross-check README claims and config against the actual code."""
import io, os, re, sys, importlib.util

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
# Resolve the project root from this file, so the suite runs from any checkout
PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(PROJ)
sys.path.insert(0, PROJ)

spec = importlib.util.spec_from_file_location("amy", os.path.join(PROJ, "Amy_chatbot_V2.py"))
amy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(amy)

readme = open("README.md", encoding="utf-8").read()
helptxt = open("commands_help.py", encoding="utf-8").read()
envex = open(".env.example", encoding="utf-8").read()
reqs = open("requirements.txt", encoding="utf-8").read()

fails = []
def check(label, ok, detail=""):
    print(("  PASS " if ok else "  FAIL ") + label + (("  -> " + detail) if detail and not ok else ""))
    if not ok:
        fails.append(label)

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
check("memory depth %d documented" % database.MAX_MEMORY_MESSAGES,
      "last %d messages" % database.MAX_MEMORY_MESSAGES in readme)
check("Discord 2000-char limit documented",
      str(amy.MAX_DISCORD_LEN) in readme)

print()
print("=== commands: code vs README vs help text ===")
src = open("Amy_chatbot_V2.py", encoding="utf-8").read()
dispatched = set(re.findall(r'command == "([a-z0-9]+)"', src))
dispatched |= set(re.findall(r'command in \(([^)]*)\)', src)[0].replace('"', '').replace(' ', '').split(',')) \
    if re.findall(r'command in \(([^)]*)\)', src) else set()
for cmd in sorted(dispatched):
    check("/%s in help text" % cmd, "`/%s" % cmd in helptxt)
    check("/%s in README table" % cmd, "`/%s" % cmd in readme)

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
if fails:
    print("%d CHECK(S) FAILED:" % len(fails))
    for f in fails:
        print("   - " + f)
    sys.exit(1)
print("ALL AUDIT CHECKS PASSED")
