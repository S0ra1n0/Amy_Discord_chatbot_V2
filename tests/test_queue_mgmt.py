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

from _check import check, finish

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

# ---- regression: youtu.be share links are single videos, not playlists ----
print()
print("=== is_playlist_url: youtu.be short links ===")
check("youtu.be/VIDEO?list=PL is a single video",
       music.is_playlist_url("https://youtu.be/dQw4w9WgXcQ?list=PLabc") is False)
check("youtu.be/VIDEO is a single video",
       music.is_playlist_url("https://youtu.be/dQw4w9WgXcQ") is False)
check("youtube.com/playlist?list= is still a playlist",
       music.is_playlist_url("https://www.youtube.com/playlist?list=PLabc") is True)
check("music.youtube.com playlist still detected",
       music.is_playlist_url("https://music.youtube.com/playlist?list=PLabc") is True)

# ---- regression: an explicit skip must beat TRACK loop ----
print()
print("=== advance_queue force_next (explicit skip) ===")
cur = T("playing")
q = deque([T("next")])
check("TRACK loop replays without force",
       music.advance_queue(cur, deque([T("next")]), LoopMode.TRACK).title == "playing")
check("force_next overrides TRACK loop",
       music.advance_queue(cur, q, LoopMode.TRACK, force_next=True).title == "next")

# QUEUE rotation is still honoured on a forced skip
q2 = deque([T("b")])
nxt = music.advance_queue(T("a"), q2, LoopMode.QUEUE, force_next=True)
check("forced skip under QUEUE still rotates", nxt.title == "b" and [t.title for t in q2] == ["a"])

# force_next changes nothing for OFF
q3 = deque([T("b")])
check("force_next is a no-op for OFF",
       music.advance_queue(T("a"), q3, LoopMode.OFF, force_next=True).title == "b")

# /skipto under TRACK loop reaches the requested track
q4 = deque([T("a"), T("b"), T("c"), T("d")])
music.drop_before(q4, 3)
check("skipto+TRACK lands on the requested track",
       music.advance_queue(T("playing"), q4, LoopMode.TRACK, force_next=True).title == "c")

# ---- /previous: history and stepping back ----
print()
print("=== advance_with_history ===")
A, B, C, D = T("A"), T("B"), T("C"), T("D")
h, q = deque(), deque([B])
check("moving on records the track left behind",
      music.advance_with_history(A, q, h, LoopMode.OFF) is B and list(h) == [A], list(h))
h, q = deque(), deque([B])
check("TRACK loop replaying a song records nothing",
      music.advance_with_history(A, q, h, LoopMode.TRACK) is A and not h, list(h))
h, q = deque(), deque([B])
check("a skip out of TRACK loop is recorded",
      music.advance_with_history(A, q, h, LoopMode.TRACK, force_next=True) is B and list(h) == [A])
h, q = deque(), deque([B])
check("QUEUE loop records it and still rotates it to the back",
      music.advance_with_history(A, q, h, LoopMode.QUEUE) is B and list(h) == [A]
      and list(q) == [A], (list(h), [t.title for t in q]))
h, q = deque(), deque()
check("the last track finishing is recorded too (so /previous works once the queue ends)",
      music.advance_with_history(A, q, h, LoopMode.OFF) is None and list(h) == [A])

print()
print("=== step_back ===")
h, q = deque([A, B]), deque([D])
prev = music.step_back(C, q, h)
check("goes to the most recent track", prev is B)
check("the interrupted track plays straight after it", list(q) == [B, C, D],
      [t.title for t in q])
# What advance_playback then does: take the front, record nothing
nxt = music.advance_with_history(C, q, h, LoopMode.OFF, going_back=True)
check("the going-back advance takes the front", nxt is B, nxt and nxt.title)
check("and records nothing - the interrupted track is already queued", list(h) == [A],
      [t.title for t in h])
prev = music.step_back(B, q, h)
check("a second /previous walks further back instead of bouncing", prev is A
      and [t.title for t in q] == ["A", "B", "C", "D"], [t.title for t in q])
check("no history -> None, queue untouched",
      music.step_back(A, q, deque()) is None and [t.title for t in q] == ["A", "B", "C", "D"])
