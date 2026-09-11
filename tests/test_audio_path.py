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
    # Local files decode end to end, across the formats people actually keep in MUSIC_DIR.
# Both failure modes here were silent: FFmpeg refused the HTTP reconnect flags for a local
# path, and a lossless file's probed bitrate was above what libopus accepts. In each case
# the source constructed fine and then produced nothing.
import subprocess as _sp, tempfile as _tf

_ff = music.find_ffmpeg()
if _ff:
    _dir = _tf.mkdtemp(prefix="amy_local_")
    for _ext, _codec, _rate in [("mp3", "libmp3lame", "192k"), ("m4a", "aac", "192k"),
                                ("opus", "libopus", "128k"), ("wav", "pcm_s16le", None),
                                ("flac", "flac", None)]:
        _path = os.path.join(_dir, "probe." + _ext)
        _cmd = [_ff, "-y", "-f", "lavfi", "-i", "sine=frequency=1000:duration=6",
                "-ac", "2", "-ar", "48000", "-c:a", _codec]
        if _rate:
            _cmd += ["-b:a", _rate]
        _sp.run(_cmd + [_path], capture_output=True, check=True)

        for _start, _label in [(0.0, "plain"), (3.0, "seeked")]:
            _src = asyncio.run(music.build_source(_path, 1.0, _ff, _start))
            _frames = sum(1 for _ in range(10) if _src.read())
            _src.cleanup()
            assert _frames > 0,                 "local .%s (%s) produced no audio - %d/10 frames" % (_ext, _label, _frames)
            print("  local .%-4s %-7s -> %d/10 frames" % (_ext, _label, _frames))
        os.remove(_path)
    os.rmdir(_dir)
else:
    print("  SKIP local file decoding (no ffmpeg)")

print("ALL AUDIO PATH TESTS PASSED")

asyncio.run(main())
