"""Verify the local-file sandbox actually contains /play."""
import asyncio, io, os, sys, tempfile
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
# Resolve the project root from this file, so the suite runs from any checkout
PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJ)
os.chdir(PROJ)
import music

fails = []
def check(label, ok):
    print(("  PASS " if ok else "  FAIL ") + label)
    if not ok:
        fails.append(label)

# --- with MUSIC_DIR unset, local playback must be completely off ---
os.environ.pop("MUSIC_DIR", None)
print("=== MUSIC_DIR unset -> local playback disabled ===")
check(".env not reachable", music.resolve_local_path(os.path.join(PROJ, ".env")) is None)
check("hosts file not reachable",
      music.resolve_local_path(r"C:\Windows\System32\drivers\etc\hosts") is None)
check("get_music_dir() is None", music.get_music_dir() is None)

t = asyncio.run(music.resolve_metadata.__wrapped__(".env", "x")) if hasattr(
    music.resolve_metadata, "__wrapped__") else None

# --- with MUSIC_DIR set, only files inside it are reachable ---
base = tempfile.mkdtemp(prefix="amy_music_")
inside = os.path.join(base, "song.mp3")
open(inside, "wb").close()
sub = os.path.join(base, "sub")
os.makedirs(sub, exist_ok=True)
nested = os.path.join(sub, "deep.mp3")
open(nested, "wb").close()
os.environ["MUSIC_DIR"] = base

print()
print("=== MUSIC_DIR set -> only files inside are reachable ===")
check("file in music dir resolves", music.resolve_local_path("song.mp3") == os.path.realpath(inside))
check("nested file resolves", music.resolve_local_path(r"sub\deep.mp3") == os.path.realpath(nested))
check("absolute path inside dir resolves", music.resolve_local_path(inside) == os.path.realpath(inside))
check("missing file in dir -> None", music.resolve_local_path("nope.mp3") is None)

print()
print("=== escape attempts must all be rejected ===")
attacks = [
    r"..\..\..\Windows\System32\drivers\etc\hosts",
    os.path.join(PROJ, ".env"),
    r"C:\Windows\System32\drivers\etc\hosts",
    "../" * 12 + "Windows/System32/drivers/etc/hosts",
    os.path.join(base, "..", os.path.basename(PROJ), ".env"),
]
for a in attacks:
    got = music.resolve_local_path(a)
    check("rejected: %s" % (a[:52]), got is None)

# sibling directory with a prefix-matching name must not be reachable
sibling = base + "_evil"
os.makedirs(sibling, exist_ok=True)
evil = os.path.join(sibling, "x.mp3")
open(evil, "wb").close()
check("prefix-similar sibling dir rejected", music.resolve_local_path(evil) is None)

print()
print("=== resolve_metadata honours the sandbox ===")
async def main():
    tr = await music.resolve_metadata("song.mp3", "tester")
    check("in-dir file -> local Track", tr.is_local and tr.title == "song.mp3")
asyncio.run(main())

# cleanup
import shutil
os.environ.pop("MUSIC_DIR", None)
shutil.rmtree(base, ignore_errors=True)
shutil.rmtree(sibling, ignore_errors=True)

# --- review L8: a local track is re-checked against the sandbox when it PLAYS ---
# A track saved in the queue snapshot is rebuilt at startup and handed to FFmpeg. If
# MUSIC_DIR had since been unset or moved, the stored absolute path still played - the
# check only happened when the track was first queued.
print()
print("=== local tracks are re-checked at play time ===")
_play_dir = tempfile.mkdtemp(prefix="amy_play_")
_song = os.path.join(_play_dir, "song.mp3")
open(_song, "wb").close()
os.environ["MUSIC_DIR"] = _play_dir
_inside = music.Track(title="song.mp3", query=os.path.realpath(_song), is_local=True)
check("a track inside MUSIC_DIR still plays",
      asyncio.run(music.resolve_stream_url(_inside)) == os.path.realpath(_song))
_forged = music.Track(title=".env", query=os.path.join(PROJ, ".env"), is_local=True)
try:
    asyncio.run(music.resolve_stream_url(_forged))
    check("a stored path outside MUSIC_DIR is refused", False)
except Exception:
    check("a stored path outside MUSIC_DIR is refused", True)
os.environ.pop("MUSIC_DIR", None)
try:
    asyncio.run(music.resolve_stream_url(_inside))
    check("with MUSIC_DIR unset, a previously valid local track is refused", False)
except Exception:
    check("with MUSIC_DIR unset, a previously valid local track is refused", True)

# --- review M10: nothing Amy sends may ping @everyone, a role, or an arbitrary user ---
# discord.py sends no mention restriction unless the client sets one, and Discord then
# parses everything. Amy repeats text she doesn't control - model replies, which can be
# steered by fetched web pages or simply by asking; YouTube titles in "Loading **...**" -
# so any of it could mass-ping the server.
print()
print("=== no text Amy sends can ping everyone, roles or users ===")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _fakes import assert_isolated_db, isolate_db
isolate_db()
import importlib.util
import discord
from discord.http import handle_message_parameters
_spec = importlib.util.spec_from_file_location("amy", os.path.join(PROJ, "Amy_chatbot_V2.py"))
amy = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(amy)
assert_isolated_db(amy)

am = amy.bot.allowed_mentions
check("the bot sets a client-wide mention policy", am is not None)
if am is not None:
    check("@everyone and @here never ping", am.everyone is False)
    check("roles never ping", am.roles is False)
    check("arbitrary users never ping", am.users is False)
    check("replying to someone still notifies them, as before", am.replied_user is True)

    hostile = "@everyone @here <@123456789> <@!987654321> <@&555> read this"
    sent = handle_message_parameters(content=hostile, previous_allowed_mentions=am)
    wire = sent.payload.get("allowed_mentions") or {}
    check("on the wire, Discord is told to parse no mentions at all", wire.get("parse") == [])
    check("and no user or role ids are whitelisted",
          not wire.get("users") and not wire.get("roles"))

    # Slash-command follow-ups (the "Loading **<title>**..." line) are sent through an
    # interaction webhook, a different code path from channel messages. Build one exactly
    # as discord.Interaction.followup does and check it inherits the same policy.
    hook = discord.Webhook.from_state(
        data={"id": "1", "type": 3, "token": "t", "application_id": "1"},
        state=amy.bot._connection)
    check("slash-command follow-ups inherit the policy",
          getattr(hook._state, "allowed_mentions", None) is am)

print()
if fails:
    print("%d CHECK(S) FAILED" % len(fails))
    sys.exit(1)
print("ALL SECURITY CHECKS PASSED")
