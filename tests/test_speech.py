"""
speech.py on its own: text clean-up, the English check, sentence splitting, voice recipes,
audio framing, and the Speaker's lifecycle - all with a fake engine, so neither Kokoro nor
PyTorch is needed. The real engine is exercised by test_speech_live.py.
"""
import asyncio
import io
import os
import sys
import tempfile

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJ)

import speech
from _check import check, finish

print("=== importing speech pulls in nothing heavy ===")
heavy = [m for m in ("torch", "kokoro", "numpy", "spacy", "discord") if m in sys.modules]
check("no torch/kokoro/numpy/spacy/discord after `import speech`", not heavy, heavy)

print()
print("=== clean_for_speech ===")
C = speech.clean_for_speech
for label, raw, want in [
    ("plain text is untouched", "Sure, that works.", "Sure, that works."),
    ("bold and italics go", "This is **really** *quite* good.", "This is really quite good."),
    ("a markdown link keeps its words", "See [the docs](https://example.com/x) first.",
     "See the docs first."),
    ("a bare link becomes 'a link'", "Read https://example.com/a?b=1 now.", "Read a link now."),
    ("a code block becomes one line", "Try this:\n```py\nprint(1)\n```",
     "Try this: " + speech.CODE_PLACEHOLDER),
    ("inline code keeps its text", "Run `pip install` first.", "Run pip install first."),
    ("user/role/channel mentions vanish", "Hey <@123456789012345678>, see <#987654321098765432>.",
     "Hey, see."),
    ("@everyone is never spoken", "Hello @everyone and @here!", "Hello and!"),
    ("custom Discord emoji vanish", "Nice <:pog:123456789012345678> one", "Nice one."),
    ("emoji vanish", "Done ✅ and dusted \U0001F389", "Done and dusted."),
    ("headings lose their #", "## Results\nAll good", "Results. All good."),
    ("each bullet becomes a sentence", "Steps:\n- open it\n- close it",
     "Steps: open it. close it."),
    ("long numbers (IDs) aren't read out", "Your id is 123456789012345678 ok", "Your id is ok."),
    ("identifiers keep their underscores", "Use af_heart for now", "Use af_heart for now."),
    ("only a code block -> just the line", "```\nx = 1\n```", speech.CODE_PLACEHOLDER),
    ("an ellipsis survives", "Well... maybe.", "Well... maybe."),
]:
    got = C(raw)
    check("%s" % label, got == want, "%r -> %r" % (raw, got))

long = " ".join("This is sentence number %d of a very long reply." % i for i in range(30))
capped = C(long)
check("long replies are capped", len(capped) <= speech.MAX_SPOKEN_CHARS, len(capped))
check("...at a sentence boundary", capped.endswith("."), capped[-30:])
wall = "word " * 200
capped = C(wall)
check("text with no sentence ends is cut at a word and marked",
      len(capped) <= speech.MAX_SPOKEN_CHARS + 1 and capped.endswith("…"), capped[-20:])
check("empty in, empty out", C("") == "" and C("   \n  ") == "")

print()
print("=== is_english ===")
E = speech.is_english
for text, want in [
    ("What's the weather like today?", True),
    ("The capital of Australia is Canberra.", True),
    ("I had phở at a café yesterday, it was great.", True),    # two borrowed words
    ("Hôm nay trời đẹp quá, bạn có khỏe không?", False),
    ("Mình đã thêm bài hát vào hàng chờ.", False),
    ("今日はいい天気ですね", False),       # Japanese
    ("Привет, как дела?", False),  # Russian
    ("42", True),
    ("", True),
]:
    check("%r -> %s" % (text[:40], want), E(text) is want, E(text))

