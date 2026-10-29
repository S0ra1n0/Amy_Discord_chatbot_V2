"""
/filter: the preset table, how a filter changes the FFmpeg source (and refuses the opus
passthrough), position in TRACK seconds under a speed-changing filter, the command (restart at
the current position, idle, show, permissions, reset on leave) and the player-card field.
"""
import asyncio
import io
import os
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import discord
from discord import app_commands

from _fakes import (Guild, Interaction, Member, RealVoiceChannel, VoiceClient, load_bot,
                    seed_queue, text_of)
from _check import check, finish

amy = load_bot()
music, ui, voicelines = amy.music, amy.ui, amy.voicelines

OWNER, USER = 42, 99
CH = RealVoiceChannel(10, "Chung")

# ---- preset table --------------------------------------------------------------------------
print("=== presets ===")
check("the four presets", list(music.FILTER_PRESETS) == ["off", "bass boost", "nightcore", "slowed"],
      list(music.FILTER_PRESETS))
check("off has no chain and rate 1", music.filter_chain("off") == "" and music.filter_rate("off") == 1.0)
for name in ("bass boost", "nightcore", "slowed"):
    check("%s has a chain" % name, music.filter_chain(name) != "")
check("rates: bass 1.0, nightcore 1.25, slowed 0.8",
      (music.filter_rate("bass boost"), music.filter_rate("nightcore"), music.filter_rate("slowed"))
      == (1.0, 1.25, 0.8))
check("nightcore resamples to 48k, asetrate 60000, back to 48k",
      music.filter_chain("nightcore") == "aresample=48000,asetrate=60000,aresample=48000")
check("slowed uses asetrate 38400", "asetrate=38400" in music.filter_chain("slowed"))
check("bass boost ends in a limiter (the boost can't clip)",
      "bass=" in music.filter_chain("bass boost") and "alimiter" in music.filter_chain("bass boost"))
check("unknown names mean off", music.filter_chain("???") == "" and music.filter_rate("???") == 1.0)
check("normalise_filter: case and spacing",
      music.normalise_filter("  Bass   BOOST ") == "bass boost" and music.normalise_filter("echo") is None
      and music.normalise_filter(None) is None)

# ---- source building -----------------------------------------------------------------------
print()
print("=== build_source ===")
check("no filter: options are just -vn", music.source_options("off") == "-vn")
check("filter: -af chain follows", music.source_options("nightcore") == "-vn -af " + music.filter_chain("nightcore"))

calls = []


class FakePCM(discord.AudioSource):
    def __init__(self, source, **kw):
        calls.append(("pcm", kw))

    def read(self):
        return b""


class FakeOpus(discord.AudioSource):
    def __init__(self, source, **kw):
        calls.append(("opus", kw))

    @classmethod
    async def probe(cls, source, **kw):
        return "opus", 128

    def is_opus(self):
        return True

    def read(self):
        return b""


_real_pcm, _real_opus = discord.FFmpegPCMAudio, discord.FFmpegOpusAudio
discord.FFmpegPCMAudio, discord.FFmpegOpusAudio = FakePCM, FakeOpus   # type: ignore[misc,assignment]
try:
    src = asyncio.run(music.build_source("https://x/y", 1.0, "ffmpeg", 0.0))
    check("full volume, no filter: opus passthrough", calls[-1][0] == "opus", calls)
    check("...with plain options", calls[-1][1]["options"] == "-vn", calls[-1])
    src = asyncio.run(music.build_source("https://x/y", 1.0, "ffmpeg", 30.0, "nightcore"))
    check("full volume WITH a filter: no passthrough (PCM)", calls[-1][0] == "pcm", calls)
    check("...carrying the -af chain", calls[-1][1]["options"].endswith("-af " + music.filter_chain("nightcore")),
          calls[-1])
    check("...and -ss in track time before the input", "-ss 30.000" in calls[-1][1]["before_options"], calls[-1])
    check("...wrapped for /volume", isinstance(src, discord.PCMVolumeTransformer))
    src = asyncio.run(music.build_source("https://x/y", 0.5, "ffmpeg", 0.0, "bass boost"))
    check("below full volume the chain is there too", "alimiter" in calls[-1][1]["options"], calls[-1])
    src = asyncio.run(music.build_source("/music/a.mp3", 1.0, "ffmpeg", 0.0, "slowed"))
    check("a local file is filtered too", calls[-1][0] == "pcm" and "asetrate=38400" in calls[-1][1]["options"], calls[-1])
