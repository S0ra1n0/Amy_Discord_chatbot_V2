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

print()
print("ALL MUSIC TESTS PASSED")
