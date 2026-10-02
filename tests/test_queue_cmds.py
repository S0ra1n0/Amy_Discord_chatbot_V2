import asyncio
import io
import os
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
# Resolve the project root from this file, so the suite runs from any checkout
PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _fakes import (Guild, Interaction, Member, RealVoiceChannel, VoiceChannel, VoiceClient,
                    load_bot, make_track, run_music, run_voice, seed_queue, text_of)

amy = load_bot()

OWNER, USER = 42, 99
GUILD = 500
CH = VoiceChannel(10, "Chung")

from _check import check, finish
from discord import app_commands


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
        listener = Member(7, name="listener")

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
        check("mid-track position kept for when it is picked up",
              abs(p5.resume_position - 9.0) < 1e-9, p5.resume_position)

        # Only bots left counts as empty
        joined.clear(); advanced.clear()
        robot = Member(8, is_bot=True, name="robot")
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

# ---- Restore safety (review findings M2, M3, L3) ------------------------------------
# Each of these failed against the pre-fix code; see the review report for the scenarios.
_safe_db = _DB(_tf.mktemp(suffix=".db"))
_saved = {
    "db": amy.db, "connect": amy.voice.connect_to, "advance": amy.advance_playback,
    "ffmpeg": amy.music.find_ffmpeg, "resolve": amy.music.resolve_metadata,
    "get_guild": amy.bot.get_guild,
}
amy.db = _safe_db
_advanced = []


async def _record_advance(g):
    _advanced.append(g.id)
    p_ = amy.music_manager.player_for(g.id)
    if p_.current is None and p_.queue:          # behave like a real advance: pop the head
        p_.current = p_.queue.popleft()


amy.advance_playback = _record_advance


def _snapshot(gid, titles, channel_id=None, pos=0.0):
    _safe_db.save_player_state(
        gid, [{"title": t, "query": "https://youtu.be/%s" % t} for t in titles],
        "off", 1.0, voice_channel_id=channel_id, text_channel_id=88, resume_position=pos)


