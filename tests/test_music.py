import io, os, sys
from collections import deque
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
# Resolve the project root from this file, so the suite runs from any checkout
PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJ)
os.chdir(PROJ)

import music
from music import LoopMode, Track, advance_queue, format_duration, parse_volume

def T(name, dur=None):
    return Track(title=name, query=name, duration=dur, requested_by="tester")

# ---- format_duration ----
assert format_duration(0) == "0:00"
assert format_duration(5) == "0:05"
assert format_duration(65) == "1:05"
assert format_duration(599) == "9:59"
assert format_duration(3600) == "1:00:00"
assert format_duration(3725) == "1:02:05"
assert format_duration(None) == "?:??"
assert format_duration(-1) == "?:??"
print("format_duration: OK (8 cases)")

# ---- parse_volume ----
assert parse_volume("0") == 0
assert parse_volume("50") == 50
assert parse_volume("100") == 100
assert parse_volume("101") is None
assert parse_volume("-1") is None
assert parse_volume("abc") is None
assert parse_volume("") is None
assert parse_volume("5.5") is None
print("parse_volume: OK (8 cases)")

# ---- advance_queue: LoopMode.OFF ----
q = deque([T("b"), T("c")])
nxt = advance_queue(T("a"), q, LoopMode.OFF)
assert nxt.title == "b", nxt.title
assert [t.title for t in q] == ["c"], [t.title for t in q]
print("advance OFF: takes next, drops finished: OK")

q = deque()
assert advance_queue(T("a"), q, LoopMode.OFF) is None
print("advance OFF: empty queue -> None: OK")

# ---- advance_queue: LoopMode.TRACK ----
q = deque([T("b")])
cur = T("a")
nxt = advance_queue(cur, q, LoopMode.TRACK)
assert nxt is cur, "TRACK must replay the same track"
assert [t.title for t in q] == ["b"], "TRACK must not consume the queue"
print("advance TRACK: replays current, queue untouched: OK")

# TRACK with nothing playing should still pull from the queue
q = deque([T("b")])
nxt = advance_queue(None, q, LoopMode.TRACK)
assert nxt.title == "b"
print("advance TRACK: no current -> pulls next: OK")

# ---- advance_queue: LoopMode.QUEUE ----
q = deque([T("b"), T("c")])
nxt = advance_queue(T("a"), q, LoopMode.QUEUE)
assert nxt.title == "b"
assert [t.title for t in q] == ["c", "a"], [t.title for t in q]
print("advance QUEUE: rotates finished to the back: OK")

# full rotation returns to the start
q = deque([T("b")])
cur = T("a")
n1 = advance_queue(cur, q, LoopMode.QUEUE)          # -> b, queue [a]
n2 = advance_queue(n1, q, LoopMode.QUEUE)           # -> a, queue [b]
assert (n1.title, n2.title) == ("b", "a"), (n1.title, n2.title)
assert [t.title for t in q] == ["b"]
print("advance QUEUE: full rotation cycles correctly: OK")

# ---- total_duration ----
assert music.total_duration(deque([T("a", 60), T("b", 30)])) == 90
assert music.total_duration(deque([T("a", 60), T("b", None)])) is None
assert music.total_duration(deque()) == 0
print("total_duration: OK (3 cases)")

# ---- render_queue ----
out = music.render_queue(None, deque(), LoopMode.OFF)
assert "Nothing is playing" in out
out = music.render_queue(T("Song A", 100), deque([T("Song B", 200)]), LoopMode.OFF)
assert "Song A" in out and "Song B" in out and "1:40" in out and "3:20" in out
assert "Loop" not in out, "loop line should be hidden when off"
out = music.render_queue(T("Song A"), deque(), LoopMode.QUEUE)
assert "Loop" in out and "queue" in out
# truncation
big = deque([T("t%d" % i) for i in range(25)])
out = music.render_queue(T("cur"), big, LoopMode.OFF, page=1, page_size=10)
assert "page 1/3" in out, out[-120:]
assert "`10.`" in out and "`11.`" not in out, "page 1 must stop at position 10"
print("render_queue: OK (4 cases incl. pagination)")

