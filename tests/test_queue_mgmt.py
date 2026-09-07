import io, os, sys
from collections import deque
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
# Resolve the project root from this file, so the suite runs from any checkout
PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJ); os.chdir(PROJ)
import music
from music import LoopMode

def T(n, d=None, who="me"):
    return music.Track(title=n, query=n, duration=d, requested_by=who)

fails = []
def check(label, ok):
    print(("  PASS " if ok else "  FAIL ") + label)
    if not ok: fails.append(label)

print("=== is_playlist_url ===")
for q, want in [
    ("https://www.youtube.com/playlist?list=PLabc", True),
    ("http://youtube.com/playlist?list=PLabc", True),
    ("https://www.youtube.com/watch?v=abc&list=PLabc", False),   # share link -> single video
    ("https://www.youtube.com/watch?list=PLabc&v=abc", False),   # param order must not matter
    ("https://www.youtube.com/watch?v=abc", False),
    ("https://youtu.be/abc", False),
    ("never gonna give you up", False),
    ("", False),
    ("ftp://example.com/playlist?list=x", False),                # non-http scheme
]:
    check("%-52s -> %s" % (repr(q)[:52], want), music.is_playlist_url(q) is want)

print()
print("=== total_pages / clamp_page ===")
check("0 items -> 1 page", music.total_pages(0) == 1)
check("10 items -> 1 page", music.total_pages(10) == 1)
check("11 items -> 2 pages", music.total_pages(11) == 2)
check("100 items -> 10 pages", music.total_pages(100) == 10)
check("clamp 0 -> 1", music.clamp_page(0, 25) == 1)
check("clamp -5 -> 1", music.clamp_page(-5, 25) == 1)
check("clamp 99 -> last", music.clamp_page(99, 25) == 3)
check("clamp 2 stays 2", music.clamp_page(2, 25) == 2)

print()
print("=== remove_at (1-based, bounds checked) ===")
q = deque([T("a"), T("b"), T("c")])
check("removes middle", music.remove_at(q, 2).title == "b")
check("queue now a,c", [t.title for t in q] == ["a", "c"])
check("index 0 -> None", music.remove_at(q, 0) is None)
check("negative -> None", music.remove_at(q, -1) is None)
check("past end -> None", music.remove_at(q, 99) is None)
check("queue untouched by bad indexes", [t.title for t in q] == ["a", "c"])
check("empty queue -> None", music.remove_at(deque(), 1) is None)
check("remove last remaining", music.remove_at(q, 2).title == "c" and [t.title for t in q] == ["a"])

print()
print("=== peek_at does not mutate ===")
q = deque([T("a"), T("b")])
check("peek returns track", music.peek_at(q, 2).title == "b")
check("length unchanged", len(q) == 2)
check("out of range -> None", music.peek_at(q, 5) is None)

print()
print("=== shuffle_queue ===")
q = deque([T("t%d" % i) for i in range(20)])
before = sorted(t.title for t in q)
music.shuffle_queue(q)
check("same length", len(q) == 20)
check("same membership", sorted(t.title for t in q) == before)
check("still a deque", isinstance(q, deque))
q1 = deque([T("only")])
music.shuffle_queue(q1)
check("single item safe", [t.title for t in q1] == ["only"])
q0 = deque(); music.shuffle_queue(q0)
check("empty safe", len(q0) == 0)

print()
print("=== drop_before (/skipto) ===")
q = deque([T("a"), T("b"), T("c"), T("d")])
check("drop before 3 -> 2 dropped", music.drop_before(q, 3) == 2)
check("queue now c,d", [t.title for t in q] == ["c", "d"])
check("drop before 1 -> 0", music.drop_before(q, 1) == 0)
check("queue unchanged", [t.title for t in q] == ["c", "d"])
check("out of range -> 0", music.drop_before(q, 99) == 0)
check("queue still intact", [t.title for t in q] == ["c", "d"])

print()
print("=== render_queue pagination ===")
q = deque([T("track%d" % i, 60) for i in range(1, 26)])
p1 = music.render_queue(T("cur"), q, LoopMode.OFF, page=1)
p3 = music.render_queue(T("cur"), q, LoopMode.OFF, page=3)
check("page 1 shows 1..10", "`1.`" in p1 and "`10.`" in p1 and "`11.`" not in p1)
check("page 3 uses absolute numbering", "`21.`" in p3 and "`25.`" in p3)
check("page 3 is the last partial page", "`26.`" not in p3)
check("page indicator present", "page 1/3" in p1 and "page 3/3" in p3)
check("out-of-range page clamps to last",
      music.render_queue(T("cur"), q, LoopMode.OFF, page=99) == p3)
check("page 0 clamps to first",
      music.render_queue(T("cur"), q, LoopMode.OFF, page=0) == p1)
check("single page hides the hint",
      "Use `/queue" not in music.render_queue(T("cur"), deque([T("x")]), LoopMode.OFF))
check("empty queue still renders",
      "Nothing is playing" in music.render_queue(None, deque(), LoopMode.OFF))

print()
if fails:
    print("%d CHECK(S) FAILED" % len(fails))
    for f in fails: print("   -", f)
    sys.exit(1)

# ---- regression: youtu.be share links are single videos, not playlists ----
print()
print("=== is_playlist_url: youtu.be short links ===")
fails2 = []
def check2(label, ok):
    print(("  PASS " if ok else "  FAIL ") + label)
    if not ok: fails2.append(label)

check2("youtu.be/VIDEO?list=PL is a single video",
       music.is_playlist_url("https://youtu.be/dQw4w9WgXcQ?list=PLabc") is False)
check2("youtu.be/VIDEO is a single video",
       music.is_playlist_url("https://youtu.be/dQw4w9WgXcQ") is False)
check2("youtube.com/playlist?list= is still a playlist",
       music.is_playlist_url("https://www.youtube.com/playlist?list=PLabc") is True)
check2("music.youtube.com playlist still detected",
       music.is_playlist_url("https://music.youtube.com/playlist?list=PLabc") is True)

# ---- regression: an explicit skip must beat TRACK loop ----
print()
print("=== advance_queue force_next (explicit skip) ===")
cur = T("playing")
q = deque([T("next")])
check2("TRACK loop replays without force",
       music.advance_queue(cur, deque([T("next")]), LoopMode.TRACK).title == "playing")
check2("force_next overrides TRACK loop",
       music.advance_queue(cur, q, LoopMode.TRACK, force_next=True).title == "next")

# QUEUE rotation is still honoured on a forced skip
q2 = deque([T("b")])
nxt = music.advance_queue(T("a"), q2, LoopMode.QUEUE, force_next=True)
check2("forced skip under QUEUE still rotates", nxt.title == "b" and [t.title for t in q2] == ["a"])

# force_next changes nothing for OFF
q3 = deque([T("b")])
check2("force_next is a no-op for OFF",
       music.advance_queue(T("a"), q3, LoopMode.OFF, force_next=True).title == "b")

# /skipto under TRACK loop reaches the requested track
q4 = deque([T("a"), T("b"), T("c"), T("d")])
music.drop_before(q4, 3)
check2("skipto+TRACK lands on the requested track",
       music.advance_queue(T("playing"), q4, LoopMode.TRACK, force_next=True).title == "c")

if fails2:
    print("%d REGRESSION CHECK(S) FAILED" % len(fails2))
    sys.exit(1)
print()
print("ALL QUEUE MANAGEMENT TESTS PASSED")