try:
    # M2: restore must never overwrite a player that is already in use. At startup a /play
    # can land while on_ready is still syncing commands; on a re-READY the player is live.
    live = seed_queue(amy, 4400, ["live1", "live2"], current="live now")
    _snapshot(4400, ["stale1", "stale2"])
    ok = asyncio.run(amy.restore_player(Guild(owner_id=1, member=None, guild_id=4400)))
    check("M2: restore declines a player that is already playing", ok is False, ok)
    check("M2: the live queue is untouched",
          [t.title for t in live.queue] == ["live1", "live2"], [t.title for t in live.queue])
    check("M2: the live track is untouched", live.current.title == "live now", live.current)
    check("M2: the stale snapshot is dropped", _safe_db.load_player_state(4400) is None)

    # Connected but idle also counts as in use - restore must not stack a queue onto it.
    amy.music_manager.cleanup(4401)
    g_conn = Guild(owner_id=1, member=None, guild_id=4401)
    g_conn.voice_client = VoiceClient(RealVoiceChannel(90, "Here", []))
    _snapshot(4401, ["stale"])
    ok = asyncio.run(amy.restore_player(g_conn))
    check("M2: restore declines while Amy is already in a voice channel", ok is False, ok)
    check("M2: nothing was queued onto the connected player",
          list(amy.music_manager.player_for(4401).queue) == [])

    # M2: restoring is a once-per-process event. discord.py fires READY again after a
    # failed RESUME, and by then the snapshot holds the track that is currently playing.
    amy._queues_restored = False
    fakes = {4402: Guild(owner_id=1, member=None, guild_id=4402)}
    amy.bot.get_guild = lambda gid: fakes.get(gid)
    amy.music_manager.cleanup(4402)
    _snapshot(4402, ["first"])
    asyncio.run(amy.restore_saved_queues())
    first_run = [t.title for t in amy.music_manager.player_for(4402).queue]
    amy.music_manager.cleanup(4402)
    _snapshot(4402, ["second"])
    asyncio.run(amy.restore_saved_queues())
    check("M2: the first READY restores", first_run == ["first"], first_run)
    check("M2: a later READY restores nothing",
          list(amy.music_manager.player_for(4402).queue) == [],
          [t.title for t in amy.music_manager.player_for(4402).queue])

    # M3: /join must pick up a queue that is waiting with nothing playing. The docs promised
    # this; the code only connected and went quiet.
    _join_guild = {}

    async def _connect_and_attach(channel):
        g_ = _join_guild["g"]
        g_.voice_client = VoiceClient(channel)
        # A real client is idle the moment it connects. The shared fake defaults to
        # "playing" because other suites use a fresh one to mean "Amy is mid-song".
        g_.voice_client.stopped = True
        return g_.voice_client

    amy.voice.connect_to = _connect_and_attach

    joiner = Member(10, VoiceChannel(91, "Joinable"), name="joiner")
    g_join = Guild(OWNER, joiner, None, guild_id=4403)
    _join_guild["g"] = g_join
    amy.music_manager.cleanup(4403)
    backlog = seed_queue(amy, 4403, ["waiting1", "waiting2"], current=None)
    _advanced.clear()
    out = run_voice(amy, "join", user=joiner, guild=g_join)
    check("M3: /join starts a waiting queue", _advanced == [4403], _advanced)
    check("M3: /join says it is picking the queue up", "queued" in out.lower(), out)
    check("M3: playback begins with the head of the queue",
          backlog.current is not None and backlog.current.title == "waiting1", backlog.current)

    # ...and stays quiet when there is nothing waiting.
    joiner2 = Member(11, VoiceChannel(92, "Empty queue room"), name="j2")
    g_join2 = Guild(OWNER, joiner2, None, guild_id=4404)
    _join_guild["g"] = g_join2
    amy.music_manager.cleanup(4404)
    _advanced.clear()
    run_voice(amy, "join", user=joiner2, guild=g_join2)
    check("M3: /join with no queue does not start anything", _advanced == [], _advanced)

    # M3: /play while idle with a backlog must say where the track landed, not
    # "Loading <it>" while the backlog's head actually plays.
    CH_P = VoiceChannel(93, "Player room")
    asker = Member(12, CH_P, name="asker")
    idle_vc = VoiceClient(CH_P)
    idle_vc.stopped = True                          # connected, nothing playing
    g_play = Guild(OWNER, asker, idle_vc, guild_id=4405)
    amy.music_manager.cleanup(4405)
    pl = seed_queue(amy, 4405, ["restored1", "restored2"], current=None)
    amy.music.find_ffmpeg = lambda: "ffmpeg"

    async def _fake_resolve(query, requested_by="?"):
        return amy.music.Track(title="Requested Song", query="https://youtu.be/req",
                               duration=200, requested_by=requested_by)

    amy.music.resolve_metadata = _fake_resolve
    _advanced.clear()
    it_play = Interaction(asker, g_play)
    run_music(amy, "play", ["requested song"], interaction=it_play)
    status = it_play.sent[0]
    final_text = text_of(None, it_play)
    check("M3: /play with a backlog starts the backlog", _advanced == [4405], _advanced)
    check("M3: /play does not claim to be loading the requested track",
          "Loading **Requested Song**" not in final_text, final_text[:200])
    check("M3: /play reports the track as queued",
          status.embed is not None and (status.embed.author.name or "") == "Added to queue",
          status.embed.author.name if status.embed else status.content)
    pos = [f.value for f in status.embed.fields if f.name == "Position"] if status.embed else []
    check("M3: and at the right position, behind what is now playing",
          pos == [str(len(pl.queue))] and pl.queue[-1].title == "Requested Song",
          (pos, [t.title for t in pl.queue]))

    # A plain /play with nothing queued still starts the requested track straight away.
    idle_vc2 = VoiceClient(CH_P)
    idle_vc2.stopped = True
    g_play2 = Guild(OWNER, asker, idle_vc2, guild_id=4406)
    amy.music_manager.cleanup(4406)
    _advanced.clear()
    it_play2 = Interaction(asker, g_play2)
    run_music(amy, "play", ["requested song"], interaction=it_play2)
    check("M3: /play on an empty queue still starts immediately", _advanced == [4406], _advanced)
    check("M3: and still says it is loading that track",
          "Loading **Requested Song**" in text_of(None, it_play2), text_of(None, it_play2)[:200])

    # L3: leaving voice must forget the saved queue at once, not up to 20s later - otherwise a
    # restart in that window rejoins a channel Amy was told to leave and resumes the music.
    _snapshot(4407, ["should be forgotten"], channel_id=77)
    seed_queue(amy, 4407, ["x"], current="y")
    amy.cleanup_guild_music(4407)
    check("L3: leaving clears the saved queue", _safe_db.load_player_state(4407) is None)
    check("L3: leaving clears the in-memory player",
          4407 not in amy.music_manager.players, list(amy.music_manager.players))
    import inspect as _inspect
    _src = _inspect.getsource(amy)
    check("L3: every leave path uses the clearing cleanup",
          _src.count("on_cleanup=cleanup_guild_music") == 3
          and "on_cleanup=music_manager.cleanup" not in _src,
          (_src.count("on_cleanup=cleanup_guild_music"),
           _src.count("on_cleanup=music_manager.cleanup")))