finally:
    discord.FFmpegPCMAudio, discord.FFmpegOpusAudio = _real_pcm, _real_opus   # type: ignore[misc]

# ---- time maths ----------------------------------------------------------------------------
print()
print("=== position is TRACK seconds ===")
check("rate 1: unchanged", music.elapsed_seconds(100.0, None, 45.0, 130.0) == 75.0)
check("nightcore: 8 wall seconds = 10 track seconds",
      abs(music.elapsed_seconds(100.0, None, 0.0, 108.0, 1.25) - 10.0) < 1e-9)
check("slowed: 10 wall seconds = 8 track seconds",
      abs(music.elapsed_seconds(100.0, None, 0.0, 110.0, 0.8) - 8.0) < 1e-9)
check("the seek offset is track time, not scaled",
      abs(music.elapsed_seconds(100.0, None, 60.0, 108.0, 1.25) - 70.0) < 1e-9)
check("paused clock still freezes under a rate",
      abs(music.elapsed_seconds(100.0, 104.0, 0.0, 999.0, 1.25) - 5.0) < 1e-9)

pl = music.GuildPlayer(1)
pl.mark_started(100.0, now=1000.0, rate=1.25)
check("player.position scales by the active rate", abs(pl.position(now=1008.0) - 110.0) < 1e-9, pl.position(now=1008.0))
pl.mark_paused(now=1008.0)
pl.mark_resumed(now=1100.0)
check("a pause adds nothing under a rate", abs(pl.position(now=1100.0) - 110.0) < 1e-9, pl.position(now=1100.0))
check("...and it runs on at the rate after resuming", abs(pl.position(now=1108.0) - 120.0) < 1e-9)
pl.mark_stopped()
check("stopped: position 0 and rate back to 1", pl.position(now=5000.0) == 0.0 and pl.rate == 1.0)
pl.mark_started(0.0, now=0.0)
check("mark_started defaults to rate 1", pl.rate == 1.0 and pl.position(now=10.0) == 10.0)

print("-- starting a source sets the clock rate of the filter it was built with --")


class _Src(discord.AudioSource):
    def read(self):
        return b""


class _VC:
    def __init__(self):
        self.source = None

    def play(self, source, after=None):
        self.source = source

    def stop(self):
        pass

    def is_playing(self):
        return self.source is not None

    def is_paused(self):
        return False


built = []
_rb, _rr = music.build_source, music.resolve_stream_url


async def _fake_build(url, volume, ffmpeg, start_at=0.0, filter_name="off"):
    built.append((start_at, filter_name))
    return _Src()


async def _fake_resolve(track):
    return "https://stream/x"


