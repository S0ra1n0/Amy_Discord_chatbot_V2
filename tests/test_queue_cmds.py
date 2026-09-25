import asyncio
import io
import os
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
# Resolve the project root from this file, so the suite runs from any checkout
PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _fakes import (Guild, Interaction, Member, RealVoiceChannel, VoiceChannel, VoiceClient,
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
# ---- Snapshot / restore ---------------------------------------------------------------
# A restart used to lose the whole queue. snapshot_player writes the current track into
# slot 0 so a restore knows where to pick up, and restore_player puts it all back.
import tempfile as _tf
from database import ConversationDB as _DB

_snap_db = _DB(_tf.mktemp(suffix=".db"))
_real_db = amy.db
amy.db = _snap_db
try:
    p = seed_queue(amy, 4242, ["one", "two", "three"], current="playing now")
    p.loop_mode = amy.LoopMode.QUEUE
    p.volume = 0.35
    p.text_channel_id = 888
    p.mark_started(0.0)

    amy.snapshot_player(4242)
    st = _snap_db.load_player_state(4242)
    check("a snapshot was written", st is not None)
    titles = [t["title"] for t in st["tracks"]]
    check("current track occupies slot 0", titles[0] == "playing now", titles)
    check("the queue follows in order", titles[1:] == ["one", "two", "three"], titles)
    check("loop mode saved", st["loop_mode"] == "queue", st["loop_mode"])
    check("volume saved", abs(st["volume"] - 0.35) < 1e-9, st["volume"])
    check("text channel saved", st["text_channel_id"] == 888, st["text_channel_id"])

    # Restoring into a fresh player must rebuild the same running order.
    amy.music_manager.cleanup(4242)
    g = Guild(owner_id=1, member=None, guild_id=4242)
    restored = asyncio.run(amy.restore_player(g))
    p2 = amy.music_manager.player_for(4242)
    check("restore reported success", restored is True)
    check("every track came back",
          [t.title for t in p2.queue] == ["playing now", "one", "two", "three"],
          [t.title for t in p2.queue])
    check("loop mode came back", p2.loop_mode is amy.LoopMode.QUEUE, p2.loop_mode)
    check("volume came back", abs(p2.volume - 0.35) < 1e-9, p2.volume)
    check("snapshot consumed once restored", _snap_db.load_player_state(4242) is None)

    # An empty player clears the snapshot rather than saving something to restore later.
    amy.music_manager.cleanup(4242)
    amy.snapshot_player(4242)
    check("empty player saves nothing", _snap_db.load_player_state(4242) is None)
    check("nothing to restore returns False",
          asyncio.run(amy.restore_player(Guild(owner_id=1, member=None, guild_id=4242))) is False)

    # --- the resume path, which used to be reachable only by restarting the bot ---
    # restore_player narrows with isinstance(channel, discord.VoiceChannel), so a duck-typed
    # fake was invisible to it and this branch had no test. RealVoiceChannel subclasses the
    # real class, so the narrowing passes and the whole path can run offline.
    joined = []
    advanced = []
    real_connect, real_advance = amy.voice.connect_to, amy.advance_playback

    async def fake_connect(channel):
        joined.append(channel.name)
        return None

    async def fake_advance(g):
        advanced.append(g.id)

    amy.voice.connect_to = fake_connect
    amy.advance_playback = fake_advance
    try:
        listener = Member(7, "listener")
        listener.bot = False

        # People still in the channel -> rejoin and pick up mid-track
        p3 = seed_queue(amy, 4343, ["a", "b"], current="mid track")
        p3.mark_started(0.0)
        amy.snapshot_player(4343)
        _snap_db.save_player_state(
            4343, [{"title": t, "query": "https://youtu.be/%s" % t} for t in
                   ("mid track", "a", "b")],
            "off", 1.0, voice_channel_id=77, text_channel_id=88, resume_position=42.0)

        amy.music_manager.cleanup(4343)
        g_busy = Guild(owner_id=1, member=None, guild_id=4343)
        g_busy.add_channel(RealVoiceChannel(77, "Lounge", [listener]))
        ok = asyncio.run(amy.restore_player(g_busy))
        p4 = amy.music_manager.player_for(4343)
        check("restored with people present", ok is True)
        check("rejoined the voice channel", joined == ["Lounge"], joined)
        check("started playing again", advanced == [4343], advanced)
        check("picked up mid-track", abs(p4.resume_position - 42.0) < 1e-9, p4.resume_position)
        check("queue is intact", [t.title for t in p4.queue] == ["mid track", "a", "b"],
              [t.title for t in p4.queue])

        # Empty channel -> restore the queue but stay out
        joined.clear(); advanced.clear()
        _snap_db.save_player_state(
            4344, [{"title": "x", "query": "https://youtu.be/x"}],
            "off", 1.0, voice_channel_id=78, text_channel_id=88, resume_position=9.0)
        g_empty = Guild(owner_id=1, member=None, guild_id=4344)
        g_empty.add_channel(RealVoiceChannel(78, "Empty", []))
        ok = asyncio.run(amy.restore_player(g_empty))
        p5 = amy.music_manager.player_for(4344)
        check("restored with nobody present", ok is True)
        check("did not rejoin an empty channel", joined == [], joined)
        check("did not start playing", advanced == [], advanced)
        check("queue still restored", [t.title for t in p5.queue] == ["x"],
              [t.title for t in p5.queue])
        check("no mid-track resume queued", p5.resume_position == 0.0, p5.resume_position)

        # Only bots left counts as empty
        joined.clear(); advanced.clear()
        robot = Member(8, "robot"); robot.bot = True
        _snap_db.save_player_state(
            4345, [{"title": "y", "query": "https://youtu.be/y"}],
            "off", 1.0, voice_channel_id=79, text_channel_id=88, resume_position=0.0)
        g_bots = Guild(owner_id=1, member=None, guild_id=4345)
        g_bots.add_channel(RealVoiceChannel(79, "Bots", [robot]))
        asyncio.run(amy.restore_player(g_bots))
        check("a channel of bots is not an audience", joined == [], joined)

        # Channel deleted while Amy was down
        joined.clear(); advanced.clear()
        _snap_db.save_player_state(
            4346, [{"title": "z", "query": "https://youtu.be/z"}],
            "off", 1.0, voice_channel_id=999, text_channel_id=88, resume_position=0.0)
        g_gone = Guild(owner_id=1, member=None, guild_id=4346)
        ok = asyncio.run(amy.restore_player(g_gone))
        check("missing channel still restores the queue", ok is True)
        check("missing channel does not rejoin", joined == [], joined)
        check("queue survived the missing channel",
              [t.title for t in amy.music_manager.player_for(4346).queue] == ["z"])
    finally:
        amy.voice.connect_to = real_connect
        amy.advance_playback = real_advance

    # A snapshot of tracks that can't be rebuilt must not leave a phantom entry behind.
    _snap_db.save_player_state(4242, [{"title": "", "query": ""}], "off", 1.0, None, None, 0.0)
    check("unreadable rows restore as nothing",
          asyncio.run(amy.restore_player(Guild(owner_id=1, member=None, guild_id=4242))) is False)
    check("and the snapshot is dropped", _snap_db.load_player_state(4242) is None)
finally:
    amy.db = _real_db
    _snap_db.conn.close()

print()
print("ALL QUEUE COMMAND TESTS PASSED")