finally:
    amy.db = _saved["db"]
    amy.voice.connect_to = _saved["connect"]
    amy.advance_playback = _saved["advance"]
    amy.music.find_ffmpeg = _saved["ffmpeg"]
    amy.music.resolve_metadata = _saved["resolve"]
    amy.bot.get_guild = _saved["get_guild"]
    _safe_db.conn.close()

# ---- L4: the periodic snapshot must not catch a half-finished track change ----------
# advance_playback pops the next track into a local variable and then spends seconds
# resolving it, with player.current still holding the finished track. A snapshot taken in
# that window saved neither, so a crash then lost the next track. The periodic task now
# skips a player whose lock is held; advance_playback snapshots itself once it's done.
_l4_db = _DB(_tf.mktemp(suffix=".db"))
_l4_real = amy.db
amy.db = _l4_db
try:
    busy = seed_queue(amy, 4500, ["after"], current="finishing")

    async def _tick_while_locked():
        async with busy.lock:
            await amy.snapshot_queues_task.coro()

    asyncio.run(_tick_while_locked())
    check("L4: no snapshot while a track change is in progress",
          _l4_db.load_player_state(4500) is None, _l4_db.load_player_state(4500))
    asyncio.run(amy.snapshot_queues_task.coro())
    st = _l4_db.load_player_state(4500)
    check("L4: the next tick saves it once the change is done",
          st is not None and [t["title"] for t in st["tracks"]] == ["finishing", "after"],
          st and [t["title"] for t in st["tracks"]])
finally:
    amy.db = _l4_real
    _l4_db.conn.close()
    amy.music_manager.cleanup(4500)

# ---- Playback wiring in the bot (review M6, L1, L2) ------------------------------------
# The engine is tested in test_music.py; these check the bot actually uses it.
_pw_db = _DB(_tf.mktemp(suffix=".db"))
_pw_saved = (amy.db, amy.music.restart_at, amy.music.play_track, amy.refresh_now_playing,
             amy.idle_disconnect)
