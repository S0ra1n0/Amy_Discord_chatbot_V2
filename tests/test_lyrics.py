"""
/lyrics: title -> search terms, choosing a LRCLIB record, timestamp stripping, paging, the
HTTP call (httpx.MockTransport) and the command wired end to end with the search faked.
Fully offline - LRCLIB is never contacted.
"""
import asyncio
import io
import logging
import os
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
# Resolve the project root from this file, so the suite runs from any checkout
PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJ)

import httpx

from _fakes import (Guild, Interaction, Member, VoiceChannel, VoiceClient, load_bot,
                    make_track, run_music, seed_queue)
from _check import check, finish

amy = load_bot()
import lyrics
import ui

OWNER, USER, GUILD = 42, 99, 500


def rec(artist="Daft Punk", title="One More Time", duration=320, plain="la la\nla la",
        synced=None, instrumental=False):
    return {"artistName": artist, "trackName": title, "duration": duration,
            "plainLyrics": plain, "syncedLyrics": synced, "instrumental": instrumental}


print("=== search_terms: noisy titles become artist + song ===")
T = lyrics.search_terms
t = T("【Kasane Teto SV2】Heat Abnormal (English Cover)")
check("Japanese brackets and cover tag dropped", (t.artist, t.title) == (None, "Heat Abnormal"), t)
t = T("Daft Punk - One More Time (Official Video)")
check("Artist - Song, bracket noise dropped",
      (t.artist, t.title) == ("Daft Punk", "One More Time"), t)
t = T("Artist - Song feat. Someone Else")
check("feat. credit dropped from the song", (t.artist, t.title) == ("Artist", "Song"), t)
t = T("Artist feat. Guest - Song")
check("feat. credit dropped from the artist", (t.artist, t.title) == ("Artist", "Song"), t)
t = T("Artist - Song (feat. Guest) [4K]")
check("bracketed feat. dropped", (t.artist, t.title) == ("Artist", "Song"), t)
t = T("Song Name [4K Remaster]")
check("no dash: title only", (t.artist, t.title) == (None, "Song Name"), t)
t = T("Artist - Song - Official Video")
check("trailing unbracketed noise dropped", (t.artist, t.title) == ("Artist", "Song"), t)
t = T("Artist - Song | Topic Channel")
check("text after ' | ' dropped", (t.artist, t.title) == ("Artist", "Song"), t)
t = T("Spider-Man Theme")
check("a hyphen inside a word is not a split", (t.artist, t.title) == (None, "Spider-Man Theme"), t)
t = T("初音ミク - メルト")
check("non-Latin artist and title kept", (t.artist, t.title) == ("初音ミク", "メルト"), t)
t = T("Artist - (Official Video)")
check("a title that is only noise still searches something", t.title == "Artist", t)
t = T("Artist – Song")
check("en dash splits too", (t.artist, t.title) == ("Artist", "Song"), t)
check("shown / q forms",
      T("A - B").shown == "A - B" and T("A - B").q == "A B" and T("B").shown == "B", T("A - B"))
check("terms are capped", len(T("x" * 1000).title) <= lyrics.MAX_TERMS_CHARS)

print()
print("=== strip_timestamps ===")
synced = "[ar:Someone]\n[length:3:20]\n[00:12.34] Hello there\n[00:15.00][00:40.10]Again\n[01:02] <01:03.00>end"
out = lyrics.strip_timestamps(synced)
check("stamps removed", "[00" not in out and "<01" not in out, out)
check("metadata tag lines removed", "[ar" not in out and "length" not in out, out)
check("words kept in order", out.split("\n") == ["Hello there", "Again", "end"], out.split("\n"))

print()
print("=== local files: extension and track number dropped ===")
t = T(lyrics.local_title("Artist - Song.mp3"))
check("local: extension dropped", (t.artist, t.title) == ("Artist", "Song"), t)
t = T(lyrics.local_title("01 - Artist - Song.flac"))
check("local: track number and extension dropped", (t.artist, t.title) == ("Artist", "Song"), t)
check("local: '07. Song.ogg' form", lyrics.local_title("07. Song.ogg") == "Song")
check("local: a name that is all number keeps something", lyrics.local_title("1984.mp3") == "1984")
check("local: a number-named artist survives", lyrics.local_title("311 - Amber.mp3") == "311 - Amber",
      lyrics.local_title("311 - Amber.mp3"))
