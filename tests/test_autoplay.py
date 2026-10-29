import asyncio
import dataclasses
import io
import os
import sys
import time

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
# Resolve the project root from this file, so the suite runs from any checkout
PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _fakes import (Guild, Interaction, Member, RealVoiceChannel, VoiceClient, load_bot,
                    text_of)
from _check import check, finish
from discord import app_commands

amy = load_bot()
music = amy.music

OWNER, USER = 42, 99


def vid(n):
    """An 11-character video id."""
    return ("vid%08d" % n)[:11]


def yt(n, title=None):
    return music.Track(title=title or "song %d" % n,
                       query="https://www.youtube.com/watch?v=" + vid(n),
                       duration=200, requested_by="userA")


# ---- youtube_video_id ----------------------------------------------------------------------
print("=== youtube_video_id ===")
ID = "dQw4w9WgXcQ"
for label, url, expected in [
    ("watch?v=", "https://www.youtube.com/watch?v=" + ID, ID),
    ("watch with extra params", "https://www.youtube.com/watch?feature=share&v=%s&t=42s&list=RD%s"
     % (ID, ID), ID),
    ("m. and music. hosts", "https://music.youtube.com/watch?v=" + ID, ID),
    ("youtu.be/", "https://youtu.be/" + ID, ID),
    ("youtu.be/ with ?t=", "https://youtu.be/%s?t=30" % ID, ID),
    ("shorts", "https://www.youtube.com/shorts/" + ID, ID),
    ("embed", "https://www.youtube.com/embed/" + ID, ID),
    ("http scheme", "http://youtube.com/watch?v=" + ID, ID),
    ("plain playlist", "https://www.youtube.com/playlist?list=PLabc", None),
    ("not YouTube", "https://vimeo.com/watch?v=" + ID, None),
    ("look-alike host", "https://notyoutube.com/watch?v=" + ID, None),
    ("a search term", "never gonna give you up", None),
    ("a local path", "C:\\Music\\song.mp3", None),
    ("too-short id", "https://www.youtube.com/watch?v=abc", None),
    ("empty", "", None),
]:
    check("youtube_video_id: " + label, music.youtube_video_id(url) == expected,
          music.youtube_video_id(url))
check("mix_url is the RD playlist of the video",
      music.mix_url(ID) == "https://www.youtube.com/watch?v=%s&list=RD%s" % (ID, ID))

# ---- pick_autoplay / recent_video_ids ------------------------------------------------------
print()
print("=== pick_autoplay ===")
seed, a, b, c = yt(1), yt(2), yt(3), yt(4)
recent = music.recent_video_ids([seed, a, None, music.Track("local", "C:\\x.mp3", is_local=True)])
check("recent ids come from the tracks, ignoring None and non-YouTube", recent == {vid(1), vid(2)},
      recent)
check("skips the seed and recent songs, takes the first new one",
      music.pick_autoplay([seed, a, b, c], recent) is b)
def _dur(n, seconds):
    return music.Track("d%d" % n, yt(n).query, duration=seconds)


cap = music.AUTOPLAY_MAX_SECONDS
check("the cap is 15 minutes", cap == 900, cap)
check("an entry longer than the cap is skipped",
      music.pick_autoplay([_dur(6, cap + 1), c], recent) is c)
check("an entry exactly at the cap is accepted",
      music.pick_autoplay([_dur(6, cap), c], recent).title == "d6")
check("an entry with no duration is accepted",
      music.pick_autoplay([_dur(6, None), c], recent).title == "d6")
check("returns None when nothing is new", music.pick_autoplay([seed, a], recent) is None)
check("returns None for no candidates", music.pick_autoplay([], recent) is None)
check("skips an entry without a usable url or title",
      music.pick_autoplay([music.Track("x", "no url"), music.Track("", yt(5).query), c],
                          recent) is c)
check("the same song under another URL form is still recent",
      music.pick_autoplay([music.Track("same", "https://youtu.be/" + vid(2)), c], recent) is c)

print()
print("=== should_autoplay (the gate) ===")
base = dict(enabled=True, last=yt(1), force_next=False, going_back=False, humans=1,
            played=60.0)