amy.db = _pw_db
try:
    # L1: both stop paths must cancel a track that is still being prepared.
    p, it = setup(USER, "userA", ["a", "b"])
    gen = p.generation
    run_music(amy, "stop", interaction=it)
    check("L1: /stop cancels in-flight starts", p.generation == gen + 1, (gen, p.generation))
    import inspect as _insp
    _btn = _insp.getsource(amy.PlayerControls.stop_button)
    check("L1: the Stop button goes through stop_all as well",
          "stop_all()" in _btn and "queue.clear()" not in _btn, _btn[:160])

    # M6: a failed seek must say the track is still playing - because it now is.
    async def _seek_fails(*a, **kw):
        raise RuntimeError("yt-dlp hiccup")
    amy.music.restart_at = _seek_fails
    p, it = setup(USER, "userA", ["a"])
    p.current = amy.music.Track(title="Long song", query="https://youtu.be/long", duration=600, requested_by="userA")
    out = run_music(amy, "seek", ["1:30"], interaction=it)
    check("M6: a failed seek says the track is still playing",
          "still playing" in out, out)
    check("M6: a failed seek leaves the queue alone",
          [t.title for t in p.queue] == ["a"] and p.current.title == "Long song",
          ([t.title for t in p.queue], p.current))

    # L1: a seek overtaken by /stop says so instead of claiming to have jumped.
    async def _seek_stopped(*a, **kw):
        return False
    amy.music.restart_at = _seek_stopped
    p, it = setup(USER, "userA", [])
    p.current = amy.music.Track(title="Long song", query="https://youtu.be/long", duration=600, requested_by="userA")
    out = run_music(amy, "seek", ["1:30"], interaction=it)
    check("L1: a seek overtaken by /stop does not claim to have jumped",
          "stopped" in out.lower() and "Jumped" not in out, out)

    # L2: the refreshed card must reflect a paused track as paused.
    seen = {}

    async def _record_refresh(guild, stopped=False, paused=False):
        seen["paused"] = paused

    async def _seek_ok(vc, player, track, after, position):
        vc._paused = True                       # restart_at kept the track paused
        return True

    amy.refresh_now_playing = _record_refresh
    amy.music.restart_at = _seek_ok
    p, it = setup(USER, "userA", [])
    p.current = amy.music.Track(title="Long song", query="https://youtu.be/long", duration=600, requested_by="userA")
    it.guild.voice_client._paused = True
    run_music(amy, "seek", ["1:30"], interaction=it)
    check("L2: after seeking a paused track the card shows it paused",
          seen.get("paused") is True, seen)

    # L1: if advance_playback's start is overtaken by /stop, nothing interrupted a playing
    # source, so no after-callback tidies up - advance_playback must do it itself.
    async def _start_stopped(*a, **kw):
        return False

    async def _no_idle(guild):
        return None

    amy.music.play_track = _start_stopped
    amy.idle_disconnect = _no_idle
    seen.clear()
    p, it = setup(USER, "userA", ["next one"])
    p.current = None
    it.guild.voice_client.stopped = True        # between tracks: nothing playing
    asyncio.run(amy.advance_playback(it.guild))
    check("L1: an aborted start leaves nothing marked as playing", p.current is None, p.current)
    check("L1: an aborted start still shows the finished card", seen.get("paused") is False
          and "paused" in seen, seen)
    check("L1: an aborted start still starts the idle timer", p.idle_task is not None)
    if p.idle_task is not None:
        p.idle_task.cancel()
finally:
    (amy.db, amy.music.restart_at, amy.music.play_track, amy.refresh_now_playing,
     amy.idle_disconnect) = _pw_saved
    _pw_db.conn.close()
    amy.music_manager.cleanup(GUILD)

# ---- /seek and /replay command paths (review M7) ---------------------------------------
_m7_saved = (amy.music.restart_at, amy.music.play_track, amy.refresh_now_playing)
_targets = []


async def _record_restart(vc, player, track, after, position):
    _targets.append(position)
    return True


async def _quiet_refresh(guild, stopped=False, paused=False):
    return None


def _seekable(title="Long song", duration=600):
    return amy.music.Track(title=title, query="https://youtu.be/x", duration=duration,
                           requested_by="userA")


