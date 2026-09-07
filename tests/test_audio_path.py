"""Verify the opus-passthrough path engages on a real YouTube stream."""
import asyncio, io, os, sys
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
# Resolve the project root from this file, so the suite runs from any checkout
PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJ); os.chdir(PROJ)
import discord, music
FFMPEG = music.find_ffmpeg()

async def main():
    track = await music.resolve_metadata("rick astley never gonna give you up", "tester")
    url = await music.resolve_stream_url(track)
    print("resolved:", track.title[:60])

    src = await music.build_source(url, 1.0, FFMPEG)
    print("volume 100% ->", type(src).__name__)
    assert isinstance(src, discord.FFmpegOpusAudio), "full volume should pass opus through"
    src.cleanup()

    src = await music.build_source(url, 0.5, FFMPEG)
    print("volume  50% ->", type(src).__name__)
    assert isinstance(src, discord.PCMVolumeTransformer), "below full volume must decode to PCM"
    assert abs(src.volume - 0.5) < 0.001
    src.cleanup()

    src = await music.build_source(url, 1.0, FFMPEG)
    frames = 0
    for _ in range(50):
        if not src.read(): break
        frames += 1
    src.cleanup()
    print("frames read:", frames, "(~%dms of audio)" % (frames * 20))
    assert frames >= 40, "stream did not produce a steady frame flow"
    print()
    print("ALL AUDIO PATH TESTS PASSED")

asyncio.run(main())