check("a song that ended with listeners present: yes", music.should_autoplay(**base))
for label, change in [
    ("setting off", dict(enabled=False)),
    ("nothing was playing (after /stop, /leave)", dict(last=None)),
    ("a local file", dict(last=music.Track("f", "C:\\f.mp3", is_local=True))),
    ("a track with no video id", dict(last=music.Track("t", "plain words"))),
    ("a /skip of the user's own song", dict(force_next=True)),
    ("it ended after 3 seconds (a stream that can't be read)", dict(played=3.0)),
    ("a 60s song that ended after 9.9 seconds", dict(played=9.9)),
    ("a /previous", dict(going_back=True)),
    ("nobody listening", dict(humans=0)),
]:
    check("no autoplay: " + label, not music.should_autoplay(**dict(base, **change)))

check("a /skip of an AUTOPLAY song moves on to another",
      music.should_autoplay(**dict(base, force_next=True, played=2.0,
                                   last=dataclasses.replace(yt(1), requested_by="Autoplay"))))
check("exactly the minimum play time counts", music.should_autoplay(**dict(base, played=10.0)))
short = dataclasses.replace(yt(1), duration=5)
check("a 5s song that played 4s to its end counts",
      music.should_autoplay(**dict(base, last=short, played=4.0)))
check("...but not after 2s", not music.should_autoplay(**dict(base, last=short, played=2.0)))
check("an unknown-length song needs the full minimum",
      not music.should_autoplay(**dict(base, last=dataclasses.replace(yt(1), duration=None),
                                       played=5.0)))
check("autoplay_picks returns spares in order, bounded to the attempts",
      [t.title for t in music.autoplay_picks([yt(i) for i in range(2, 9)], set())]
      == ["song 2", "song 3", "song 4"] and music.AUTOPLAY_ATTEMPTS == 3)

# ---- related_tracks, with the yt-dlp call faked --------------------------------------------
print()
print("=== related_tracks (yt-dlp faked) ===")
_real_flat = music._ytdl_extract_flat
_asked = []


def _fake_flat(url, first=None):
    _asked.append((url, first))
    return {"entries": [
        {"id": vid(1), "title": "seed", "duration": 200, "url": "x"},
        None,                                            # a failed entry
        {"id": vid(2), "title": "Second", "duration": 180, "thumbnail": "t"},
        {"id": "bad id", "title": "broken id"},
        {"id": vid(3), "title": ""},                     # no title
        {"url": "https://youtu.be/" + vid(4), "title": "Fourth"},
        {"id": vid(5), "title": "Live now", "live_status": "is_live"},
        {"id": vid(6), "title": "Soon", "live_status": "is_upcoming"},
        {"id": vid(7), "title": "Old flag", "is_live": True},
        {"id": vid(8), "title": "Was live", "live_status": "not_live"},
    ]}


music._ytdl_extract_flat = _fake_flat
try:
    got = asyncio.run(music.related_tracks(yt(1)))
    check("asks for the Mix of the seed, bounded to a few entries",
          _asked == [(music.mix_url(vid(1)), music.AUTOPLAY_CANDIDATES)], _asked)
    check("keeps usable entries as canonical watch URLs, requested by Autoplay",
          [(t.title, t.query, t.requested_by) for t in got]
          == [("seed", "https://www.youtube.com/watch?v=" + vid(1), "Autoplay"),
              ("Second", "https://www.youtube.com/watch?v=" + vid(2), "Autoplay"),
              ("Fourth", "https://www.youtube.com/watch?v=" + vid(4), "Autoplay"),
              ("Was live", "https://www.youtube.com/watch?v=" + vid(8), "Autoplay")],
          [(t.title, t.query) for t in got])
    try:
        asyncio.run(music.related_tracks(music.Track("t", "plain words")))
        check("a seed with no video id is refused", False)
    except ValueError:
        check("a seed with no video id is refused", True)
finally:
    music._ytdl_extract_flat = _real_flat

# ---- advance_playback wiring ---------------------------------------------------------------
print()
print("=== advance_playback: autoplay when the queue drains ===")
_real = (music.play_track, music.related_tracks, amy.refresh_now_playing, amy.announce,
         amy.finish_playback, amy.autoplay_enabled, amy.up_next_enabled)
played, said, finished, lookups = [], [], [], []
hooks = []          # run during the (slow) related lookup, like a command landing meanwhile
mix = []            # what the fake Mix returns, or an Exception to raise


bad = set()         # titles whose stream "can't be prepared"


async def _play(vc, player, track, after, start_at=0.0):
    if track.title in bad:
        raise RuntimeError("stream unreadable")
    played.append(track)
    player.current = track
    vc.stopped = False
    return True