amy.music.restart_at = _record_restart
amy.refresh_now_playing = _quiet_refresh
try:
    for args, want_pos, label in [(["1:30"], 90.0, "m:ss"), (["90"], 90.0, "bare seconds"),
                                  (["1:02:03"], 3723.0, "h:mm:ss")]:
        p, it = setup(USER, "userA", [])
        p.current = _seekable(duration=7200)
        _targets.clear()
        out = run_music(amy, "seek", args, interaction=it)
        check("M7: /seek %s jumps to %ss" % (label, want_pos), _targets == [want_pos], _targets)
        check("M7: /seek %s confirms where it went" % label, "Jumped to" in out, out)

    p, it = setup(USER, "userA", [])
    p.current = _seekable()
    _targets.clear()
    out = run_music(amy, "replay", interaction=it)
    check("M7: /replay restarts from 0", _targets == [0.0], _targets)
    check("M7: /replay says so", "from the start" in out, out)

    for bad in ["abc", "1:75", "-5", ""]:
        p, it = setup(USER, "userA", [])
        p.current = _seekable()
        _targets.clear()
        out = run_music(amy, "seek", [bad], interaction=it)
        check("M7: /seek %r is refused without seeking" % bad,
              _targets == [] and "couldn't read" in out, (out, _targets))

    p, it = setup(USER, "userA", [])
    p.current = _seekable(duration=200)
    _targets.clear()
    out = run_music(amy, "seek", ["3:20"], interaction=it)
    check("M7: seeking to the very end is refused (that's /skip)",
          _targets == [] and "past the end" in out, (out, _targets))

    p, it = setup(USER, "userA", [])
    p.current = amy.music.Track(title="Odd", query="not-a-url", duration=200, requested_by="a")
    _targets.clear()
    out = run_music(amy, "seek", ["1:00"], interaction=it)
    check("M7: a track with no seekable source is refused",
          _targets == [] and "can't seek" in out, (out, _targets))

    p, it = setup(USER, "userA", [])
    p.current = None
    it.guild.voice_client.stopped = True
    _targets.clear()
    out = run_music(amy, "seek", ["1:00"], interaction=it)
    check("M7: /seek with nothing playing is refused",
          _targets == [] and "Nothing is playing" in out, (out, _targets))

    # The lock. Swapping sources stops the old one, and its after-callback calls
    # advance_playback - which, unblocked, would pop the next song off the queue and play
    # it instead. Simulate exactly that ordering: stop, fire the callback, take a while to
    # build the new source, then start it.
    _popped = []

    async def _would_play(vc, player, track, after, start_at=0.0):
        _popped.append(track.title)
        return True

    async def _racy_restart(vc, player, track, after, position):
        vc.stopped = True                                   # old source stopped...
        asyncio.get_running_loop().create_task(amy.advance_playback(it.guild))  # ...callback
        await asyncio.sleep(0.02)                           # the seconds-long resolve
        vc.stopped = False                                  # new source playing
        return True

    amy.music.restart_at = _racy_restart
    amy.music.play_track = _would_play
    p, it = setup(USER, "userA", ["next song", "after that"])
    p.current = _seekable()

    async def _drive():
        await amy.music_seek(amy.MusicContext.build(it, it.guild), "1:30")
        await asyncio.sleep(0.05)                           # let the callback finish
    asyncio.run(_drive())
    check("M7: a seek never lets the old track's callback skip ahead",
          _popped == [] and [t.title for t in p.queue] == ["next song", "after that"],
          (_popped, [t.title for t in p.queue]))
    check("M7: and the seeked track is still the current one",
          p.current is not None and p.current.title == "Long song", p.current)
finally:
    amy.music.restart_at, amy.music.play_track, amy.refresh_now_playing = _m7_saved
    amy.music_manager.cleanup(GUILD)

# ---- /websearch with no recency chosen does not filter (review L5) ----------------------
_ws_seen = []
_ws_real = amy.websearch.search


async def _record_search(query, limit=5, recency=None, with_content=True):
    _ws_seen.append(recency)
    return []

amy.websearch.search = _record_search
try:
    _cmd = amy.tree.get_command("websearch")
    _p, _it = setup(OWNER, "owner", [])
    asyncio.run(_cmd.callback(_it, "latest iphone", None))
    check("L5: /websearch with no recency asks for no filter", _ws_seen == ["any"], _ws_seen)

    class _Choice:
        value = "week"
    _ws_seen.clear()
    _p, _it = setup(OWNER, "owner", [])
    asyncio.run(_cmd.callback(_it, "latest iphone", _Choice()))
    check("L5: /websearch with a recency passes it through", _ws_seen == ["week"], _ws_seen)
finally:
    amy.websearch.search = _ws_real

# ---- the real slash callbacks, end to end through the typed handlers --------------------
# The music commands used to share one string dispatcher: slash arguments were turned back
# into text and re-parsed. Each command now has a typed handler, and the slash layer does the
# conversion (a Choice to a LoopMode, an optional volume level). These drive the registered
# callbacks themselves, so the wiring is covered, not just the handlers - and they cover the
# commands that had no direct test before the split: pause, resume, skip, nowplaying,
# volume and loop.
print()
print("=== slash callbacks -> typed handlers ===")


def slash(name, it, *args):
    asyncio.run(amy.tree.get_command(name).callback(it, *args))
    return text_of(None, it)