# ---- GuildPlayer / MusicManager ----
mgr = music.MusicManager()
p = mgr.player_for(1)
assert mgr.player_for(1) is p, "same guild must reuse its player"
assert mgr.player_for(2) is not p
p.queue.append(T("x"))
p.current = T("y")
p.loop_mode = LoopMode.QUEUE
mgr.cleanup(1)
p2 = mgr.player_for(1)
assert len(p2.queue) == 0 and p2.current is None and p2.loop_mode is LoopMode.OFF, "cleanup must reset"
mgr.cleanup(999)  # cleaning a guild that never played must not raise
print("MusicManager player/cleanup: OK")

# ---- find_ffmpeg honours FFMPEG_PATH ----
old = os.environ.get("FFMPEG_PATH")
os.environ["FFMPEG_PATH"] = r"C:\definitely\not\here\ffmpeg.exe"
got = music.find_ffmpeg()
assert got != r"C:\definitely\not\here\ffmpeg.exe", "nonexistent FFMPEG_PATH must fall through to PATH"
real = os.path.abspath(__file__)  # any file that certainly exists
os.environ["FFMPEG_PATH"] = real
assert music.find_ffmpeg() == real, "an existing FFMPEG_PATH must win"
if old is None:
    del os.environ["FFMPEG_PATH"]
else:
    os.environ["FFMPEG_PATH"] = old
print("find_ffmpeg: OK (honours valid path, ignores bogus one)")

# ---- Playback position ----------------------------------------------------------------
# discord.py exposes no playback cursor, so position is derived from the clock. That only
# works if paused stretches are excluded and any seek offset is added back - get either
# wrong and the progress bar drifts, and /seek lands in the wrong place.
assert music.elapsed_seconds(None, None, 0.0, 100.0) == 0.0, "nothing playing"
assert music.elapsed_seconds(100.0, None, 0.0, 130.0) == 30.0, "plain elapsed"
assert music.elapsed_seconds(100.0, None, 45.0, 130.0) == 75.0, "seek offset must be added"
assert music.elapsed_seconds(100.0, 110.0, 0.0, 999.0) == 10.0, "paused clock must freeze"
assert music.elapsed_seconds(200.0, None, 0.0, 100.0) == 0.0, "never negative"

pl = music.GuildPlayer(1)
assert pl.position(now=500.0) == 0.0, "no position before anything plays"
pl.mark_started(0.0, now=100.0)
assert pl.position(now=130.0) == 30.0
pl.mark_paused(now=130.0)
assert pl.position(now=400.0) == 30.0, "position must not advance while paused"
pl.mark_paused(now=500.0)                       # double pause must not rewind
assert pl.position(now=600.0) == 30.0
pl.mark_resumed(now=430.0)                      # paused for 300s
assert pl.position(now=440.0) == 40.0, "time spent paused must not count"
pl.mark_resumed(now=999.0)                      # resume when not paused is a no-op
assert pl.position(now=440.0) == 40.0

pl.mark_started(90.0, now=1000.0)               # a seek restarts the clock at the offset
assert pl.position(now=1005.0) == 95.0
pl.mark_stopped()
assert pl.position(now=2000.0) == 0.0
pl.mark_started(10.0, now=10.0)
pl.reset()
assert pl.position(now=99.0) == 0.0, "reset must clear the position too"
print("playback position: OK (pause, resume, seek offset, reset)")

# ---- parse_timestamp ------------------------------------------------------------------
for text, want in [("90", 90), ("0", 0), ("1:30", 90), ("0:05", 5), ("10:00", 600),
                   ("1:02:03", 3723), ("  2:00  ", 120)]:
    assert music.parse_timestamp(text) == want, "%r -> %s" % (text, music.parse_timestamp(text))