async def _related(seed, *a, **kw):
    lookups.append(seed)
    await asyncio.sleep(0)
    for fn in hooks:
        fn()
    if isinstance(mix, Exception):
        raise mix
    # like the real one, entries come back requested by Autoplay
    return [dataclasses.replace(t, requested_by=music.AUTOPLAY_REQUESTER) for t in mix]


async def _refresh(*a, **kw):
    pass


async def _finish(guild, player):
    finished.append(player.current)
    player.last_played = player.current or player.last_played
    player.current = None


music.play_track, music.related_tracks = _play, _related
amy.refresh_now_playing, amy.finish_playback = _refresh, _finish
amy.announce = lambda guild, event, title=None, **kw: said.append((event, title))


def run(gid, *, last="yt", humans=1, enabled=True, mix_result=(), history=(), setup_fn=None,
        queue=(), ran_for=60.0):
    """One natural end of a song with an empty queue; returns the player."""
    global mix
    ch = RealVoiceChannel(10, "Chung", [Member(i, None, name="u%d" % i) for i in range(humans)])
    vc = VoiceClient(ch)
    vc.stopped = True
    guild = Guild(OWNER, Member(USER, ch), vc, guild_id=gid)
    amy.music_manager.cleanup(gid)
    p = amy.music_manager.player_for(gid)
    p.current = yt(1) if last == "yt" else music.Track("file.mp3", "C:\\file.mp3", is_local=True)
    p.started_at = time.monotonic() - ran_for     # how long the song that ended played
    p.history.extend(history)
    p.queue.extend(queue)
    mix = mix_result
    amy.autoplay_enabled, amy.up_next_enabled = enabled, True
    for lst in (played, said, finished, lookups):
        lst.clear()
    bad.clear()
    hooks.clear()
    if setup_fn:
        setup_fn(p)
    asyncio.run(amy.advance_playback(guild))
    return p


try:
    run(8001, mix_result=[yt(1), yt(2)])
    check("the queue drained and a related song plays", [t.title for t in played] == ["song 2"],
          [t.title for t in played])
    check("...requested by Autoplay", played and played[0].requested_by == "Autoplay")
    check("...and finish_playback did not run", finished == [], finished)
    check("...announced as 'Up next'", said == [("up_next", "song 2")], said)

    p = run(8002, mix_result=[yt(1), yt(2), yt(3)], history=[yt(2)])
    check("a song in the history is skipped (recent-skip)", [t.title for t in played] == ["song 3"],
          [t.title for t in played])
    check("the song that just ended went on the history, for /previous",
          p.history[-1].title == "song 1", [t.title for t in p.history])

    run(8003, mix_result=[yt(1), yt(2)], enabled=False)
    check("autoplay off: finish_playback, no lookup", finished != [] and lookups == []
          and played == [], (finished, lookups))
    run(8004, last="local", mix_result=[yt(2)])
    check("a local last track: finish, no lookup", finished != [] and lookups == [] and played == [])
    run(8005, humans=0, mix_result=[yt(2)])
    check("nobody in the channel: finish, no lookup", finished != [] and lookups == []
          and played == [])
    run(8006, mix_result=RuntimeError("yt-dlp broke"))
    check("a failing Mix lookup finishes cleanly", finished != [] and played == [])
    run(8007, mix_result=[])
    check("an empty Mix finishes", finished != [] and played == [])
    run(8008, mix_result=[yt(1)])
    check("a Mix with nothing new finishes", finished != [] and played == [])

    run(8009, mix_result=[yt(2)], setup_fn=lambda p: setattr(p, "current", None))
    check("nothing was playing (after /stop, /leave): finish, no lookup",
          finished != [] and lookups == [] and played == [])
    run(8010, mix_result=[yt(2)], setup_fn=lambda p: setattr(p, "skip_requested", True))
    check("a /skip of the last song finishes, no lookup", finished != [] and lookups == []
          and played == [])
    run(8011, mix_result=[yt(2)],
        setup_fn=lambda p: setattr(p, "loop_mode", music.LoopMode.TRACK))
    check("TRACK loop repeats and never looks up", lookups == [] and [t.title for t in played]
          == ["song 1"] and finished == [])

    run(8014, mix_result=[yt(2)], ran_for=1.0)
    check("a pick that ended after 1 second does not chain (unreadable stream)",
          finished != [] and lookups == [] and played == [], (finished, lookups, played))

    def _autoplay_last(p):
        p.current.requested_by = music.AUTOPLAY_REQUESTER
        p.skip_requested = True
    run(8015, mix_result=[yt(1), yt(2)], ran_for=2.0, setup_fn=_autoplay_last)
    check("/skip of an Autoplay song (even 2s in) moves on to the next related one, no 'Up next'",
          [t.title for t in played] == ["song 2"] and said == [] and finished == [],
          (played, said, finished))
    run(8016, mix_result=[yt(2)], setup_fn=lambda p: setattr(p, "skip_requested", True))
    check("/skip of the user's own last song still finishes", finished != [] and played == [])

    # a pick that won't start: spares from the same lookup, then give up
    def _bad(*titles):
        return lambda p: bad.update(titles)
    run(8017, mix_result=[yt(1), yt(2), yt(3), yt(4), yt(5)], setup_fn=_bad("song 2"))
    check("a pick that fails to start is replaced by the next candidate, one lookup",
          [t.title for t in played] == ["song 3"] and len(lookups) == 1 and finished == [],
          (played, lookups, finished))
    check("...and only the song that started is announced", said == [("up_next", "song 3")], said)
    run(8018, mix_result=[yt(1), yt(2), yt(3), yt(4), yt(5)],
        setup_fn=_bad("song 2", "song 3", "song 4"))
    check("when every attempt fails (3, bounded) playback finishes",
          played == [] and finished != [] and said == [], (played, finished, said))

    # /stop landing while the Mix is being fetched wins
    run(8012, mix_result=[yt(2)],
        setup_fn=lambda p: hooks.append(p.stop_all))
    check("/stop during the lookup: nothing plays, playback finishes",
          played == [] and finished != [] and said == [], (played, finished, said))

    # a /play queued during the lookup goes first
    run(8013, mix_result=[yt(2)],
        setup_fn=lambda p: hooks.append(lambda: p.queue.append(yt(9, "requested"))))
    check("a /play landing during the lookup plays instead of the autoplay pick",
          [t.title for t in played] == ["requested"], [t.title for t in played])
    check("...and is not announced as 'Up next' (the /play speaks for itself)", said == [], said)