p, it = setup(USER, "userA", ["t1", "t2"])
p.started_at = 1000.0              # the clock only freezes once a track has started
vc = it.guild.voice_client
out = slash("pause", it)
check("/pause pauses and says so", vc.is_paused() and "Paused." in out, out)
check("/pause stops the position clock", p.paused_at is not None, p.paused_at)
out = slash("resume", Interaction(it.user, it.guild))
check("/resume resumes and says so", not vc.is_paused() and "Resumed." in out, out)
check("/resume restarts the clock", p.paused_at is None, p.paused_at)

p, it = setup(USER, "userA", ["t1"])
out = slash("skip", it)
check("/skip names the skipped track", "Skipped **playing**" in out, out)
check("/skip asks the queue to advance", p.skip_requested and it.guild.voice_client.stopped)

p, it = setup(USER, "userA", ["t1"])
out = slash("nowplaying", it)
check("/nowplaying shows the current track", "playing" in out and "Nothing is playing" not in out, out)
p, it = setup(USER, "userA", [])
p.current = None
check("/nowplaying with nothing playing says so",
      "Nothing is playing right now." in slash("nowplaying", it))

p, it = setup(USER, "userA", ["t1"])
out = slash("loop", it, app_commands.Choice(name="track", value="track"))
check("/loop converts the Choice to a LoopMode", p.loop_mode is amy.LoopMode.TRACK, p.loop_mode)
check("/loop confirms the mode", "Loop set to **track**" in out, out)

p, it = setup(OWNER, "owner", ["t1"])
out = slash("volume", it, None)
check("/volume with no level reports the current one", "Current volume: **100%**" in out, out)
p, it = setup(OWNER, "owner", ["t1"])
out = slash("volume", it, 40)
check("/volume 40 sets it", abs(p.volume - 0.4) < 1e-9 and "**40%**" in out, (p.volume, out))

p, it = setup(USER, "userA", ["t%d" % i for i in range(1, 26)])
check("/queue passes the page through", "`11.`" in slash("queue", it, 2))
p, it = setup(USER, "userA", ["mine", "also mine"])
out = slash("remove", it, 2)
check("/remove passes the position through",
      "also mine" in out and [t.title for t in p.queue] == ["mine"], out)
p, it = setup(USER, "userA", ["a", "b", "c"])
out = slash("skipto", it, 3)
check("/skipto passes the position through", "Skipping to **c**" in out, out)

# The shared permission rule, through the new context
stranger = Member(55, VoiceChannel(11, "elsewhere"), name="stranger")
p, it = setup(USER, "userA", ["t1"])
it_far = Interaction(stranger, Guild(OWNER, stranger, it.guild.voice_client, guild_id=GUILD))
check("someone outside Amy's channel can't pause",
      "You need to be in my voice channel" in slash("pause", it_far))
p, it = setup(USER, "userA", ["t1"])
it.guild.voice_client = None
check("a command needing a connection says Amy isn't in one",
      "I'm not in a voice channel." in slash("pause", it))
p, it = setup(USER, "userA", ["t1"])
it.guild.voice_client.disconnected = True     # a client object that has lost its connection
check("a disconnected voice client counts as not connected",
      "I'm not in a voice channel." in slash("pause", it))

# /play passes the query through untouched. The dispatcher split it on whitespace and joined
# it with single spaces, so a local file named "My  Song.mp3" could never be found.
_seen_queries = []
_real_resolve, _real_advance, _real_ffmpeg = (amy.music.resolve_metadata, amy.advance_playback,
                                               amy.music.find_ffmpeg)


async def _record_resolve(query, requested_by="?"):
    _seen_queries.append(query)
    return amy.music.Track(title="Found", query="https://youtu.be/f", duration=60,
                           requested_by=requested_by)


async def _no_advance(guild):
    return None

amy.music.resolve_metadata = _record_resolve
amy.advance_playback = _no_advance
amy.music.find_ffmpeg = lambda: "ffmpeg"
try:
    p, it = setup(USER, "userA", [])
    p.current = None
    it.guild.voice_client.stopped = True
    slash("play", it, "  My  Song   Name.mp3 ")
    check("/play keeps inner whitespace in the query",
          _seen_queries == ["My  Song   Name.mp3"], _seen_queries)
    p, it = setup(USER, "userA", [])
    check("/play with a blank query asks what to play",
          "What should I play?" in slash("play", it, "   "))