# "1:75" is a typo; treating it as 2:15 would be worse than refusing.
for bad in ["", "   ", "abc", "1:aa", "-5", "1:-2", "1:75", "1:2:3:4", "1.5", None]:
    assert music.parse_timestamp(bad) is None, "%r should be rejected" % bad
print("parse_timestamp: OK (m:ss, h:mm:ss, bare seconds; rejects nonsense)")

# ---- clamp_seek -----------------------------------------------------------------------
assert music.clamp_seek(50.0, 200) == 50.0
assert music.clamp_seek(-10.0, 200) == 0.0, "never negative"
assert music.clamp_seek(500.0, 200) == 200 - music.SEEK_END_MARGIN, "must stop short of the end"
assert music.clamp_seek(500.0, None) == 500.0, "live streams have no end to clamp to"
assert music.clamp_seek(5.0, 1) == 0.0, "a track shorter than the margin clamps to 0"
print("clamp_seek: OK")

# ---- progress_bar ---------------------------------------------------------------------
bar = music.progress_bar(63, 260)
assert bar.startswith("1:03") and bar.endswith("4:20"), bar
assert "●" in bar.lower() or "●" in bar, "needs a position marker"
start, end = music.progress_bar(0, 100), music.progress_bar(100, 100)
assert start.index("●") < end.index("●"), "marker must move with elapsed time"
assert "live" in music.progress_bar(30, None), "unknown duration must not fake a full bar"
assert "live" in music.progress_bar(30, 0)
for e, d in [(0, 10), (10, 10), (99, 10), (5, 1)]:
    assert "●" in music.progress_bar(e, d), "bar broke at elapsed=%s dur=%s" % (e, d)
print("progress_bar: OK (moves, handles live streams and overruns)")

# ---- seek_before_options --------------------------------------------------------------
# -ss must come BEFORE the input, or FFmpeg decodes and throws away everything up to the
# seek point instead of jumping by container index.
URL = "https://example.test/audio.webm"
assert music.seek_before_options(0, URL).startswith(music.FFMPEG_BEFORE_OPTIONS), "no offset, no -ss"
assert "-ss" not in music.seek_before_options(-5, URL)
opts = music.seek_before_options(90.5, URL)
assert opts.startswith("-ss 90.500"), opts
assert music.FFMPEG_BEFORE_OPTIONS in opts, "-nostdin must survive"

# The reconnect flags are HTTP-protocol options. Passing them for a local file made FFmpeg
# exit with "Option reconnect not found" before opening anything - the source still built,
# then played silence, so local files failed with no error surfaced anywhere.
assert music.FFMPEG_RECONNECT_OPTIONS in music.seek_before_options(0, URL),     "http sources still need reconnect"
assert music.FFMPEG_RECONNECT_OPTIONS in music.seek_before_options(0, "HTTP://X.test/a")
for local in ["C:/music/song.mp3", "/home/me/song.mp3", "song.mp3", ""]:
    got = music.seek_before_options(0, local)
    assert "-reconnect" not in got, "%r must not get HTTP options: %r" % (local, got)
    assert "-nostdin" in got, got
assert "-ss 6.000" in music.seek_before_options(6, "C:/music/song.mp3"), "local seek still works"
print("seek_before_options: OK (-ss leads; reconnect only for http)")

# ---- clamp_bitrate --------------------------------------------------------------------
# A lossless local file probes far above what libopus accepts (a WAV reports 1536 kbps).
# Unclamped, the encoder fails to open and the track plays as silence.
assert music.clamp_bitrate(1536) == music.MAX_OPUS_BITRATE
assert music.clamp_bitrate(128) == 128, "normal bitrates pass through"
assert music.clamp_bitrate(music.MAX_OPUS_BITRATE) == music.MAX_OPUS_BITRATE
assert music.clamp_bitrate(0) == 1, "never zero - libopus needs a positive rate"
assert music.clamp_bitrate(-5) == 1
assert music.clamp_bitrate(None) is None, "unknown bitrate stays unknown"
print("clamp_bitrate: OK (lossless sources no longer play as silence)")

print()
print("ALL MUSIC TESTS PASSED")
