import io, os, sys, asyncio, inspect, importlib.util
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
# Resolve the project root from this file, so the suite runs from any checkout
PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJ); os.chdir(PROJ)
import discord, music, ui

fails = []
def check(label, ok, got=""):
    print(("  PASS " if ok else "  FAIL ") + label)
    if not ok:
        fails.append(label); print("        got:", str(got)[:130])

def T(title, dur=200, i=0):
    return music.Track(title=title, query="https://youtu.be/%d" % i,
                       duration=dur, requested_by="me")

print("=== search_results_embed ===")
tracks = [T("Song %d" % i, 60 + i, i) for i in range(1, 6)]
e = ui.search_results_embed("rick astley", tracks)
check("lists every result", all(("`%d.`" % i) in e.description for i in range(1, 6)), e.description)
check("shows the query", "rick astley" in e.author.name, e.author.name)
check("has a hint footer", "Pick one" in (e.footer.text or ""), e.footer.text)
check("durations rendered", "1:01" in e.description, e.description)

long_q = "z" * 500
e2 = ui.search_results_embed(long_q, tracks)
check("long query clipped in author", len(e2.author.name) <= 256, len(e2.author.name))

print()
print("=== SearchResults view structure ===")
spec = importlib.util.spec_from_file_location("amy", os.path.join(PROJ, "Amy_chatbot_V2.py"))
amy = importlib.util.module_from_spec(spec); sys.modules["amy"] = amy
spec.loader.exec_module(amy)

class FakeGuild:
    id = 500
v = amy.SearchResults(tracks, requester_id=42, guild=FakeGuild())
check("has a finite timeout (not persistent)", v.timeout == 60.0, v.timeout)
check("one select component", len(v.children) == 1 and isinstance(v.children[0], discord.ui.Select))
opts = v.children[0].options
check("one option per result", len(opts) == 5, len(opts))
check("values are the indexes", [o.value for o in opts] == ["0", "1", "2", "3", "4"],
      [o.value for o in opts])
check("labels within Discord's 100-char limit", all(len(o.label) <= 100 for o in opts))
check("descriptions carry the duration", opts[0].description == "1:01", opts[0].description)
check("requester recorded", v.requester_id == 42)

print()
print("=== overlong titles are clipped for the menu ===")
huge = [music.Track(title="Q" * 400, query="https://youtu.be/x", duration=10, requested_by="me")]
v2 = amy.SearchResults(huge, requester_id=1, guild=FakeGuild())
check("label clipped to 100", len(v2.children[0].options[0].label) <= 100,
      len(v2.children[0].options[0].label))

print()
print("=== /search is declared correctly on the tree ===")
# Discord enforces required parameters client-side, so a missing query can no longer
# reach the handler at all - what matters now is that the declaration says so.
sch = amy.tree.get_command("search")
check("/search registered", sch is not None)
qp = {p.name: p for p in sch.parameters}["query"]
check("query is required", qp.required, qp.required)
check("query is described", bool(qp.description), qp.description)
check("/search defers (yt-dlp exceeds the 3s limit)",
      "response.defer(" in inspect.getsource(sch.callback))


print()
print("=== live search against YouTube ===")
async def live():
    res = await music.search_tracks("rick astley never gonna give you up", "tester")
    check("returns multiple results", len(res) >= 2, len(res))
    check("all have titles", all(t.title for t in res))
    check("all have playable queries", all(t.query.startswith("http") for t in res))
    check("respects the limit", len(res) <= music.SEARCH_RESULTS, len(res))
    short = await music.search_tracks("rick astley", "tester", limit=2)
    check("custom limit honoured", len(short) <= 2, len(short))
asyncio.run(live())

print()
if fails:
    print("%d CHECK(S) FAILED" % len(fails)); sys.exit(1)
print("ALL SEARCH TESTS PASSED")