finally:
    amy.music.resolve_metadata, amy.advance_playback, amy.music.find_ffmpeg = (
        _real_resolve, _real_advance, _real_ffmpeg)

# ---- /previous and the Previous button, end to end ------------------------------------
print()
print("=== /previous ===")
_started = []
_real_play, _real_refresh = amy.music.play_track, amy.refresh_now_playing


async def _fake_play(vc, player, track, after, start_at=0.0):
    _started.append(track.title)
    player.current = track
    vc.stopped = False                      # the new source is playing
    return True


async def _quiet(*a, **kw):
    return None


def _drive(coro_fn):
    """Run a callback, then let the background advance it spawned finish."""
    async def go():
        await coro_fn()
        for _ in range(20):
            await asyncio.sleep(0.01)
    asyncio.run(go())


amy.music.play_track = _fake_play
amy.refresh_now_playing = _quiet
try:
    # Going back while a song plays: the previous one starts, the interrupted one is next.
    p, it = setup(USER, "userA", ["next up"])
    earlier = make_track(amy, "earlier song")
    p.history.append(earlier)
    out = slash("previous", it)
    check("/previous names the track it goes back to", "Going back to **earlier song**" in out, out)
    check("/previous stops the current source so the queue advances",
          it.guild.voice_client.stopped and p.back_requested)
    check("/previous queues it first, then the interrupted track",
          [t.title for t in p.queue] == ["earlier song", "playing", "next up"],
          [t.title for t in p.queue])
    _started.clear()
    asyncio.run(amy.advance_playback(it.guild))       # what vc.stop()'s callback does
    check("the advance plays the previous track", _started == ["earlier song"], _started)
    check("and doesn't record the interrupted one (it's queued, not done)",
          list(p.history) == [], [t.title for t in p.history])
    check("the go-back flag is consumed", p.back_requested is False)

    # Nothing playing - the queue ran out - replays the last track, in the background.
    p, it = setup(USER, "userA", [])
    p.current = None
    it.guild.voice_client.stopped = True
    p.history.append(make_track(amy, "last one"))
    _started.clear()
    _drive(lambda: amy.tree.get_command("previous").callback(it))
    check("with nothing playing, /previous replays the last track", _started == ["last one"],
          _started)

    p, it = setup(USER, "userA", ["t1"])
    check("no history says so",
          "There's no previous track" in slash("previous", it))
    check("and stops nothing", not it.guild.voice_client.stopped)

    stranger = Member(56, VoiceChannel(12, "elsewhere"), name="stranger")
    p, it = setup(USER, "userA", ["t1"])
    p.history.append(make_track(amy, "x"))
    it_far = Interaction(stranger, Guild(OWNER, stranger, it.guild.voice_client, guild_id=GUILD))
    check("someone outside Amy's channel can't go back",
          "You need to be in my voice channel" in slash("previous", it_far))
    check("...and the history is untouched", len(p.history) == 1)

    # The button shares go_back with the command.
    p, it = setup(USER, "userA", [])
    p.history.append(make_track(amy, "via button"))
    asyncio.run(amy.PlayerControls().previous_button.callback(it))
    check("the Previous button goes back too",
          [t.title for t in p.queue][:2] == ["via button", "playing"] and p.back_requested,
          [t.title for t in p.queue])
    p, it = setup(USER, "userA", [])
    asyncio.run(amy.PlayerControls().previous_button.callback(it))
    check("the button with no history explains itself",
          "There's no previous track" in text_of(None, it), text_of(None, it))
finally:
    amy.music.play_track, amy.refresh_now_playing = _real_play, _real_refresh

# The exit code is set by _check's gate, wherever a failure happens. It used to be one
# `if fails: sys.exit(1)` that sat above the restore section, so ~30 checks there could
# print FAIL while the script exited 0 - the restore path looked covered and wasn't.
finish("ALL QUEUE COMMAND TESTS PASSED")