print()
print("=== split_sentences ===")
S = speech.split_sentences
for label, text, want in [
    ("splits on . ! ?", "First one here. Second one here! Third one here?",
     ["First one here.", "Second one here!", "Third one here?"]),
    ("decimals stay whole", "Pi is about 3.14 these days. Neat stuff indeed.",
     ["Pi is about 3.14 these days.", "Neat stuff indeed."]),
    ("abbreviations don't split", "Ask Dr. Smith about it. She knows the answer.",
     ["Ask Dr. Smith about it.", "She knows the answer."]),
    ("tiny pieces join the next", "Sure. The song is queued now.", ["Sure. The song is queued now."]),
    ("a tiny last piece joins the one before", "The song is queued now. Ok.",
     ["The song is queued now. Ok."]),
    ("one sentence stays one", "Just this one sentence here", ["Just this one sentence here"]),
    ("nothing in, nothing out", "   ", []),
]:
    got = S(text)
    check(label, got == want, got)

print()
print("=== voice recipes (AMY_VOICE) ===")
P = speech.parse_voice_recipe
check("unset -> the default voice", P(None) == ([("af_heart", 1.0)], None))
check("one voice", P("af_bella") == ([("af_bella", 1.0)], None), P("af_bella"))
recipe, problem = P("af_heart:3, af_bella:1")
check("weights are normalised to add up to 1",
      problem is None and recipe == [("af_heart", 0.75), ("af_bella", 0.25)], recipe)
check("names are case-insensitive", P("AF_Sky")[0] == [("af_sky", 1.0)])
for bad, why in [("jf_alpha", "not English"), ("af_heart:abc", "bad weight"),
                 ("af_heart:0", "zero weight"), ("af_heart,af_heart", "listed twice"),
                 ("heart", "not a voice name"),
                 (",".join("af_v%s" % c for c in "abcdef"), "too many voices")]:
    got, problem = P(bad)
    check("%s (%s) falls back to the default and says why" % (bad[:24], why),
          got == [("af_heart", 1.0)] and problem is not None and "AMY_VOICE" in problem, problem)
check("format: one voice", speech.format_recipe([("af_heart", 1.0)]) == "af_heart")
check("format: a blend", speech.format_recipe([("af_heart", 0.6), ("af_bella", 0.4)])
      == "af_heart 60% + af_bella 40%")

print()
print("=== audio framing ===")
import audioop
one_second = audioop.mul(b"\x10\x00" * speech.ENGINE_RATE, 2, 1)     # 1s of 24 kHz mono
frames = speech.to_discord_frames(one_second)
check("1 s of engine audio -> 50 frames of 20 ms", len(frames) == 50, len(frames))
check("every frame is exactly FRAME_BYTES", all(len(f) == speech.FRAME_BYTES for f in frames))
odd = speech.to_discord_frames(b"\x10\x00" * 600)                # 25 ms: one frame and a bit
check("a partial last frame is padded, not dropped",
      [len(f) for f in odd] == [speech.FRAME_BYTES] * 2, [len(f) for f in odd])
check("no audio -> no frames", speech.to_discord_frames(b"") == [])
loud = b"\xff\x7f" * 100                                            # full-scale samples
check("loud speech is brought down to the peak target (headroom for mixing)",
      abs(audioop.max(speech.normalise_peak(loud), 2) - int(speech.PEAK_TARGET * 32767)) <= 2)
quiet = b"\x01\x00" * 100
check("near-silence isn't blown up past 4x",
      audioop.max(speech.normalise_peak(quiet), 2) <= 4)
check("silence stays silence", speech.normalise_peak(b"\x00\x00" * 10) == b"\x00\x00" * 10)