h, q = deque([A]), deque()
check("with nothing playing it just queues the last track",
      music.step_back(None, q, h) is A and list(q) == [A])
# QUEUE loop: A was rotated to the back when it finished, so it's in the queue AND history
h, q = deque([A]), deque([C, A])
music.step_back(B, q, h)
check("QUEUE loop doesn't end up with the track twice", [t.title for t in q] == ["A", "B", "C"],
      [t.title for t in q])
check("...but a different queued copy of the same song is left alone",
      (lambda h2, q2: (music.step_back(B, q2, h2), [t.title for t in q2])[1])(
          deque([A]), deque([C, T("A")])) == ["A", "B", "C", "A"])

print()
print("=== GuildPlayer history ===")
p = music.GuildPlayer(1)
check("bounded at MAX_HISTORY", p.history.maxlen == music.MAX_HISTORY, p.history.maxlen)
p.current = A
p.stop_all()
check("/stop keeps what was playing reachable", list(p.history) == [A])
p.back_requested = True
p.stop_all()
check("/stop clears a pending go-back", p.back_requested is False)
p.reset()
check("leaving voice clears the history", not p.history)

# ---- /previous fixes (code review, 2026-10-03) ----
print()
print("=== /previous regressions ===")
# P2: QUEUE loop rotated A into the queue, then /play added D behind it. The old check only
# looked at queue[-1], so A stayed and played twice in every cycle.
h, q = deque([A]), deque([C, A, D])
music.step_back(B, q, h)
check("P2: the rotated copy is removed even with tracks queued after it",
      [t.title for t in q] == ["A", "B", "C", "D"], [t.title for t in q])
# P3: /previous moved B into the queue, then a /clearqueue emptied it before the advance ran.
# B was in neither the queue nor the history - unreachable.
h, q = deque(), deque()
check("P3: a going-back advance with an emptied queue plays nothing",
      music.advance_with_history(B, q, h, LoopMode.OFF, going_back=True) is None)
check("P3: ...but keeps the interrupted track reachable", list(h) == [B], [t.title for t in h])
# P1: /stop while the next track loads. `current` still holds X, which the advance already
# recorded; stop_all used to record it again, so /previous brought X back twice.
p = music.GuildPlayer(2)
X = T("X")
p.history.append(X)
p.current = X
p.stop_all()
check("P1: /stop during a load doesn't record the same track twice", list(p.history) == [X],
      [t.title for t in p.history])
p = music.GuildPlayer(3)
Y = T("Y")
p.history.append(Y)                         # something played before...
p.current = X                               # ...then X, on TRACK loop: never recorded yet
p.stop_all()
check("P1: ...but a looping track that was never recorded still is",
      list(p.history) == [Y, X], [t.title for t in p.history])

print()
print("=== spawn holds its task until it finishes ===")
import asyncio
import threading


async def _spawn_check():
    gate = asyncio.Event()

    async def work():
        await gate.wait()
    task = music.spawn(work())
    held = task in music._background
    gate.set()
    await task
    await asyncio.sleep(0)
    return held, task in music._background

held, still = asyncio.run(_spawn_check())
check("a spawned task is held while it runs", held)
check("and released once it finishes", not still)


async def _after_check():
    ran = asyncio.Event()

    async def advance():
        ran.set()
    loop = asyncio.get_running_loop()
    after = music.make_after_callback(loop, advance)
    threading.Thread(target=after, args=(None,)).start()   # discord.py calls it off-loop
    await asyncio.wait_for(ran.wait(), 2)
    return True

check("the after-callback advances the queue from another thread", asyncio.run(_after_check()))

_spawned = []
_real_spawn = music.spawn


def _recording_spawn(coro):
    _spawned.append(coro)
    return _real_spawn(coro)

music.spawn = _recording_spawn
try:
    asyncio.run(_after_check())
finally:
    music.spawn = _real_spawn
check("P4: ...and starts it through spawn, so the task is held", len(_spawned) == 1, _spawned)

finish("ALL QUEUE MANAGEMENT TESTS PASSED")