music.build_source, music.resolve_stream_url = _fake_build, _fake_resolve
try:
    song = music.Track(title="S", query="https://youtu.be/abcdefghijk", duration=300, requested_by="u")
    pl = music.GuildPlayer(2)
    pl.filter_name = "nightcore"
    asyncio.run(music.restart_at(_VC(), pl, song, lambda e: None, 90.0))
    check("restart_at builds with the filter and runs the clock at its rate",
          built[-1] == (90.0, "nightcore") and pl.rate == 1.25 and pl.seek_offset == 90.0, (built, pl.rate))
    pl = music.GuildPlayer(3)
    pl.filter_name = "slowed"
    asyncio.run(music.play_track(_VC(), pl, song, lambda e: None, 10.0))
    check("play_track too", built[-1] == (10.0, "slowed") and pl.rate == 0.8, (built, pl.rate))
    pl = music.GuildPlayer(4)
    asyncio.run(music.play_track(_VC(), pl, song, lambda e: None))
    check("no filter: rate 1", pl.rate == 1.0 and built[-1][1] == "off")
    pl = music.GuildPlayer(5)
    check("a new player has no active filter", pl.active_filter == "off")
    asyncio.run(music.play_track(_VC(), pl, music.Track(title="S", query="https://youtu.be/abcdefghijk",
                                                        duration=300, requested_by="u"), lambda e: None))
    pl.filter_name = "nightcore"
    check("changing the stored filter does not touch the playing source's",
          pl.active_filter == "off" and pl.rate == 1.0)
    pl.filter_name = "slowed"
    asyncio.run(music.restart_at(_VC(), pl, music.Track(title="S", query="https://youtu.be/abcdefghijk",
                                                        duration=300, requested_by="u"), lambda e: None, 5.0))
    check("_start records the filter the source was built with", pl.active_filter == "slowed")
    pl.mark_stopped()
    check("stopped: active filter off", pl.active_filter == "off")

finally:
    music.build_source, music.resolve_stream_url = _rb, _rr

print("-- end timestamp, wall time = remaining / rate --")
check("rate 1 as before", music.end_timestamp(63, 260, 1000.0) == 1197)
check("nightcore: 100 track s left ends in 80 wall s", music.end_timestamp(160, 260, 1000.0, 1.25) == 1080)
check("slowed: 80 track s left ends in 100 wall s", music.end_timestamp(180, 260, 1000.0, 0.8) == 1100)
check("still None at/after the end", music.end_timestamp(260, 260, 1000.0, 1.25) is None)

print("-- autoplay's played-enough uses the position (track seconds) --")
pl.mark_started(0.0, now=0.0, rate=1.25)
check("8 wall seconds of nightcore = 10 track seconds = played enough",
      music.played_enough(pl.position(now=8.0), 200))

# ---- the card ------------------------------------------------------------------------------
print()
print("=== player card ===")
trk = music.Track(title="Song", query="https://youtu.be/abcdefghijk", duration=260, requested_by="userA")


def fields(**kw):
    return {f.name: f.value for f in ui.now_playing_embed(trk, **kw).fields}


check("no Filter field when off", "Filter" not in fields(elapsed=10, now=1000.0))
check("Filter field names the preset", fields(elapsed=10, now=1000.0, filter_name="nightcore").get("Filter") == "nightcore")
check("no Filter field on the finished card", "Filter" not in fields(stopped=True, filter_name="nightcore"))
check("Ends is scaled by the rate (250 s left at 1.25 = 200 s)",
      "<t:1200:R>" in fields(elapsed=10, now=1000.0, filter_name="nightcore")["Position"],
      fields(elapsed=10, now=1000.0, filter_name="nightcore"))

print("-- the card shows what the source REALLY plays, not what is stored for the next one --")
import re as _re
import time as _time
_p = seed_queue(amy, 9100, [], current="playing")
_p.current = music.Track(title="Long song", query="https://youtu.be/long", duration=600, requested_by="u")
_ctx_it = Interaction(Member(USER, CH, name="userA"),
                      Guild(OWNER, Member(USER, CH, name="userA"), VoiceClient(CH), guild_id=9100))


def card_text(player):
    r = asyncio.run(amy.music_nowplaying(amy.MusicContext.build(_ctx_it, _ctx_it.guild)))
    return text_of(r)


_p.mark_started(0.0, now=_time.monotonic() - 40.0)         # 40 s in, source built unfiltered
_p.filter_name = "nightcore"                               # stored for the NEXT source only
_txt = card_text(_p)
_end = int(_re.search(r"<t:(\d+):R>", _txt).group(1))
check("stored nightcore, source at rate 1: Ends uses 1.0 (560 s left)",
      abs(_end - (_time.time() + 560)) <= 3, (_end - _time.time()))