check("local: dots inside the name survive", lyrics.local_title("Mr. Brightside.mp3") == "Mr. Brightside")

print()
print("=== strip_timestamps keeps stanza breaks ===")
check("blank line between stanzas kept",
      lyrics.strip_timestamps("[00:01.00] one\n\n[00:04.00] three") == "one\n\nthree",
      repr(lyrics.strip_timestamps("[00:01.00] one\n\n[00:04.00] three")))
check("stamp-only line becomes a blank line",
      lyrics.strip_timestamps("[00:01.00] a\n[00:02.00] \n[00:03.00] b") == "a\n\nb",
      repr(lyrics.strip_timestamps("[00:01.00] a\n[00:02.00] \n[00:03.00] b")))
check("CRLF input handled",
      lyrics.strip_timestamps("[00:01.00] a\r\n\r\n[00:03.00] b") == "a\n\nb")

print()
print("=== choose_result ===")
c = lyrics.choose_result
check("empty list: None", c([]) is None)
check("records without lyrics: None",
      c([rec(plain=""), rec(plain=None, synced=None), rec(plain="  ")]) is None)
check("no duration: first usable wins",
      c([rec(plain=""), rec(title="B"), rec(title="C")]).title == "B")
res = [rec(title="Live cut", duration=400), rec(title="Right one", duration=318),
       rec(title="Closer", duration=321)]
check("duration: closest within tolerance wins (not the first)",
      c(res, 320).title == "Closer", c(res, 320))
check("duration: first usable when none is within tolerance",
      c(res, 100).title == "Live cut", c(res, 100))
check("duration just past tolerance does not count",
      c([rec(title="First", duration=300), rec(title="Far", duration=326)], 320).title == "First")
check("duration at the tolerance edge counts",
      c([rec(title="First", duration=300), rec(title="Edge", duration=325)], 320).title == "Edge")
check("plain lyrics preferred over synced in the same record",
      c([rec(plain="plain words", synced="[00:01.00] synced words")]).text == "plain words")
f = c([rec(plain=None, synced="[00:01.00] only synced\n[00:02.00] second")])
check("synced only: stamps stripped", f.text == "only synced\nsecond", f.text)
f = c([rec(plain=None, instrumental=True, title="Wordless")])
check("instrumental flagged", f.instrumental and f.text is None and f.title == "Wordless", f)
check("a usable record before an instrumental one still wins by order",
      c([rec(title="Sung"), rec(plain=None, instrumental=True)]).title == "Sung")
check("heading", c([rec()]).heading == "Daft Punk - One More Time")
check("junk entries ignored", c(["x", None, 3, rec(title="Ok")]).title == "Ok")

print()
print("=== split_pages / lyric_pages ===")
text = "\n".join("line %03d" % i for i in range(300))             # 300 short lines
pages = lyrics.split_pages(text, 100)
check("every page within the limit", all(len(p) <= 100 for p in pages), [len(p) for p in pages])
check("only splits on line boundaries",
      all(l.startswith("line ") and len(l) == 8 for p in pages for l in p.split("\n")))
check("no line lost or reordered", "\n".join(pages).split("\n") == text.split("\n"))
check("short text is one page", lyrics.split_pages("a\nb") == ["a\nb"])
check("blank-only text gives no pages", lyrics.split_pages("  \n \n") == [])
long_line = ("word " * 100).strip()                                 # 499 chars, no newline
pages = lyrics.split_pages("intro\n" + long_line + "\noutro", 120)
check("a very long single line is cut within the limit",
      all(len(p) <= 120 for p in pages), [len(p) for p in pages])
check("...at spaces, losing no words",
      " ".join(" ".join(pages).split()) == "intro " + long_line + " outro")
pages = lyrics.split_pages("x" * 250, 100)
check("an unbroken string is hard-cut", [len(p) for p in pages] == [100, 100, 50], [len(p) for p in pages])
try:
    lyrics.split_pages("\\--", 1)
    check("split_pages refuses limit < 2 (it would loop forever)", False, "no error")
