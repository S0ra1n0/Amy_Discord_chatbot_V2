import asyncio, io, os, sys, tempfile
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
# Resolve the project root from this file, so the suite runs from any checkout
PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJ)
os.chdir(PROJ)

import music

async def main():
    # 1. local file path bypasses yt-dlp entirely.
    # Local playback is sandboxed to MUSIC_DIR, so point it at a temp dir first.
    import tempfile as _tf
    base = _tf.mkdtemp(prefix="amy_resolve_")
    os.environ["MUSIC_DIR"] = base
    tmp = os.path.join(base, "song.mp3")
    open(tmp, "wb").close()
    t = await music.resolve_metadata(tmp, requested_by="tester")
    assert t.is_local and t.title == "song.mp3", t
    assert await music.resolve_stream_url(t) == os.path.realpath(tmp)
    os.remove(tmp)
    os.environ.pop("MUSIC_DIR", None)
    print("local file path: OK (no network, resolves to itself)")

    # 2. search by name
    print()
    print("resolving a search query via yt-dlp...")
    t = await music.resolve_metadata("rick astley never gonna give you up", requested_by="tester")
    print("  title    :", t.title)
    print("  duration :", music.format_duration(t.duration))
    print("  query    :", t.query[:70])
    assert t.title and t.title != "Unknown title"
    assert t.query.startswith("http"), "should store the canonical page URL for later re-resolution"
    assert not t.is_local

    # 3. stream URL resolved separately (the expiry-avoidance design)
    url = await music.resolve_stream_url(t)
    print("  stream   :", url[:70] + "...")
    assert url.startswith("http")
    print("search + stream resolution: OK")

    # 4. a bad query must raise, not hang or return junk
    print()
    try:
        await music.resolve_metadata("https://www.youtube.com/watch?v=__definitely_not_a_video__",
                                     requested_by="tester")
        print("bad query: FAILED - should have raised")
        return 1
    except Exception as e:
        print("bad query correctly raised:", type(e).__name__)

    print()
    print("ALL RESOLUTION TESTS PASSED")
    return 0

sys.exit(asyncio.run(main()) or 0)
