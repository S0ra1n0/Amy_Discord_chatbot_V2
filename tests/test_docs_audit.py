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
check("model name in README matches code (%s)" % amy.model_manager.current,
      amy.model_manager.current in readme, "README still references an old model")
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
import fnmatch


def is_gitignored(name, lines):
    """A top-level file named by a .gitignore pattern (no directories, no negation)."""
    pats = [l.strip() for l in lines if l.strip() and not l.lstrip().startswith(("#", "!"))
            and "/" not in l.strip()]
    return any(fnmatch.fnmatch(name, p) for p in pats)


def tracked_python_files():
    """Top-level .py files git tracks, or None when git isn't usable here."""
    import subprocess
    try:
        out = subprocess.run(["git", "ls-files", "*.py"], capture_output=True, text=True,
                             encoding="utf-8", timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return {l.strip() for l in out.stdout.splitlines() if l.strip() and "/" not in l}


def modules_to_document(mods, lines, tracked=None):
    """
    The modules the table must list: everything except scratch files that are gitignored AND
    untracked - a broad pattern added later can never hide a real (tracked) module.
    """
    return [m for m in mods if (tracked is not None and m in tracked) or not is_gitignored(m, lines)]


_ignore_lines = open(".gitignore", encoding="utf-8").read().splitlines()
check("a gitignored scratch file is recognised (Code Runner's tempCodeRunnerFile.py)",
      is_gitignored("tempCodeRunnerFile.py", _ignore_lines) and not is_gitignored("speech.py", _ignore_lines))
# A recreated scratch file (never committed) must not fail the suite
check("the table check skips a scratch file but not a real module",
      modules_to_document(["speech.py", "tempCodeRunnerFile.py"], _ignore_lines) == ["speech.py"])
_tracked = tracked_python_files()
check("git can list the tracked modules", _tracked is not None and "speech.py" in _tracked, _tracked)
check("a tracked module is never skipped, even if a broad pattern would ignore it",
      modules_to_document(["speech.py"], ["*.py"], {"speech.py"}) == ["speech.py"]
      and modules_to_document(["scratch.py"], ["*.py"], {"speech.py"}) == [])
check("no tracked .py is skipped by the real .gitignore",
      not _tracked or set(modules_to_document(sorted(_tracked), _ignore_lines, _tracked)) == _tracked)
for mod in modules_to_document(sorted(glob.glob("*.py")), _ignore_lines, _tracked):
    check("module %s is in its table" % mod, "| `%s` |" % mod in arch)
check("README links to it", "ARCHITECTURE.md" in readme)

print()
print("=== suite counts in the docs match the runner ===")
# README said "27 suites" for a while after more were added. The runner's lists are the truth;
# the wording may change, the numbers may not. History ("8 of the 14 offline suites") is skipped.
import run_tests
n_off, n_net, n_live = len(run_tests.OFFLINE), len(run_tests.NETWORK), len(run_tests.LIVE)
n_all = n_off + n_net + n_live
# The runner must test the venv it was started from (the no-TTS CI stand-in once silently
# ran the bot's .venv instead); only a bare system Python hands over to the project .venv.
_other = os.path.join("C:", os.sep, "elsewhere", "Scripts", "python.exe")
check("runner keeps a venv Python it was started with",
      run_tests.choose_interpreter(_other, in_venv=True) == _other)
_proj_venv = os.path.join(run_tests.PROJ, ".venv", "Scripts", "python.exe")
if os.path.isfile(_proj_venv):
    check("runner hands a bare system Python over to the project .venv",
          run_tests.choose_interpreter(_other, in_venv=False) == _proj_venv)
t_readme = open(os.path.join("tests", "README.md"), encoding="utf-8").read()
summary = re.search(r"(\d+)\s+suites\s*\((\d+)\s+offline,\s*(\d+)\s+network,\s*(\d+)\s+live\)", readme)
check("README states the suite breakdown at all", summary is not None)
if summary:
    got = tuple(int(g) for g in summary.groups())
    check("README suite counts %s == runner (total, offline, network, live) %s"
          % (got, (n_all, n_off, n_net, n_live)), got == (n_all, n_off, n_net, n_live), str(got))
for name, text in (("README.md", readme), ("tests/README.md", t_readme), ("ARCHITECTURE.md", arch)):
    for kind, want in (("offline", n_off), ("network", n_net), ("live", n_live)):
        for m in re.finditer(r"(?<!of the )\b(\d+) %s suites?\b" % kind, text):
            check("%s '%s' == %d" % (name, m.group(0), want), int(m.group(1)) == want, m.group(0))
    for m in re.finditer(r"(?<!of the )\b(\d+) (?:test )?suites\b(?! \()", text):
        check("%s '%s' == %d total" % (name, m.group(0), n_all), int(m.group(1)) == n_all, m.group(0))

print()
print("=== .gitignore patterns actually match ===")
# gitignore has no end-of-line comments: "*.log.*   # rotated logs" is a pattern that
# includes the comment text, so it matches nothing. Two lines were written that way and
# amy.log.1... and kokoro_model/ were silently NOT ignored.
gitignore = open(".gitignore", encoding="utf-8").read().splitlines()
trailing = [l for l in gitignore if l.strip() and not l.lstrip().startswith("#") and " #" in l]
check("no pattern line has a comment after it", not trailing, str(trailing))
for must in ("*.log.*", "tempCodeRunnerFile.py", "kokoro_model/", "piper_model/", "amy_memory.db", ".env"):
    check("%s is ignored" % must, must in [l.strip() for l in gitignore])

print()
finish("ALL AUDIT CHECKS PASSED")
