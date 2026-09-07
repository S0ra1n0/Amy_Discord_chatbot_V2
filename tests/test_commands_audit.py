import io, os, sys, time, asyncio, importlib.util
from collections import deque
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
# Resolve the project root from this file, so the suite runs from any checkout
PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJ); os.chdir(PROJ)
spec = importlib.util.spec_from_file_location("amy", os.path.join(PROJ, "Amy_chatbot_V2.py"))
amy = importlib.util.module_from_spec(spec); sys.modules["amy"] = amy
spec.loader.exec_module(amy)
amy.voice.get_voice_client = lambda g: g.voice_client
amy.voice.active_channel = lambda vc: None if (vc is None or not vc.is_connected()) else vc.channel

fails = []
def check(label, ok, got=""):
    print(("  PASS " if ok else "  FAIL ") + label)
    if not ok: fails.append(label); print("        got:", str(got)[:130])

class Perms:
    connect = speak = manage_channels = True
    administrator = False
class VC:
    def __init__(s, i, n): s.id, s.name, s.members = i, n, []
    def permissions_for(s, w): return Perms()
class VoiceClient:
    def __init__(s, ch): s.channel, s.stopped, s.disconnected = ch, False, False
    def is_connected(s): return not s.disconnected
    def is_playing(s): return not s.stopped
    def is_paused(s): return False
    def stop(s): s.stopped = True
    async def disconnect(s, force=False): s.disconnected = True
class Member:
    def __init__(s, i, ch=None):
        s.id, s.bot, s.roles, s.guild_permissions = i, False, [], Perms()
        s.voice = type("VS", (), {"channel": ch})() if ch else None
class Guild:
    def __init__(s, owner, m, vcl=None):
        s.id, s.owner_id, s._m, s.voice_client = 500, owner, m, vcl
        s.me = type("M", (), {"guild_permissions": Perms()})()
    def get_member(s, u): return s._m if s._m and s._m.id == u else None
class Msg:
    def __init__(s, uid, g):
        s.author = type("A", (), {"id": uid, "__str__": lambda self: "u"})()
        s.guild, s.channel = g, type("C", (), {"id": 7, "category": None})()

def text_of(r):
    if isinstance(r, str): return r
    parts = [r.content or ""]
    if r.embed is not None:
        parts += [r.embed.title or "", r.embed.description or ""]
        if r.embed.author: parts.append(r.embed.author.name or "")
        for f in r.embed.fields: parts += [f.name or "", str(f.value or "")]
    return chr(10).join(parts)

def run(cmd, msg):
    amy.voice_cmd_store.clear()
    return text_of(asyncio.run(amy.execute_command(cmd, msg)))

OWNER, USER = 42, 99
g_plain = Guild(OWNER, Member(USER))

print("=== /dice freeze bug is fixed ===")
t0 = time.time()
out = run("dice 6 1000000000", Msg(USER, g_plain))
dt = time.time() - t0
check("huge amount refused", "Amount must be between 1 and %d" % amy.MAX_DICE_AMOUNT in out, out)
check("refused instantly (was ~300s)", dt < 1.0, "%.2fs" % dt)
check("huge sides refused",
      "Sides must be between 1 and %d" % amy.MAX_DICE_SIDES in run("dice 999999999", Msg(USER, g_plain)))

t0 = time.time()
out = run("dice 6 %d" % amy.MAX_DICE_AMOUNT, Msg(USER, g_plain))
check("max allowed roll is fast", (time.time() - t0) < 0.5)
check("max allowed roll succeeds", "Rolled %d" % amy.MAX_DICE_AMOUNT in out, out)

print()
print("=== /dice boundaries and output ===")
check("amount 0 refused", "Amount must be" in run("dice 6 0", Msg(USER, g_plain)))
check("negative amount refused", "Amount must be" in run("dice 6 -5", Msg(USER, g_plain)))
check("sides 0 refused", "Sides must be" in run("dice 0", Msg(USER, g_plain)))
check("non-numeric refused", "must be integers" in run("dice abc", Msg(USER, g_plain)))
check("bare /dice works (replaces /dice1)", "Rolled 1d6" in run("dice", Msg(USER, g_plain)))
out = run("dice 6 3", Msg(USER, g_plain))
check("multi-roll shows each value (replaces /dice2)",
      out.count("**") >= 8 and "+" in out and "=" in out, out)
check("single roll has no breakdown", "+" not in run("dice 20", Msg(USER, g_plain)))

print()
print("=== deleted commands are gone ===")
for gone in ("dice1", "dice2", "clear"):
    check("/%s no longer recognised" % gone,
          "Unknown command" in run(gone, Msg(USER, g_plain)), run(gone, Msg(USER, g_plain)))

print()
print("=== /stop stops but stays connected ===")
ch = VC(10, "Chung")
m = Member(USER, ch)
vcl = VoiceClient(ch)
g = Guild(OWNER, m, vcl)
amy.music_manager.cleanup(500)
p = amy.music_manager.player_for(500)
p.queue = deque([amy.music.Track(title="t%d" % i, query="q", duration=60, requested_by="u")
                 for i in range(3)])
p.current = amy.music.Track(title="playing", query="q", duration=60, requested_by="u")
p.loop_mode = amy.LoopMode.QUEUE
out = run("stop", Msg(USER, g))
check("queue cleared", len(p.queue) == 0, len(p.queue))
check("loop reset", p.loop_mode is amy.LoopMode.OFF, p.loop_mode)
check("playback stopped", vcl.stopped)
check("STILL CONNECTED (the behaviour change)", not vcl.disconnected)
check("reply mentions staying", "stay" in out.lower(), out)
check("reply points at /leave", "/leave" in out, out)

print()
if fails:
    print("%d CHECK(S) FAILED" % len(fails)); sys.exit(1)
print("ALL COMMAND AUDIT TESTS PASSED")
