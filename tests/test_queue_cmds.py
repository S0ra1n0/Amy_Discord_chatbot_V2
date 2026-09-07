import io
import os
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
# Resolve the project root from this file, so the suite runs from any checkout
PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _fakes import (Guild, Interaction, Member, VoiceChannel, VoiceClient,
                    load_bot, run_music, seed_queue)

amy = load_bot()

OWNER, USER = 42, 99
GUILD = 500
CH = VoiceChannel(10, "Chung")

fails = []


def check(label, ok, got=""):
    print(("  PASS " if ok else "  FAIL ") + label)
    if not ok:
        fails.append(label)
        print("        got:", str(got)[:120])


def setup(author_id, name, titles, owners=None):
    """A guild with Amy connected and `titles` queued, plus a matching interaction."""
    amy.voice_cmd_store.clear()
    player = seed_queue(amy, GUILD, titles, owners)
    member = Member(author_id, CH, name=name)
    guild = Guild(OWNER, member, VoiceClient(CH), guild_id=GUILD)
    return player, Interaction(member, guild)


print("=== /queue pagination ===")
p, it = setup(USER, "userA", ["t%d" % i for i in range(1, 26)])
out = run_music(amy, "queue", [2], interaction=it)
check("page 2 shows absolute positions", "`11.`" in out and "`20.`" in out, out)
p, it = setup(USER, "userA", ["t%d" % i for i in range(1, 26)])
check("out-of-range page clamps", "page 3/3" in run_music(amy, "queue", [99], interaction=it))

print()
print("=== /remove ownership ===")
p, it = setup(USER, "userA", ["mine", "theirs"], owners={"mine": "userA", "theirs": "userB"})
out = run_music(amy, "remove", [2], interaction=it)
check("cannot remove another user's track", "only remove your own" in out, out)
check("queue untouched", len(p.queue) == 2)
out = run_music(amy, "remove", [1], interaction=it)
check("can remove own track", "Removed" in out and "mine" in out, out)
check("queue now 1", len(p.queue) == 1)

p, it = setup(OWNER, "ownerName", ["mine", "theirs"],
              owners={"mine": "userA", "theirs": "userB"})
out = run_music(amy, "remove", [2], interaction=it)
check("admin removes anyone's track", "Removed" in out and "theirs" in out, out)

p, it = setup(USER, "userA", ["a"])
check("bad position rejected", "no track at position" in run_music(amy, "remove", [9], interaction=it))

print()
print("=== /shuffle ===")
p, it = setup(USER, "userA", ["a", "b", "c", "d"])
check("shuffles", "Shuffled" in run_music(amy, "shuffle", interaction=it))
check("membership preserved", sorted(t.title for t in p.queue) == ["a", "b", "c", "d"])
p, it = setup(USER, "userA", ["only"])
check("refuses with <2 tracks", "Not enough" in run_music(amy, "shuffle", interaction=it))

print()
print("=== /clearqueue ===")
p, it = setup(OWNER, "ownerName", ["a", "b"])
check("admin clears", "Cleared" in run_music(amy, "clearqueue", interaction=it))
check("queue emptied", len(p.queue) == 0)
check("current track kept", p.current is not None)
check("empty queue reports so", "already empty" in run_music(amy, "clearqueue", interaction=it))

print()
print("=== /skipto ===")
p, it = setup(USER, "userA", ["a", "b", "c", "d"])
out = run_music(amy, "skipto", [3], interaction=it)
check("skips to position", "Skipping to" in out and "c" in out, out)
check("dropped tracks before it", [t.title for t in p.queue] == ["c", "d"],
      [t.title for t in p.queue])
check("flagged the skip so TRACK loop can't swallow it", p.skip_requested)
check("triggered stop()", it.guild.voice_client.stopped)
p, it = setup(USER, "userA", ["a"])
check("bad position rejected", "no track at position" in run_music(amy, "skipto", [9], interaction=it))

print()
if fails:
    print("%d FAILED" % len(fails))
    sys.exit(1)
print("ALL QUEUE COMMAND TESTS PASSED")
