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

from _check import check, finish


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
ADMIN = {"create", "clearqueue", "volume", "toggle", "status", "model", "forget", "say", "voice"}
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
             "seek", "replay",     # both re-resolve the stream URL before restarting
             "lyrics",       # asks LRCLIB over the network
             "filter"):      # restarts the song, re-resolving the stream
    src = inspect.getsource(by_name[name].callback)
    check("/%-8s defers before slow work" % name, "response.defer(" in src)

print()
print("=== guild-only where a guild is required ===")
GUILD_ONLY = {"join", "leave", "create", "play", "search", "pause", "resume", "skip",
              "stop", "queue", "nowplaying", "remove", "skipto", "shuffle", "loop",
              "clearqueue", "volume", "forget", "seek", "replay", "previous", "say", "shush", "autoplay", "filter"}
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

print()
print("=== /help is embeds: a description holds 4096, a plain message only 2000 ===")
# It was plain text and sat at 1988/2000, so every new command meant trimming wording. As
# embeds the limits are: description <= 4096, all embeds of one message <= 6000 in total.
# Descriptions, not fields (a field value caps at 1024).
import types

class HelpResp:
    def __init__(self): self.calls = []
    async def send_message(self, **kw): self.calls.append(kw)

def run_help(admin):
    resp = HelpResp()
    guild = types.SimpleNamespace(owner_id=1 if admin else 2, get_member=lambda uid: None)
    it = types.SimpleNamespace(guild=guild, user=types.SimpleNamespace(id=1), response=resp)
    asyncio.run(by_name["help"].callback(it))
    return resp.calls

for who, admin in (("everyone", False), ("admin", True)):
    calls = run_help(admin)
    check("/help (%s) sends one message" % who, len(calls) == 1, len(calls))
    kw = calls[0]
    check("/help (%s) is ephemeral" % who, kw.get("ephemeral") is True, kw)
    embeds = kw.get("embeds", [])
    descs = [e.description or "" for e in embeds]
    print("        /help %s view: %s chars" % (who, [len(d) for d in descs]))
    check("/help (%s) uses descriptions, not fields" % who,
          bool(embeds) and all(not e.fields for e in embeds))
    check("/help (%s) each description <= 4096" % who, all(len(d) <= 4096 for d in descs),
          [len(d) for d in descs])
    check("/help (%s) total <= 6000" % who, sum(len(d) for d in descs) <= 6000,
          sum(len(d) for d in descs))
    check("/help (%s) lists the everyone commands" % who, "`/play [song, URL, or file]`" in descs[0])
    check("/help (%s) keeps markdown (bold heading, backticks)" % who,
          descs[0].startswith("**Commands") and descs[0].count("`") % 2 == 0)
    check("/help (%s) text is unchanged" % who,
          descs == [HELP_EVERYONE] + ([HELP_ADMIN.strip()] if admin else []), "help text altered")
    if admin:
        check("/help (admin) has a second embed with the admin list",
              len(descs) == 2 and "`/say [text]`" in descs[1], len(descs))
    else:
        check("/help (everyone) has one embed and no admin commands",
              len(descs) == 1 and "`/say" not in descs[0] and "Admin only" not in descs[0],
              len(descs))
check("/help leaves headroom: full text under 6000 minus a 1500 margin",
      len(HELP_EVERYONE) + len(HELP_ADMIN) < 6000 - 1500,
      len(HELP_EVERYONE) + len(HELP_ADMIN))

finish("ALL SLASH TESTS PASSED")