except ValueError:
    check("split_pages refuses limit < 2 (it would loop forever)", True)
pages = lyrics.split_pages("a" * 99 + "\\*" + "b" * 10, 100)
check("a hard cut never separates a backslash from the char it escapes",
      not pages[0].endswith("\\") and "".join(pages).replace("\\", "") == "a" * 99 + "*" + "b" * 10
      and all(len(p) <= 100 for p in pages), pages)
pages = lyrics.split_pages("a" * 98 + "\\\\" + "b" * 10, 100)
check("...but an escaped backslash pair may be cut after",
      pages[0] == "a" * 98 + "\\\\", pages)
pages = lyrics.split_pages("a" * 100 + "- not a list", 100)
check("a continuation page starting with '-' is escaped", pages[1].startswith("\\- ") , pages)
pages = lyrics.split_pages("word " * 30 + "# x " + "y" * 20, 150)
check("a continuation page starting with '#' is escaped",
      any(p.startswith("\\#") for p in pages[1:]), pages)
pages = lyrics.split_pages("a" * 100 + ">quote", 100)
check("a continuation page starting with '>' is escaped", pages[1].startswith("\\>"), pages)
pages, _ = lyrics.lyric_pages("x" * 4000 + "-" + "y" * 50)
check("lyric_pages: continuation marker escaped and page within limit",
      pages[1].startswith("\\-") and all(len(p) <= lyrics.PAGE_CHARS for p in pages), pages)
big = "\n".join("verse line number %04d" % i for i in range(2000))   # ~44k chars
pages, truncated = lyrics.lyric_pages(big)
check("capped at MAX_PAGES", len(pages) == lyrics.MAX_PAGES, len(pages))
check("flagged as truncated", truncated)
check("every page fits an embed description", all(len(p) <= lyrics.PAGE_CHARS < 4096 for p in pages))
pages, truncated = lyrics.lyric_pages("short\nsong")
check("short song: one page, not truncated", pages == ["short\nsong"] and not truncated)

print()
print("=== lyrics_embeds ===")
embs = ui.lyrics_embeds("Daft Punk - One More Time", ["p1", "p2", "p3"], True, lyrics.CONTINUES_NOTE)
check("one embed per page", len(embs) == 3)
check("title shows the match and page", embs[1].title == "Daft Punk - One More Time (2/3)", embs[1].title)
check("every footer credits LRCLIB", all(e.footer.text == "Lyrics from LRCLIB" for e in embs))
check("continuation note only on the last page",
      "lrclib.net" in (embs[2].description or "") and "lrclib.net" not in (embs[0].description or ""))
one = ui.lyrics_embeds("A - B", ["only"], False, lyrics.CONTINUES_NOTE)
check("single page: plain title, no note", one[0].title == "A - B" and one[0].description == "only")
check("long heading still within the title limit",
      len(ui.lyrics_embeds("h" * 500, ["x", "y"], False, "n")[0].title) <= 256)

print()
print("=== search_lrclib over HTTP (MockTransport) ===")
seen = []