finally:
    (music.play_track, music.related_tracks, amy.refresh_now_playing, amy.announce,
     amy.finish_playback, amy.autoplay_enabled, amy.up_next_enabled) = _real

# ---- /autoplay -----------------------------------------------------------------------------
print()
print("=== /autoplay ===")
_real_ap = amy.autoplay_enabled
ch = RealVoiceChannel(10, "Chung")
member = Member(USER, ch, name="userA")
guild = Guild(OWNER, member, VoiceClient(ch), guild_id=8100)
cmd = amy.tree.get_command("autoplay")
check("/autoplay is registered, guild-only", cmd is not None and cmd.guild_only)


def slash(it, *args):
    amy.voice_cmd_store.clear()
    asyncio.run(cmd.callback(it, *args))
    return text_of(None, it)


try:
    amy.autoplay_enabled = True
    out = slash(Interaction(member, guild))
    check("no argument shows the status", "Autoplay is **on**" in out, out)
    out = slash(Interaction(member, guild), app_commands.Choice(name="off", value="off"))
    check("off turns it off and saves it", amy.autoplay_enabled is False
          and amy.db.get_bool_setting("autoplay", True) is False and "off" in out, out)
    out = slash(Interaction(member, guild))
    check("the status now says off", "Autoplay is **off**" in out, out)
    out = slash(Interaction(member, guild), app_commands.Choice(name="on", value="on"))
    check("on turns it back on and saves it", amy.autoplay_enabled is True
          and amy.db.get_bool_setting("autoplay", False) is True, out)

    stranger = Member(77, None, name="stranger")
    out = slash(Interaction(stranger, guild), app_commands.Choice(name="off", value="off"))
    check("someone outside Amy's channel can't change it", amy.autoplay_enabled is True
          and "voice channel" in out, out)
    out = slash(Interaction(stranger, guild))
    check("...but can read the status", "Autoplay is **on**" in out, out)
    unconnected = Guild(OWNER, member, None, guild_id=8101)
    out = slash(Interaction(member, unconnected), app_commands.Choice(name="off", value="off"))
    check("not connected: refused, setting unchanged", amy.autoplay_enabled is True, out)
finally:
    amy.autoplay_enabled = _real_ap
    amy.db.set_bool_setting("autoplay", _real_ap)

finish("ALL AUTOPLAY TESTS PASSED")
