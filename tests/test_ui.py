import io, os, sys, importlib.util
from collections import deque
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
# Resolve the project root from this file, so the suite runs from any checkout
PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJ); os.chdir(PROJ)
import discord, music, ui
from music import LoopMode

fails = []
def check(label, ok, got=""):
    print(("  PASS " if ok else "  FAIL ") + label)
    if not ok:
        fails.append(label); print("        got:", str(got)[:120])

def T(title="Song", dur=215, thumb=None, url="https://youtu.be/x", who="me"):
    return music.Track(title=title, query=url, duration=dur, requested_by=who, thumbnail=thumb)

print("=== Reply defaults ===")
r = ui.Reply()
check("all fields default to None", r.content is None and r.embed is None and r.view is None)
r2 = ui.Reply(content="hi")
check("content can be set alone", r2.content == "hi" and r2.embed is None)

print()
print("=== now_playing_embed ===")
e = ui.now_playing_embed(T(thumb="https://img/t.jpg"), queue_len=3,
                         loop_mode=LoopMode.QUEUE, volume=0.5)
check("title is the track title", e.title == "Song", e.title)
check("author says Now playing", e.author.name == "Now playing", e.author.name)
check("colour is OK-green", e.colour and e.colour.value == ui.COLOUR_OK, e.colour)
check("clickable url set for http source", e.url == "https://youtu.be/x", e.url)
check("thumbnail attached", e.thumbnail.url == "https://img/t.jpg", e.thumbnail.url)
names = {f.name: f.value for f in e.fields}
check("duration formatted", names.get("Duration") == "3:35", names)
check("volume shown as percent", names.get("Volume") == "50%", names)
check("queue length shown", names.get("Up next") == "3 track(s)", names)
check("loop shown when not off", names.get("Loop") == "queue", names)

e2 = ui.now_playing_embed(T(), loop_mode=LoopMode.OFF)
check("loop field hidden when off", "Loop" not in {f.name for f in e2.fields})
check("queue field hidden when empty", "Up next" not in {f.name for f in e2.fields})
check("no thumbnail when track has none", e2.thumbnail.url is None, e2.thumbnail.url)

ep = ui.now_playing_embed(T(), paused=True)
check("paused state in author", ep.author.name == "Paused", ep.author.name)
check("paused uses warn colour", ep.colour.value == ui.COLOUR_WARN)
es = ui.now_playing_embed(T(), stopped=True)
check("stopped state in author", es.author.name == "Playback finished", es.author.name)

print()
print("=== local file has no clickable url ===")
local = music.Track(title="song.mp3", query=r"C:\music\song.mp3", requested_by="me")
check("url omitted for filesystem path", ui.now_playing_embed(local).url is None)

print()
print("=== queued / playlist embeds ===")
q = ui.queued_embed(T(), position=4)
check("queued shows position", {f.name: f.value for f in q.fields}.get("Position") == "4")
check("queued author", q.author.name == "Added to queue", q.author.name)
pl = ui.playlist_embed("My Mix", added=50, skipped=133)
vals = {f.name: f.value for f in pl.fields}
check("playlist shows added", vals.get("Queued") == "50 track(s)", vals)
check("playlist shows skipped", "133" in vals.get("Skipped", ""), vals)
pl2 = ui.playlist_embed("Mix", added=5, skipped=0)
check("skipped field hidden when zero", "Skipped" not in {f.name for f in pl2.fields})

print()
print("=== queue_embed delegates to render_queue ===")
big = deque([T(title="t%d" % i) for i in range(1, 26)])
qe = ui.queue_embed(T(title="cur"), big, LoopMode.OFF, page=2)
check("page 2 uses absolute positions", "`11.`" in qe.description and "`20.`" in qe.description)
check("page indicator present", "page 2/3" in qe.description)

print()
print("=== Discord length limits respected ===")
long_title = "X" * 900
e3 = ui.now_playing_embed(T(title=long_title))
check("title clipped to 256", len(e3.title) <= ui.MAX_EMBED_TITLE, len(e3.title))
huge = deque([T(title="Y" * 200) for _ in range(200)])
qe2 = ui.queue_embed(None, huge, LoopMode.OFF, page=1)
check("description clipped to 4096", len(qe2.description) <= ui.MAX_EMBED_DESC, len(qe2.description))

print()
print("=== PlayerControls is a legal persistent view ===")
spec = importlib.util.spec_from_file_location("amy", os.path.join(PROJ, "Amy_chatbot_V2.py"))
amy = importlib.util.module_from_spec(spec); sys.modules["amy"] = amy
spec.loader.exec_module(amy)
v = amy.PlayerControls()
check("timeout is None (required for add_view)", v.timeout is None, v.timeout)
items = list(v.children)
check("exactly three buttons", len(items) == 3, len(items))
check("every button has a custom_id",
      all(getattr(i, "custom_id", None) for i in items),
      [getattr(i, "custom_id", None) for i in items])
check("custom_ids are unique",
      len({i.custom_id for i in items}) == 3)
labels = [i.label for i in items]
check("default label is Pause", labels[0] == "Pause", labels)
check("paused=True flips the label to Resume",
      amy.PlayerControls(paused=True).children[0].label == "Resume")
check("Skip and Stop present", labels[1] == "Skip" and labels[2] == "Stop", labels)

print()
if fails:
    print("%d CHECK(S) FAILED" % len(fails)); sys.exit(1)
# The progress bar replaces the Duration field only while something is actually playing.
# On the finished card a position would be meaningless.
_t = music.Track(title="Song", query="https://youtu.be/x", duration=260, requested_by="me")

_live = ui.now_playing_embed(_t, elapsed=63)
_names = [f.name for f in _live.fields]
assert "Position" in _names, _names
assert "Duration" not in _names, "Position replaces Duration while playing"
_pos = [f for f in _live.fields if f.name == "Position"][0]
assert "1:03" in _pos.value and "4:20" in _pos.value, _pos.value
assert _pos.inline is False, "the bar needs full width to read as a scrubber"

_plain = ui.now_playing_embed(_t)
assert "Duration" in [f.name for f in _plain.fields], "no elapsed -> keep Duration"
assert "Position" not in [f.name for f in _plain.fields]

_done = ui.now_playing_embed(_t, stopped=True, elapsed=63)
assert "Position" not in [f.name for f in _done.fields], "finished card shows no position"

_live_stream = ui.now_playing_embed(
    music.Track(title="Radio", query="https://x.test/s", duration=None, requested_by="me"),
    elapsed=42)
_lp = [f for f in _live_stream.fields if f.name == "Position"][0]
assert "live" in _lp.value, _lp.value
print("now_playing progress bar: OK (replaces Duration, hidden when finished)")

print("ALL UI TESTS PASSED")
