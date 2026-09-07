"""Verify the local-file sandbox actually contains /play."""
import asyncio, io, os, sys, tempfile
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
# Resolve the project root from this file, so the suite runs from any checkout
PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJ)
os.chdir(PROJ)
import music

fails = []
def check(label, ok):
    print(("  PASS " if ok else "  FAIL ") + label)
    if not ok:
        fails.append(label)

# --- with MUSIC_DIR unset, local playback must be completely off ---
os.environ.pop("MUSIC_DIR", None)
print("=== MUSIC_DIR unset -> local playback disabled ===")
check(".env not reachable", music.resolve_local_path(os.path.join(PROJ, ".env")) is None)
check("hosts file not reachable",
      music.resolve_local_path(r"C:\Windows\System32\drivers\etc\hosts") is None)
check("get_music_dir() is None", music.get_music_dir() is None)

t = asyncio.run(music.resolve_metadata.__wrapped__(".env", "x")) if hasattr(
    music.resolve_metadata, "__wrapped__") else None

# --- with MUSIC_DIR set, only files inside it are reachable ---
base = tempfile.mkdtemp(prefix="amy_music_")
inside = os.path.join(base, "song.mp3")
open(inside, "wb").close()
sub = os.path.join(base, "sub")
os.makedirs(sub, exist_ok=True)
nested = os.path.join(sub, "deep.mp3")
open(nested, "wb").close()
os.environ["MUSIC_DIR"] = base

print()
print("=== MUSIC_DIR set -> only files inside are reachable ===")
check("file in music dir resolves", music.resolve_local_path("song.mp3") == os.path.realpath(inside))
check("nested file resolves", music.resolve_local_path(r"sub\deep.mp3") == os.path.realpath(nested))
check("absolute path inside dir resolves", music.resolve_local_path(inside) == os.path.realpath(inside))
check("missing file in dir -> None", music.resolve_local_path("nope.mp3") is None)

print()
print("=== escape attempts must all be rejected ===")
attacks = [
    r"..\..\..\Windows\System32\drivers\etc\hosts",
    os.path.join(PROJ, ".env"),
    r"C:\Windows\System32\drivers\etc\hosts",
    "../" * 12 + "Windows/System32/drivers/etc/hosts",
    os.path.join(base, "..", os.path.basename(PROJ), ".env"),
]
for a in attacks:
    got = music.resolve_local_path(a)
    check("rejected: %s" % (a[:52]), got is None)

# sibling directory with a prefix-matching name must not be reachable
sibling = base + "_evil"
os.makedirs(sibling, exist_ok=True)
evil = os.path.join(sibling, "x.mp3")
open(evil, "wb").close()
check("prefix-similar sibling dir rejected", music.resolve_local_path(evil) is None)

print()
print("=== resolve_metadata honours the sandbox ===")
async def main():
    tr = await music.resolve_metadata("song.mp3", "tester")
    check("in-dir file -> local Track", tr.is_local and tr.title == "song.mp3")
asyncio.run(main())

# cleanup
import shutil
os.environ.pop("MUSIC_DIR", None)
shutil.rmtree(base, ignore_errors=True)
shutil.rmtree(sibling, ignore_errors=True)

print()
if fails:
    print("%d CHECK(S) FAILED" % len(fails))
    sys.exit(1)
print("ALL SECURITY CHECKS PASSED")