print()
print("=== KokoroEngine refuses clearly before importing anything ===")
with tempfile.TemporaryDirectory() as empty:
    missing = speech.missing_model_files(empty, ["af_heart", "af_bella"])
    check("missing files are listed", missing == ["config.json", "kokoro-v1_0.pth",
                                                  "voices/af_heart.pt", "voices/af_bella.pt"],
          missing)
    try:
        speech.KokoroEngine([("af_heart", 1.0)], model_dir=empty).load()
        refused = ""
    except speech.SpeechUnavailable as e:
        refused = str(e)
    except Exception as e:                  # got past the guard and failed later
        refused = "guard bypassed: %s" % type(e).__name__
    check("no downloaded model -> says to run the download", "python speech.py download" in refused,
          refused)
    check("...without importing torch or kokoro",
          "torch" not in sys.modules and "kokoro" not in sys.modules)

# R5: with the files present and Kokoro installed but spaCy's English model missing, Kokoro
# would `pip install` it at runtime. load() must refuse first, and import nothing heavy.
import importlib.util as _iu
with tempfile.TemporaryDirectory() as fake:
    os.makedirs(os.path.join(fake, "voices"))
    for f in ("config.json", "kokoro-v1_0.pth", "voices/af_heart.pt"):
        open(os.path.join(fake, f), "w").close()
    _real_find = _iu.find_spec

    def _find(name, *a, **kw):
        if name == speech.SPACY_MODEL:
            return None                         # the spaCy model "isn't installed"
        if name == "kokoro":
            return object()                     # Kokoro "is" - so only spaCy is missing
        return _real_find(name, *a, **kw)

    _iu.find_spec = _find
    try:
        speech.KokoroEngine([("af_heart", 1.0)], model_dir=fake).load()
        refused = ""
    except speech.SpeechUnavailable as e:
        refused = str(e)
    except Exception as e:                  # got past the guard and failed later
        refused = "guard bypassed: %s" % type(e).__name__
    finally:
        _iu.find_spec = _real_find
    check("R5: a missing spaCy model is refused, never pip-installed",
          speech.SPACY_MODEL in refused and "requirements-tts.txt" in refused, refused)
    check("R5: ...before torch or kokoro is imported",
          "torch" not in sys.modules and "kokoro" not in sys.modules)

print()
print("=== Speaker ===")


async def speaker_flow():
    eng = speech.FakeEngine()
    sp = speech.Speaker(eng)
    out = {"before": await sp.frames_for("Too early.")}
    out["started"] = await sp.start()
    out["again"] = await sp.start()          # loads once
    out["loads"] = eng.loads
    out["frames"] = await sp.frames_for("Hello there, friend.")
    out["blank"] = await sp.frames_for("   ")
    # several at once: the single worker must take them one at a time
    await asyncio.gather(*(sp.frames_for("Sentence %d here." % i) for i in range(5)))
    out["threads"] = set(eng.threads_used)
    out["spoken"] = list(eng.spoken)
    sp.close()
    return sp, out

sp, out = asyncio.run(speaker_flow())
check("nothing is spoken before the engine is ready", out["before"] == [])
check("start() loads and reports ready", out["started"] is True and sp.state == "ready")
check("start() again doesn't reload", out["again"] is True and out["loads"] == 1, out["loads"])
check("a sentence becomes 20 ms frames",
      out["frames"] and all(len(f) == speech.FRAME_BYTES for f in out["frames"]))
check("blank text is skipped", out["blank"] == [])
check("all synthesis runs on the one speech thread",
      len(out["threads"]) == 1 and next(iter(out["threads"])).startswith("speech"), out["threads"])
check("the main thread never synthesises",
      not any("MainThread" in t for t in out["threads"]))


async def failing():
    sp = speech.Speaker(speech.FakeEngine(fail_load="Kokoro isn't installed. Run: pip install"))
    ok = await sp.start()
    frames = await sp.frames_for("Anyone there?")
    sp.close()
    return sp, ok, frames

sp, ok, frames = asyncio.run(failing())
check("a failed load never raises - Amy stays text-only", ok is False and sp.state == "failed")
check("...and keeps the reason for /voice and the log", "pip install" in (sp.problem or ""),
      sp.problem)
check("...and speaks nothing", frames == [])

finish("ALL SPEECH TESTS PASSED")
