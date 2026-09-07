import io, os, sys, asyncio, importlib.util
from collections import deque
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
# Resolve the project root from this file, so the suite runs from any checkout
PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJ); os.chdir(PROJ)

spec = importlib.util.spec_from_file_location("amy", os.path.join(PROJ, "Amy_chatbot_V2.py"))
amy = importlib.util.module_from_spec(spec); sys.modules["amy"] = amy
spec.loader.exec_module(amy)

# Mocks aren't real discord objects, so duck-type the narrowing helpers
amy.voice.get_voice_client = lambda g: g.voice_client
amy.voice.active_channel = lambda vc: None if (vc is None or not vc.is_connected()) else vc.channel

class Perms:
    def __init__(s, **k):
        s.connect = s.speak = s.manage_channels = True
        s.administrator = k.get("administrator", False)
class VC:
    def __init__(s, id_, name, members=()): s.id, s.name, s.members = id_, name, list(members)
    def permissions_for(s, w): return Perms()
class VoiceClient:
    def __init__(s, ch): s.channel, s.stopped = ch, False
    def is_connected(s): return True
    def is_playing(s): return True
    def is_paused(s): return False
    def stop(s): s.stopped = True
class Member:
    def __init__(s, id_, ch=None, bot=False):
        s.id, s.bot, s.roles, s.guild_permissions = id_, bot, [], Perms()
        s.voice = type("VS", (), {"channel": ch})() if ch else None
class Guild:
    def __init__(s, owner, member, vcl=None):
        s.id, s.owner_id, s._m, s.voice_client = 500, owner, member, vcl
        s.me = type("Me", (), {"guild_permissions": Perms()})()
    def get_member(s, uid): return s._m if s._m and s._m.id == uid else None
class Msg:
    def __init__(s, author_id, guild, name):
        s.author = type("A", (), {"id": author_id, "__str__": lambda self: name})()
        s.guild, s.channel = guild, type("C", (), {"id": 77, "category": None})()

OWNER, USER, OTHER = 42, 99, 77
ch = VC(10, "Chung")

def setup(author_id, name, queue_titles, owners=None):
    """Build a guild with Amy connected and `queue_titles` queued."""
    amy.voice_cmd_store.clear()
    amy.music_manager.cleanup(500)
    p = amy.music_manager.player_for(500)
    owners = owners or {}
    p.queue = deque([amy.music.Track(title=t, query=t, duration=60,
                                     requested_by=owners.get(t, "userA"))
                     for t in queue_titles])
    p.current = amy.music.Track(title="playing", query="p", duration=90, requested_by="userA")
    m = Member(author_id, ch)
    g = Guild(OWNER, m, VoiceClient(ch))
    return p, Msg(author_id, g, name)

def run(cmd, msg): return text_of(asyncio.run(amy.execute_command(cmd, msg)))

def text_of(resp):
    """Flatten a handler result (str or ui.Reply) into searchable text."""
    if isinstance(resp, str):
        return resp
    parts = [resp.content or ""]
    e = resp.embed
    if e is not None:
        parts += [e.title or "", e.description or ""]
        if e.author:
            parts.append(e.author.name or "")
        for f in e.fields:
            parts += [f.name or "", str(f.value or "")]
    return chr(10).join(parts)



fails = []
def check(label, ok, got=""):
    print(("  PASS " if ok else "  FAIL ") + label)
    if not ok:
        fails.append(label); print("        got:", got[:110])

print("=== /queue pagination ===")
p, m = setup(USER, "userA", ["t%d" % i for i in range(1, 26)])
out = run("queue 2", m); check("page 2 shows absolute positions", "`11.`" in out and "`20.`" in out, out)
out = run("queue 99", m); check("out-of-range clamps", "page 3/3" in out, out)
out = run("queue abc", m); check("non-numeric page rejected", "must be a number" in out, out)

print()
print("=== /remove ownership ===")
p, m = setup(USER, "userA", ["mine", "theirs"], owners={"mine": "userA", "theirs": "userB"})
out = run("remove 2", m); check("cannot remove another user's track", "only remove your own" in out, out)
check("queue untouched", len(p.queue) == 2)
out = run("remove 1", m); check("can remove own track", "Removed" in out and "mine" in out, out)
check("queue now 1", len(p.queue) == 1)

p, m = setup(OWNER, "ownerName", ["mine", "theirs"], owners={"mine": "userA", "theirs": "userB"})
out = run("remove 2", m); check("admin removes anyone's track", "Removed" in out and "theirs" in out, out)

p, m = setup(USER, "userA", ["a"])
out = run("remove 9", m); check("bad position rejected", "no track at position" in out, out)
out = run("remove", m);   check("missing arg rejected", "Usage" in out, out)

print()
print("=== /shuffle ===")
p, m = setup(USER, "userA", ["a", "b", "c", "d"])
out = run("shuffle", m); check("shuffles", "Shuffled" in out, out)
check("membership preserved", sorted(t.title for t in p.queue) == ["a", "b", "c", "d"])
p, m = setup(USER, "userA", ["only"])
out = run("shuffle", m); check("refuses with <2 tracks", "Not enough" in out, out)

print()
print("=== /clearqueue is admin only ===")
p, m = setup(USER, "userA", ["a", "b"])
out = run("clearqueue", m); check("non-admin refused", "permission" in out, out)
check("queue intact after refusal", len(p.queue) == 2)
p, m = setup(OWNER, "ownerName", ["a", "b"])
out = run("clearqueue", m); check("admin clears", "Cleared" in out, out)
check("queue emptied", len(p.queue) == 0)
check("current track kept", p.current is not None)
out = run("clearqueue", m); check("empty queue reports so", "already empty" in out, out)

print()
print("=== /skipto ===")
p, m = setup(USER, "userA", ["a", "b", "c", "d"])
out = run("skipto 3", m)
check("skips to position", "Skipping to" in out and "c" in out, out)
check("dropped tracks before it", [t.title for t in p.queue] == ["c", "d"], str([t.title for t in p.queue]))
check("triggered stop()", m.guild.voice_client.stopped)
p, m = setup(USER, "userA", ["a"])
out = run("skipto 9", m); check("bad position rejected", "no track at position" in out, out)

print()
if fails:
    print("%d FAILED" % len(fails)); sys.exit(1)
print("ALL QUEUE COMMAND TESTS PASSED")