check("...and no Filter field is shown", "Filter" not in _txt, _txt)
_p.mark_started(0.0, now=_time.monotonic() - 40.0, rate=1.25)
_p.active_filter = "nightcore"
_txt = card_text(_p)
_end = int(_re.search(r"<t:(\d+):R>", _txt).group(1))
check("source really at nightcore: Ends uses 1.25 and the field says so",
      abs(_end - (_time.time() + 550 / 1.25)) <= 3 and "nightcore" in _txt, _txt)

import inspect as _insp
for _fn in (amy.refresh_now_playing, amy.PlayerControls.pause_button):
    _src = _insp.getsource(_fn)
    check("%s passes the playing source's filter and rate to the card" % _fn.__name__,
          "filter_name=player.active_filter, rate=player.rate" in _src, _src[-300:])

# ---- voice lines ---------------------------------------------------------------------------
print()
print("=== spoken confirmations ===")
check("filter lines are calm and name the preset",
      all("!" not in l and "{name}" in l for l in voicelines.LINES["filter"]), voicelines.LINES["filter"])
check("filter_off lines are calm", all("!" not in l for l in voicelines.LINES["filter_off"]))

# ---- /filter -------------------------------------------------------------------------------
print()
print("=== /filter ===")
cmd = amy.tree.get_command("filter")
check("registered, guild-only, four choices", cmd is not None and cmd.guild_only
      and [c.value for c in cmd.parameters[0].choices] == list(music.FILTER_PRESETS))
check("/filter defers before the restart", "response.defer(" in __import__("inspect").getsource(cmd.callback))

restarts, said, refreshed = [], [], []
_real = (music.restart_at, amy.announce, amy.refresh_now_playing)


async def fake_restart(vc, player, track, after, position):
    restarts.append((track.title, position, player.filter_name, player.lock.locked()))
    return True


async def fake_refresh(guild, stopped=False, paused=False):
    refreshed.append(paused)


def world(gid, who=USER, playing=True):
    amy.voice_cmd_store.clear()
    member = Member(who, CH, name="userA")
    vc = VoiceClient(CH)
    guild = Guild(OWNER, member, vc, guild_id=gid)
    p = seed_queue(amy, gid, ["next"], current="playing" if playing else None)
    if playing:
        p.current = music.Track(title="Long song", query="https://youtu.be/long", duration=600,
                                requested_by="userA")
        p.mark_started(0.0, now=music.time.monotonic() - 40.0)     # 40 s in, no filter yet
    return p, Interaction(member, guild), vc


def slash(it, name=None):
    amy.voice_cmd_store.clear()
    asyncio.run(cmd.callback(it, None if name is None else app_commands.Choice(name=name, value=name)))
    return text_of(None, it)