def serve(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def ok_handler(request):
    seen.append(request)
    return httpx.Response(200, json=[rec()])


got = asyncio.run(lyrics.search_lrclib({"q": "one more time"}, serve(ok_handler)))
check("returns the records", len(got) == 1 and got[0]["trackName"] == "One More Time", got)
check("sends the query", seen[0].url.params.get("q") == "one more time", str(seen[0].url))
check("sends a descriptive User-Agent", seen[0].headers["user-agent"].startswith("Amy-Discord-Bot"),
      seen[0].headers["user-agent"])
check("empty list is a clean answer",
      asyncio.run(lyrics.search_lrclib({"q": "x"}, serve(lambda r: httpx.Response(200, json=[])))) == [])


def expect_error(label, handler):
    try:
        asyncio.run(lyrics.search_lrclib({"q": "x"}, serve(handler)))
    except lyrics.LyricsError as e:
        check(label, True)
        return
    except Exception as e:                                          # noqa: BLE001
        check(label, False, "wrong exception %r" % e)
        return
    check(label, False, "no error raised")


expect_error("HTTP 503 -> LyricsError", lambda r: httpx.Response(503, json={"message": "busy"}))
expect_error("not JSON -> LyricsError", lambda r: httpx.Response(200, text="<html>oops"))
expect_error("JSON of the wrong shape -> LyricsError", lambda r: httpx.Response(200, json={"a": 1}))


def boom(request):
    raise httpx.ConnectError("no route")


expect_error("connection failure -> LyricsError", boom)


async def slow(request):
    await asyncio.sleep(5)
    return httpx.Response(200, json=[])


old_timeout = lyrics.TIMEOUT
lyrics.TIMEOUT = 0.2
try:
    async def timed():
        return await asyncio.wait_for(lyrics.search_lrclib({"q": "x"}, serve(slow)), 4)
    try:
        asyncio.run(timed())
        check("a hung server hits the lookup deadline", False, "no error")
    except lyrics.LyricsError:
        check("a hung server hits the lookup deadline", True)
    except asyncio.TimeoutError:
        check("a hung server hits the lookup deadline", False, "deadline not enforced")
finally:
    lyrics.TIMEOUT = old_timeout

print()
print("=== find_lyrics: artist pair first, then free text ===")
calls = []
orig_search = lyrics.search_lrclib


async def fake_search(params, client=None):
    calls.append(dict(params))
    return fake_search.answers.pop(0)


lyrics.search_lrclib = fake_search
try:
    fake_search.answers = [[], [rec(title="Via q")]]
    f = asyncio.run(lyrics.find_lyrics(lyrics.Terms("Artist", "Song")))
    check("falls back to a free-text search", f is not None and f.title == "Via q", f)
    check("first attempt used the pair, second used q",
          calls == [{"track_name": "Song", "artist_name": "Artist"}, {"q": "Artist Song"}], calls)
    calls.clear()
    fake_search.answers = [[rec(title="Direct")]]
    f = asyncio.run(lyrics.find_lyrics(lyrics.Terms("Artist", "Song")))
    check("a hit on the pair makes only one request", len(calls) == 1 and f.title == "Direct", calls)
    calls.clear()
    fake_search.answers = [[]]
    check("no artist: one q search, None when empty",
          asyncio.run(lyrics.find_lyrics(lyrics.Terms(None, "Song"))) is None and calls == [{"q": "Song"}])
    # A known duration: a right-length record from the free-text search beats the pair
    # search's out-of-tolerance guess (e.g. a 400 s live cut).
    calls.clear()
    fake_search.answers = [[rec(title="Live cut", duration=400)],
                           [rec(title="Live cut", duration=400),
                            rec(artist="Artist", title="Studio", duration=321)]]
    f = asyncio.run(lyrics.find_lyrics(lyrics.Terms("Artist", "Song"), 320))
    check("duration: q search's in-tolerance record beats the first attempt's fallback",
          f is not None and f.title == "Studio" and len(calls) == 2, (f, calls))
    calls.clear()
    fake_search.answers = [[rec(title="Live cut", duration=400)], [rec(title="Other", duration=100)]]
    f = asyncio.run(lyrics.find_lyrics(lyrics.Terms("Artist", "Song"), 320))
    check("duration: neither in tolerance -> the first attempt's fallback",
          f is not None and f.title == "Live cut" and len(calls) == 2, (f, calls))
    calls.clear()
    fake_search.answers = [[rec(title="Right", duration=319)]]
    f = asyncio.run(lyrics.find_lyrics(lyrics.Terms("Artist", "Song"), 320))
    check("duration: an in-tolerance first attempt needs no second request",
          f.title == "Right" and len(calls) == 1, calls)
    calls.clear()
    fake_search.answers = [[rec(title="Live cut", duration=400)]]
    f = asyncio.run(lyrics.find_lyrics(lyrics.Terms("Artist", "Song"), None))
    check("no duration: the first attempt's record is used, no second request",
          f.title == "Live cut" and len(calls) == 1, calls)
    calls.clear()
    fake_search.answers = [[], [rec(title="Only via q", duration=100)]]
    f = asyncio.run(lyrics.find_lyrics(lyrics.Terms("Artist", "Song"), 320))
    check("duration: a fallback from the q search is still returned",
          f is not None and f.title == "Only via q", f)
    # Wrong-song guard: the free-text round only accepts a right-length record whose names
    # loosely match; otherwise the first search's guess stands.
    calls.clear()
    fake_search.answers = [[rec(artist="Artist", title="Song", duration=230)],
                           [rec(artist="Artist", title="Song", duration=230),
                            rec(artist="Other Band", title="Different", duration=268)]]
    f = asyncio.run(lyrics.find_lyrics(lyrics.Terms("Artist", "Song"), 270))
    check("duration: a right-length cover with unrelated names loses to the first guess",
          f is not None and (f.artist, f.title) == ("Artist", "Song") and len(calls) == 2, (f, calls))
    calls.clear()
    fake_search.answers = [[], [rec(artist="Other Band", title="Different", duration=268),
                                rec(artist="ARTIST", title="Remix", duration=269)]]
    f = asyncio.run(lyrics.find_lyrics(lyrics.Terms("Artist", "Song"), 270))
    check("duration: a right-length record matching the artist (casefolded) is accepted",
          f is not None and f.title == "Remix", f)
    calls.clear()
    fake_search.answers = [[], [rec(artist="Artist", title="Song", duration=230),
                                rec(artist="Other Band", title="Different", duration=268)]]
    f = asyncio.run(lyrics.find_lyrics(lyrics.Terms("Artist", "Song"), 270))
    check("duration: empty pair search, the fallback ignores unrelated right-length records",
          f is not None and (f.artist, f.title) == ("Artist", "Song"), f)
    calls.clear()
    fake_search.answers = [[rec(artist="Artist", title="Song", duration=400)],
                           [rec(artist="Artist", title="Song", duration=400),
                            rec(artist="Cover Guy", title="Song", duration=270)]]
    f = asyncio.run(lyrics.find_lyrics(lyrics.Terms("Artist", "Song"), 270))
    check("duration: a same-titled cover by another artist does not win",
          f is not None and f.artist == "Artist", f)
    check("names_match without an artist falls back to the title",
          lyrics.names_match(rec(artist="Zed", title="Song"), lyrics.Terms(None, "song")))
    check("names_match is crosswise and loose",
          lyrics.names_match(rec(artist="Song", title="Artist feat. X"), lyrics.Terms("Artist", "Song"))
          and not lyrics.names_match(rec(artist="Q", title="Z"), lyrics.Terms("Artist", "Song")))
finally:
    lyrics.search_lrclib = orig_search

print()
print("=== escaping community text ===")
BS = chr(92)
NL = chr(10)
evil = NL.join(["intro [x](https://e.vil) and *a* ||spoiler|| `code`",
                "# not a heading", "> not a quote", "- not a list"])
esc = lyrics.escape_markdown(evil)
check("masked link brackets escaped", BS + "[x" + BS + "](https://e.vil)" in esc, esc)
check("emphasis, spoiler and code escaped",
      BS + "*a" + BS + "*" in esc and BS + "|" + BS + "|spoiler" in esc and BS + "`code" + BS + "`" in esc, esc)
check("line-start heading, quote and list markers escaped",
      NL + BS + "# not" in esc and NL + BS + "> not" in esc and NL + BS + "- not" in esc, esc)
check("plain text untouched", lyrics.escape_markdown("plain (words), ok!") == "plain (words), ok!")
pages, _ = lyrics.lyric_pages(evil)
check("lyric_pages escapes", BS + "[x" + BS + "]" in pages[0], pages[0])
heavy = NL.join("*" * 70 for _ in range(200))                       # escaping doubles every line
pages, truncated = lyrics.lyric_pages(heavy)
check("pages stay within the limit AFTER escaping",
      all(len(p) <= lyrics.PAGE_CHARS for p in pages), [len(p) for p in pages])
check("...and escaping really did add characters",
      sum(len(p) for p in pages) > len(heavy[:lyrics.PAGE_CHARS]))

print()
print("=== /lyrics wired into the bot ===")
CH = VoiceChannel(10, "Chung")


def setup(current_title="Daft Punk - One More Time (Official Video)", duration=320):
    player = seed_queue(amy, GUILD, [], current=None)
    if current_title:
        player.current = make_track(amy, current_title, duration=duration)
    member = Member(USER, CH, name="userA")
    guild = Guild(OWNER, member, VoiceClient(CH), guild_id=GUILD)
    return player, Interaction(member, guild)


class FakeLRC:
    """Stands in for lyrics.search_lrclib; records what it was asked."""
    def __init__(self, answers=None, error=None):
        self.answers, self.error, self.calls = list(answers or []), error, []

    async def __call__(self, params, client=None):
        self.calls.append(dict(params))
        if self.error:
            raise self.error
        return self.answers.pop(0) if self.answers else []


def with_fake(fake):
    lyrics.search_lrclib = fake


class Records(logging.Handler):
    def __init__(self):
        super().__init__(logging.WARNING)
        self.records = []

    def emit(self, record):
        self.records.append(record)


try:
    fake = FakeLRC()
    with_fake(fake)
    _, it = setup(current_title=None)
    out = run_music(amy, "lyrics", [""], interaction=it)
    check("nothing playing: polite refusal", "Nothing is playing" in out and "/lyrics" in out, out)
    check("nothing playing: no lookup made", fake.calls == [])

    fake = FakeLRC([[rec(title="One More Time", duration=500), rec(title="One More Time", duration=321,
                                                                    plain="the right words")]])
    with_fake(fake)
    _, it = setup()
    out = run_music(amy, "lyrics", [""], interaction=it)
    check("current track: searched with cleaned artist and title",
          fake.calls[0] == {"track_name": "One More Time", "artist_name": "Daft Punk"}, fake.calls)
    check("current track: duration picks the right record", "the right words" in out, out)
    emb = it.sent[0].embed
    check("embed titled with the matched Artist - Title", emb.title == "Daft Punk - One More Time", emb.title)
    check("footer credits LRCLIB", emb.footer.text == "Lyrics from LRCLIB", emb.footer.text)
    check("one message for a short song", len(it.sent) == 1, len(it.sent))

    fake = FakeLRC([[rec(artist="Teto", title="Heat Abnormal", plain="english words")]])
    with_fake(fake)
    _, it = setup()
    out = run_music(amy, "lyrics", ["  heat abnormal  "], interaction=it)
    check("explicit query searched, whitespace trimmed", fake.calls[0] == {"q": "heat abnormal"}, fake.calls)
    check("explicit query beats the current track", "english words" in out, out)

    fake = FakeLRC([[rec(plain=None, instrumental=True, title="Wordless")]])
    with_fake(fake)
    _, it = setup()
    out = run_music(amy, "lyrics", ["wordless"], interaction=it)
    check("instrumental says so", "instrumental" in out.lower() and "Wordless" in out, out)

    fake = FakeLRC([[], [], []])
    with_fake(fake)
    _, it = setup()
    out = run_music(amy, "lyrics", [""], interaction=it)
    check("not found: says so", "Couldn't find lyrics" in out, out)
    check("not found: shows the terms used", "Daft Punk - One More Time" in out, out)
    check("not found: suggests the explicit form", "/lyrics <artist - title>" in out, out)
    check("not found: bold part is the raw current title",
          "**Daft Punk - One More Time (Official Video)**" in out, out)
    check("not found: code part is the searched terms", "`Daft Punk - One More Time`" in out, out)
    check("not found: the two parts differ", out.count("Official Video") == 1, out)
    fake = FakeLRC([[], []])
    with_fake(fake)
    _, it = setup(current_title="Bad `tick` Song - Name")
    out = run_music(amy, "lyrics", [""], interaction=it)
    code_parts = out.split("I searched for ")[1].split(").")[0]
    check("not found: a backtick in the title cannot break the inline code",
          code_parts.count("`") == 2, code_parts)
    fake = FakeLRC([[], []])
    with_fake(fake)
    _, it = setup()
    out = run_music(amy, "lyrics", ["Some Artist - Some Song (Live)"], interaction=it)
    check("not found with a typed query: bold is what was typed",
          "**Some Artist - Some Song (Live)**" in out and "`Some Artist - Some Song`" in out, out)
    check("not found: sent no lyrics embed", all(not (m.embed and m.embed.footer.text) for m in it.sent))

    evil_name = "[Free Nitro](https://phish.example) ||x|| **b**"
    fake = FakeLRC([[], []])
    with_fake(fake)
    _, it = setup()
    out = run_music(amy, "lyrics", [evil_name], interaction=it)
    check("not found: typed query is escaped (no masked link, spoiler or bold break)",
          BS + "[Free Nitro" + BS + "](https://phish.example)" in out and BS + "|" + BS + "|x" in out
          and BS + "*" + BS + "*b" in out, out)
    fake = FakeLRC([[], []])
    with_fake(fake)
    _, it = setup(current_title="[Free Nitro](https://phish.example) ||x||")
    out = run_music(amy, "lyrics", [""], interaction=it)
    check("not found: YouTube title is escaped",
          BS + "[Free Nitro" in out and BS + "|" + BS + "|x" in out, out)
    fake = FakeLRC([[rec(artist="[Free Nitro](https://phish.example)", title="||x||",
                        plain=None, instrumental=True)]])
    with_fake(fake)
    _, it = setup()
    out = run_music(amy, "lyrics", ["wordless"], interaction=it)
    check("instrumental: community artist/title escaped",
          BS + "[Free Nitro" + BS + "](https://phish.example)" in out and BS + "|" + BS + "|x" in out, out)

    fake = FakeLRC([[rec(artist="Artist", title="Song", plain="local words")]])
    with_fake(fake)
    player, it = setup()
    player.current = amy.music.Track(title="01 - Artist - Song.flac", query="x", duration=None,
                                     requested_by="userA", is_local=True)
    run_music(amy, "lyrics", [""], interaction=it)
    check("local file: searched without number and extension",
          fake.calls[0] == {"track_name": "Song", "artist_name": "Artist"}, fake.calls)

    big = "\n".join("verse line number %04d" % i for i in range(2000))
    fake = FakeLRC([[rec(plain=big)]])
    with_fake(fake)
    _, it = setup()
    run_music(amy, "lyrics", [""], interaction=it)
    check("long song: capped at MAX_PAGES messages", len(it.sent) == lyrics.MAX_PAGES, len(it.sent))
    check("each message carries one embed within the limits",
          all(m.embed is not None and len(m.embed.description) <= 4096 for m in it.sent))
    check("each message's embeds stay under the 6000 total",
          all(len(m.embed.title) + len(m.embed.description) + len(m.embed.footer.text) <= 6000
              for m in it.sent))
    check("last page says it continues on lrclib.net", "lrclib.net" in it.sent[-1].embed.description)

    for label, err in (("timeout", lyrics.LyricsError("TimeoutError: ")),
                       ("HTTP failure", lyrics.LyricsError("HTTPStatusError: 503"))):
        with_fake(FakeLRC(error=err))
        _, it = setup()
        handler = Records()
        logging.getLogger("amy").addHandler(handler)
        try:
            out = run_music(amy, "lyrics", [""], interaction=it)
        finally:
            logging.getLogger("amy").removeHandler(handler)
        check("%s: friendly message" % label, "couldn't reach" in out.lower(), out)
        check("%s: no traceback text leaks to Discord" % label, "Error" not in out, out)
        check("%s: logged at WARNING" % label,
              any(r.levelno == logging.WARNING and "Lyrics" in r.getMessage() for r in handler.records),
              [r.getMessage() for r in handler.records])

    print()
    print("=== the slash command ===")
    cmd = amy.tree.get_command("lyrics")
    check("/lyrics registered", cmd is not None)
    check("query is optional", not {p.name: p for p in cmd.parameters}["song"].required)
    with_fake(FakeLRC([[rec(plain="slash words")]]))
    _, it = setup()
    asyncio.run(cmd.callback(it, "anything"))
    check("slash callback defers and posts the lyrics",
          it.response.is_done() and "slash words" in "".join(m.embed.description for m in it.sent
                                                           if m.embed))
finally:
    lyrics.search_lrclib = orig_search

print()
print("=== lyrics are never spoken ===")
import inspect
src = inspect.getsource(amy.music_lyrics)
check("music_lyrics never announces", "announce" not in src and "speak" not in src)

finish("ALL LYRICS TESTS PASSED")
