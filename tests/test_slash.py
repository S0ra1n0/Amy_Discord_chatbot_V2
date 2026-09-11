"""
Structural checks on the slash command tree.

The tree is fully inspectable without connecting to Discord, so these run offline. They
guard the rules Discord enforces server-side at sync time, where a violation would mean
commands silently failing to register.
"""
import inspect
import io
import os
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
# Resolve the project root from this file, so the suite runs from any checkout
PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _fakes import load_bot

amy = load_bot()
commands = list(amy.tree.walk_commands())
by_name = {c.name: c for c in commands}

fails = []


def check(label, ok, got=""):
    print(("  PASS " if ok else "  FAIL ") + label)
    if not ok:
        fails.append(label)
        print("        got:", str(got)[:130])


print("=== tree is populated ===")
check("commands registered", len(commands) >= 20, len(commands))
check("names are unique", len(by_name) == len(commands), len(commands) - len(by_name))

print()
print("=== Discord's naming rules ===")
for c in commands:
    check("/%-12s lowercase, no spaces" % c.name,
          c.name == c.name.lower() and " " not in c.name, c.name)

print()
print("=== every command has a description (Discord rejects empty ones) ===")
for c in commands:
    check("/%-12s described (<=100 chars)" % c.name,
          bool(c.description) and len(c.description) <= 100,
          "%r len=%d" % (c.description, len(c.description or "")))

print()
print("=== every parameter is described ===")
for c in commands:
    for p in c.parameters:
        check("/%s %s described" % (c.name, p.name), bool(p.description), p.description)

print()
print("=== /loop is a picker, not free text ===")
loop = by_name["loop"]
mode = {p.name: p for p in loop.parameters}["mode"]
values = [ch.value for ch in mode.choices]
check("three choices offered", len(values) == 3, values)
check("choices match LoopMode", sorted(values) == sorted(m.value for m in amy.LoopMode), values)

print()
print("=== bounded numeric parameters ===")
for cmd, param, lo, hi in [
    ("dice", "sides", 1, amy.MAX_DICE_SIDES),
    ("dice", "amount", 1, amy.MAX_DICE_AMOUNT),
    ("queue", "page", 1, 100),
    ("remove", "position", 1, 100),
    ("skipto", "position", 1, 100),
    ("volume", "level", 0, 100),
]:
    p = {x.name: x for x in by_name[cmd].parameters}[param]
    check("/%s %s bounded %s..%s" % (cmd, param, lo, hi),
          p.min_value == lo and p.max_value == hi, (p.min_value, p.max_value))

print()
print("=== admin gating ===")
ADMIN = {"create", "clearqueue", "volume", "toggle", "status", "model", "forget"}
for c in commands:
    has_check = bool(getattr(c, "checks", []))
    if c.name in ADMIN:
        check("/%-12s is admin-gated" % c.name, has_check)
    else:
        check("/%-12s is open to everyone" % c.name, not has_check)

print()
print("=== slow commands defer (Discord's 3-second response limit) ===")
# yt-dlp resolution and ollama.list() both take longer than 3s; without a defer the
# interaction fails with "application did not respond".
for name in ("play", "search", "status", "join", "leave", "create", "model",
             "seek", "replay"):     # both re-resolve the stream URL before restarting
    src = inspect.getsource(by_name[name].callback)
    check("/%-8s defers before slow work" % name, "response.defer(" in src)

print()
print("=== guild-only where a guild is required ===")
GUILD_ONLY = {"join", "leave", "create", "play", "search", "pause", "resume", "skip",
              "stop", "queue", "nowplaying", "remove", "skipto", "shuffle", "loop",
              "clearqueue", "volume", "forget", "seek", "replay"}
for name in sorted(GUILD_ONLY):
    c = by_name[name]
    allowed = getattr(c, "allowed_contexts", None)
    ok = (allowed is not None and not allowed.private_channel) or c.guild_only
    check("/%-12s guild-only" % name, ok, allowed)

print("=== long replies split instead of being truncated or rejected ===")
# Discord rejects any message over 2000 chars outright. /help is the one that grows as
# commands are added, so it must go through the splitter rather than a direct send.
import asyncio
from commands_help import HELP_EVERYONE, HELP_ADMIN

full = HELP_EVERYONE + HELP_ADMIN
print("        /help admin view is %d chars (limit %d)" % (len(full), amy.MAX_DISCORD_LEN))

sent = []
class Resp:
    def __init__(self): self._done = False
    def is_done(self): return self._done
    async def send_message(self, content=None, **kw):
        self._done = True; sent.append(content)
class Follow:
    async def send(self, content=None, **kw): sent.append(content)
class It:
    def __init__(self): self.response = Resp(); self.followup = Follow()

it = It()
asyncio.run(amy.send_reply(it, "x " * 3000))     # ~6000 chars
check("splits an over-long reply", len(sent) > 1, len(sent))
check("every part fits Discord's limit",
      all(len(c) <= amy.MAX_DISCORD_LEN for c in sent), [len(c) for c in sent])
check("nothing is lost", sum(len(c) for c in sent) == len("x " * 3000))

sent.clear()
it2 = It()
asyncio.run(amy.send_reply(it2, full, ephemeral=True))
check("/help sends in one piece today", len(sent) == 1, len(sent))
check("/help fits the limit", len(sent[0]) <= amy.MAX_DISCORD_LEN, len(sent[0]))

print()
print()
if fails:
    print("%d CHECK(S) FAILED" % len(fails))
    sys.exit(1)
print("ALL SLASH TESTS PASSED")