music.restart_at = fake_restart
amy.announce = lambda guild, event, title=None, **v: said.append((event, v))
amy.refresh_now_playing = fake_refresh
try:
    p, it, vc = world(9001)
    out = slash(it)
    check("no argument shows the current filter (off)", "**off**" in out and not restarts, out)

    out = slash(it, "nightcore")
    check("while playing: the song restarts once", len(restarts) == 1, restarts)
    check("...at the current TRACK position (~40 s)", abs(restarts[0][1] - 40.0) < 1.0, restarts)
    check("...with the new filter already set, under the player lock",
          restarts[0][2] == "nightcore" and restarts[0][3] is True, restarts)
    check("...and the player keeps it", p.filter_name == "nightcore")
    check("...spoken as a filter line (not Up next)", said == [("filter", {"name": "nightcore"})], said)
    check("...the card is refreshed", refreshed == [False], refreshed)
    check("...the reply names it", "nightcore" in out, out)

    out = slash(it)
    check("shows the active filter", "**nightcore**" in out, out)
    n = len(restarts)
    out = slash(it, "nightcore")
    check("choosing the same one again restarts nothing", len(restarts) == n, restarts)

    out = slash(it, "off")
    check("off restarts again, announced as filter_off",
          len(restarts) == n + 1 and p.filter_name == "off" and said[-1][0] == "filter_off", (restarts, said))

    # position mapping while a speed filter runs: 8 wall s at 1.25 = 10 track s past the start
    p.mark_started(100.0, now=music.time.monotonic() - 8.0, rate=1.25)
    slash(it, "slowed")
    check("a running nightcore song restarts at its TRACK position (~110 s)",
          abs(restarts[-1][1] - 110.0) < 1.0, restarts[-1])

    # a /stop during the restart's resolve (restart_at returns False): the filter is NOT changed
    async def stopped_restart(*a, **k):
        return False
    music.restart_at = stopped_restart
    p.filter_name = "off"
    out = slash(it, "nightcore")
    check("restart_at False: the filter reverts, matching the reply",
          p.filter_name == "off" and "didn't change" in out, (p.filter_name, out))
    music.restart_at = fake_restart

    # a failed restart keeps the old filter and says the song is still playing
    async def failing(*a, **k):
        raise RuntimeError("yt-dlp hiccup")
    music.restart_at = failing
    p.filter_name = "off"
    out = slash(it, "bass boost")
    check("a failed restart leaves the filter unchanged", p.filter_name == "off", p.filter_name)
    check("...and says the song is still playing", "still playing" in out, out)
    music.restart_at = fake_restart

    # idle: stored for the next song, nothing restarted
    restarts.clear(); said.clear()
    p, it, vc = world(9002, playing=False)
    out = slash(it, "slowed")
    check("idle: stored, no restart", p.filter_name == "slowed" and not restarts, (p.filter_name, restarts))
    check("...told it applies from the next song", "next song" in out, out)
    check("...and still confirmed out loud", said == [("filter", {"name": "slowed"})], said)

    # a /filter while an advance is LOADING the next song (lock held, nothing audible yet): it
    # must wait, and once the track is up restart it - the track captured the old filter.
    restarts.clear(); said.clear()
    p, it, vc = world(9005, playing=False)
    song = music.Track(title="Loading song", query="https://youtu.be/loadingsong", duration=300,
                       requested_by="userA")

    async def during_load():
        await p.lock.acquire()               # advance_playback is resolving the next track
        task = asyncio.ensure_future(
            amy.music_filter(amy.MusicContext.build(it, it.guild), "nightcore"))
        await asyncio.sleep(0.05)
        waited = not task.done() and p.filter_name == "off"
        p.current = song                     # the track started (built unfiltered)
        p.mark_started(0.0, now=music.time.monotonic() - 3.0)
        p.lock.release()
        return waited, text_of(await task)

    waited, out = asyncio.run(during_load())
    check("locked, nothing active: /filter waits for the load", waited)
    check("...then restarts the track that started", len(restarts) == 1 and restarts[0][0] == "Loading song"
          and p.filter_name == "nightcore", (restarts, p.filter_name))
    check("...and does not claim 'next song'", "next song" not in out, out)

    # /stop keeps it; leaving (reset) clears it
    p.filter_name = "bass boost"
    p.stop_all()
    check("/stop keeps the filter", p.filter_name == "bass boost")
    p.reset()
    check("leaving (reset) turns it off", p.filter_name == "off")

    # permissions: same gate as /loop
    restarts.clear()
    p, it, vc = world(9003)
    stranger = Member(77, None, name="stranger")
    it2 = Interaction(stranger, it.guild)
    out = slash(it2, "nightcore")
    check("someone outside Amy's channel can't change it", p.filter_name == "off" and not restarts
          and "voice channel" in out, out)
    out = slash(Interaction(stranger, it.guild))
    check("...but can read it", "**off**" in out, out)
    unconnected = Guild(OWNER, Member(USER, CH, name="userA"), None, guild_id=9004)
    out = slash(Interaction(Member(USER, CH, name="userA"), unconnected), "nightcore")
    check("not connected: refused", "not in a voice channel" in out, out)
    out = text_of(asyncio.run(amy.music_filter(amy.MusicContext.build(it, it.guild), "echo")))
    check("an unknown preset is rejected by the handler", "don't know" in out, out)
finally:
    music.restart_at, amy.announce, amy.refresh_now_playing = _real

finish("ALL FILTER TESTS PASSED")
