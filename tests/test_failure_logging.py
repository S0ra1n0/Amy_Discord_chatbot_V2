"""
test_failure_logging: the error-handling rule (ARCHITECTURE.md, "Error handling"): a boundary catch-all logs what it
EXPECTED in one line and anything else with a traceback; inner code catches only the
exceptions it expects, so a bug surfaces instead of being swallowed.
"""
import asyncio
import io
import logging
import os
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _check import check, finish
from _fakes import Guild, Interaction, Member, VoiceChannel, VoiceClient, load_bot, run_music

amy = load_bot()
import logs
from yt_dlp.utils import DownloadError


class Capture(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.records = []

    def emit(self, record):
        self.records.append(record)


cap = Capture()
logging.getLogger("amy").addHandler(cap)


def last():
    return cap.records[-1] if cap.records else None


print("=== log_failure ===")
cap.records.clear()
logs.log_failure("Lookup", DownloadError("bad link"), amy.music.lookup_errors())
r = last()
check("an expected failure is one line", r is not None and r.exc_info is None, r)
check("  ...at ERROR by default", r is not None and r.levelno == logging.ERROR)
check("  ...and says what happened", r is not None and "bad link" in r.getMessage())

cap.records.clear()
logs.log_failure("Lookup", AttributeError("oops"), amy.music.lookup_errors(), logging.WARNING)
r = last()
check("a bug carries its traceback", r is not None and r.exc_info is not None
      and r.exc_info[0] is AttributeError, r)
check("  ...at the level asked for", r is not None and r.levelno == logging.WARNING)

cap.records.clear()
logs.log_failure("Autoplay", asyncio.TimeoutError(), amy.music.lookup_errors(), logging.WARNING)
r = last()
check("a TimeoutError (empty str) still names its type",
      r is not None and "TimeoutError" in r.getMessage() and r.exc_info is None, r and r.getMessage())

print()
print("=== the type tuples ===")
check("lookup errors cover yt-dlp, ValueError, OSError, timeouts",
      issubclass(DownloadError, amy.music.lookup_errors())
      and all(t in amy.music.lookup_errors() for t in (ValueError, OSError, asyncio.TimeoutError)))
check("voice errors add RuntimeError (no PyNaCl) to the Discord ones",
      RuntimeError in amy.VOICE_ERRORS and RuntimeError not in amy.DISCORD_ERRORS)
check("an aiohttp connect failure counts as a Discord error", OSError in amy.DISCORD_ERRORS)

print()
print("=== /search: the user is answered whatever fails ===")
CH = VoiceChannel(10, "Chung")
real_search, real_ffmpeg = amy.music.search_tracks, amy.music.find_ffmpeg
amy.music.find_ffmpeg = lambda: "ffmpeg"


def search_with(exc):
    async def _boom(query, requested_by="?"):
        raise exc
    amy.music.search_tracks = _boom
    member = Member(99, CH, name="userA")
    it = Interaction(member, Guild(42, member, VoiceClient(CH), guild_id=500))
    cap.records.clear()
    text = run_music(amy, "search", ["rick"], interaction=it)
    status = it.sent[0]
    return text, status


try:
    text, status = search_with(DownloadError("no such video"))
    check("a yt-dlp error shows the friendly message",
          status.embed is not None and "went wrong" in (status.embed.description or ""))
    check("  ...logged without a traceback",
          last() is not None and last().exc_info is None, last())

    text, status = search_with(AttributeError("a real bug"))
    check("a bug still shows the friendly message",
          status.embed is not None and "went wrong" in (status.embed.description or ""))
    check("  ...but is logged with its traceback",
          last() is not None and last().exc_info is not None, last())
finally:
    amy.music.search_tracks, amy.music.find_ffmpeg = real_search, real_ffmpeg

print()
print("=== _on_hold_done: only a closing loop is absorbed ===")


class FakeLoop:
    def __init__(self, exc):
        self.exc = exc

    def call_soon_threadsafe(self, fn):
        raise self.exc


real_loop = amy.bot.loop
try:
    amy.bot.loop = FakeLoop(RuntimeError("Event loop is closed"))
    cap.records.clear()
    try:
        amy._on_hold_done(500)
        ok = True
    except Exception:
        ok = False
    check("a closed loop is absorbed", ok)
    check("  ...with a warning", last() is not None and last().levelno == logging.WARNING)

    amy.bot.loop = FakeLoop(AttributeError("a real bug"))
    try:
        amy._on_hold_done(500)
        raised = False
    except AttributeError:
        raised = True
    check("a bug is not swallowed", raised)
finally:
    amy.bot.loop = real_loop

# Real shutdown state: after Client.close() the loop is the MISSING sentinel, whose attributes
# raise AttributeError - that must not escape into the audio thread (Mixer.read).
import discord
try:
    amy.bot.loop = discord.utils.MISSING
    cap.records.clear()
    try:
        amy._on_hold_done(500)
        ok = True
    except Exception:
        ok = False
    check("a MISSING loop (after close) returns quietly", ok)
    check("  ...without a warning", not cap.records)
finally:
    amy.bot.loop = real_loop

print()
print("=== /status: only an unreachable Ollama reads as Unavailable ===")
real_list = amy.ollama.list
member = Member(99, CH, name="userA")
guild = Guild(42, member, VoiceClient(CH), guild_id=500)
guild.name = "g"
guild.member_count = 1


def status_text(exc):
    def _list():
        raise exc
    amy.ollama.list = _list
    return asyncio.run(amy.build_status(Interaction(member, guild)))


try:
    check("a refused connection reads as Unavailable",
          "Unavailable" in status_text(ConnectionError("refused")))
    try:
        not_ollama = "Unavailable" in status_text(ValueError("not json"))
    except ValueError:
        not_ollama = False
    check("a 200 that is not Ollama (ValueError) reads as Unavailable", not_ollama)
    try:
        status_text(AttributeError("a real bug"))
        raised = False
    except AttributeError:
        raised = True
    check("a bug in the check is not reported as Unavailable", raised)
finally:
    amy.ollama.list = real_list

finish("ALL FAILURE-LOGGING TESTS PASSED")
