"""
speech.py on its own: text clean-up, the English check, sentence splitting, voice recipes,
audio framing, and the Speaker's lifecycle - all with a fake engine, so neither Kokoro nor
PyTorch is needed. The real engine is exercised by test_speech_live.py.
"""
import asyncio
import io
import os
import re
import sys
import struct
import tempfile

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJ)

import speech
from _check import check, finish

print("=== importing speech pulls in nothing heavy ===")
heavy = [m for m in ("torch", "kokoro", "numpy", "spacy", "discord", "piper", "onnxruntime")
         if m in sys.modules]
check("no torch/kokoro/numpy/spacy/discord/piper/onnxruntime after `import speech`", not heavy, heavy)

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
    ("'feat.' doesn't split a credit", "Now playing Firestone, by Kygo, feat. Conrad Sewell. Enjoy it while it lasts.",
     ["Now playing Firestone, by Kygo, feat. Conrad Sewell.", "Enjoy it while it lasts."]),
    ("'ft.' doesn't split a credit", "Now playing Get Lucky ft. Pharrell Williams. Enjoy it while it lasts.",
     ["Now playing Get Lucky ft. Pharrell Williams.", "Enjoy it while it lasts."]),
    ("a bracketed '(feat.' doesn't split a credit",
     "I recommend Levitating (feat. DaBaby) by Dua Lipa. Enjoy it while it lasts.",
     ["I recommend Levitating (feat. DaBaby) by Dua Lipa.", "Enjoy it while it lasts."]),
    ("a bracketed '[ft.' doesn't split a credit",
     "Next up is Laugh Now Cry Later [ft. Lil Durk] by Drake. Enjoy it while it lasts.",
     ["Next up is Laugh Now Cry Later [ft. Lil Durk] by Drake.", "Enjoy it while it lasts."]),
    ("nothing in, nothing out", "   ", []),
]:
    got = S(text)
    check(label, got == want, got)

print()
print("=== voice recipes (AMY_VOICE) ===")
P = speech.parse_voice_recipe
check("unset -> Amy's chosen voice", P(None) == (speech.DEFAULT_RECIPE, None), P(None))
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
    check("%s (%s) falls back to Amy's voice and says why" % (bad[:24], why),
          got == speech.DEFAULT_RECIPE and problem is not None and "AMY_VOICE" in problem, problem)
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

# ======================================================================================
# The Piper engine (optional): pure helpers, and the engine with fakes. The real thing is
# exercised by test_piper_live.py.
# ======================================================================================
import logging
import subprocess
import urllib.request

print()
print("=== TTS_ENGINE ===")
PE = speech.parse_engine
check("unset / blank -> kokoro, no problem", PE(None) == ("kokoro", None) and PE("  ") == ("kokoro", None))
check("piper (any case, padded)", PE(" Piper ") == ("piper", None) and PE("PIPER")[0] == "piper")
check("kokoro", PE("kokoro") == ("kokoro", None))
eng, problem = PE("pipr")
check("a typo falls back to kokoro and names the setting",
      eng == "kokoro" and problem is not None and "TTS_ENGINE" in problem and "pipr" in problem, problem)

print()
print("=== PIPER_VOICE path, and the description ===")
check("blank -> the downloaded cori voice", speech.resolve_piper_path(None) == speech.PIPER_MODEL
      and speech.resolve_piper_path("  ") == speech.PIPER_MODEL)
check("the default is inside the project's piper_model/",
      speech.PIPER_MODEL == os.path.join(PROJ, "piper_model", "en_GB-cori-medium.onnx"), speech.PIPER_MODEL)
check("a relative path is from the project",
      speech.resolve_piper_path("voices/x.onnx") == os.path.normpath(os.path.join(PROJ, "voices", "x.onnx")))
absolute = os.path.join(tempfile.gettempdir(), "x.onnx")
check("an absolute path is kept", speech.resolve_piper_path(absolute) == os.path.normpath(absolute))
check("format: the shipped defaults",
      speech.format_piper(speech.PIPER_MODEL, 1.05, 0.0, True)
      == "Piper en_GB-cori-medium (length 1.05, pitch unchanged, soft EQ on)")
check("format: a pitch shift is shown",
      speech.format_piper(speech.PIPER_MODEL, 1.05, 1.0, True)
      == "Piper en_GB-cori-medium (length 1.05, pitch +1.0 st, soft EQ on)")
check("format: no pitch, no soft EQ",
      speech.format_piper("x/voice.onnx", 1.0, 0.0, False)
      == "Piper voice (length 1.00, pitch unchanged, soft EQ off)")
check("the code defaults reproduce the chosen voice",
      speech.DEFAULT_PIPER_LENGTH == 1.05 and speech.DEFAULT_PIPER_PITCH == 0.0
      and speech.PIPER_NOISE_SCALE == 0.333 and speech.PIPER_NOISE_W == 0.333)
check("...and with the default pitch the chain has no rubberband step, only the soft EQ",
      "rubberband" not in (speech.piper_filter_chain(speech.DEFAULT_PIPER_PITCH, True, True) or ""))

print()
print("=== piper_text: what espeak mispronounces ===")
T = speech.piper_text
for label, raw, want in [
    ("feat. -> featuring", "Now playing Firestone, by Kygo, feat. Conrad Sewell.",
     "Now playing Firestone, by Kygo, featuring Conrad Sewell."),
    ("ft. -> featuring", "Get Lucky ft. Pharrell Williams.", "Get Lucky featuring Pharrell Williams."),
    ("Feat without a dot, in brackets", "Firestone (Feat Conrad Sewell)", "Firestone (featuring Conrad Sewell)"),
    ("the dot of 'feat.' can't end the sentence", "By Kygo feat. Conrad. Next one.",
     "By Kygo featuring Conrad. Next one."),
    ("the noun 'feat' is left alone", "What a feat. Next up is Take Me to Church.",
     "What a feat. Next up is Take Me to Church."),
    ("'feat' inside another word is left alone", "Defeat. Featherlight.", "Defeat. Featherlight."),
    ("'ft' as feet is left alone", "It is 6 ft. tall and 6 ft Tall.", "It is 6 ft. tall and 6 ft Tall."),
    ("'ft' inside Daft/Left is left alone", "Daft Punk. Left Alone.", "Daft Punk. Left Alone."),
    ("#3 -> number 3", "It's #3 in the queue.", "It's number 3 in the queue."),
    ("#12 mid-sentence", "Issue #12, then more.", "Issue number 12, then more."),
    ("C#3 and a bare # are left alone", "A note C#3 and # alone", "A note C#3 and # alone"),
    ("2 min -> 2 minutes", "Starting in 2 min.", "Starting in 2 minutes."),
    ("1 min -> 1 minute", "That's 1 min of music.", "That's 1 minute of music."),
    ("3 mins 20 secs", "It's 3 mins 20 secs long.", "It's 3 minutes 20 seconds long."),
    ("1 sec -> 1 second", "Wait 1 sec.", "Wait 1 second."),
    ("minutes already spelt out are left alone", "5 minutes and 10 seconds", "5 minutes and 10 seconds"),
    ("'min' inside a word is left alone", "5 mining towns, 2 secrets", "5 mining towns, 2 secrets"),
    ("1:05 -> 1 oh 5", "Skipping to 1:05.", "Skipping to 1 oh 5."),
    ("10:09 -> 10 oh 9", "At 10:09 exactly.", "At 10 oh 9 exactly."),
    ("2:30 is read fine as it is", "Starting at 2:30.", "Starting at 2:30."),
    ("h:mm:ss times are left alone", "It runs 1:05:30 in total.", "It runs 1:05:30 in total."),
    ("Sade", "Starting Smooth Operator, by Sade.", "Starting Smooth Operator, by Shah-day."),
    ("...possessive, any case", "SADE's best. sade rocks.", "Shah-day's best. Shah-day rocks."),
    ("Sade inside other words is left alone", "Persuade the Sadeness.", "Persuade the Sadeness."),
    ("Maneskin and its accented form", "Zitti e buoni, by Måneskin or Maneskin.",
     "Zitti e buoni, by Mawneskin or Mawneskin."),
    ("Einaudi", "By Ludovico Einaudi.", "By Ludovico Eye-now-dee."),
    ("Beyonce and its accented form", "Beyoncé, then Beyonce.", "Bee-yon-say, then Bee-yon-say."),
    ("Dua Lipa, even with extra spaces", "Levitating by Dua  Lipa.", "Levitating by Dooah Leepah."),
    ("normal text is untouched", "Sure, that works. Volume is at 40 percent! Next: Adele?",
     "Sure, that works. Volume is at 40 percent! Next: Adele?"),
]:
    got = T(raw)
    check(label, got == want, "%r -> %r" % (raw, got))
check("several fixes in one line",
      T("Firestone feat. Conrad Sewell is #3, starting at 1:05 in 2 min, by Sade.")
      == "Firestone featuring Conrad Sewell is number 3, starting at 1 oh 5 in 2 minutes, by Shah-day.")
raw_block = T("By [[ʃɑːdˈeɪ]]. Next up is Dua!")
check("raw-phoneme blocks can't get through (Piper would drop the full stop after one)",
      "[[" not in raw_block and "]]" not in raw_block and ". Next up is Dua!" in raw_block, raw_block)
nested = T("By []][hhhhh][[]. Next up is Dua!")
check("brackets that would join into a raw block after a one-pass strip are all removed",
      "[" not in nested and "]" not in nested and nested.endswith(". Next up is Dua!"), nested)
check("every dictionary respelling is plain letters, spaces and hyphens - no raw blocks",
      all(re.fullmatch(r"[A-Za-z -]+", v) for v in speech.PIPER_NAMES.values()), speech.PIPER_NAMES)
check("empty in, empty out", T("") == "" and T("   ") == "")

print()
print("=== piper_filter_chain ===")
F = speech.piper_filter_chain
SOFT = "equalizer=f=4500:t=q:w=1:g=-3,highshelf=f=6500:g=-3,deesser=i=0.4:m=0.5:f=0.5:s=o"
PITCH = "rubberband=pitch=1.059463:formant=preserved"
check("a +1 semitone shift with formants kept, then the soft EQ",
      F(1.0, True, True) == PITCH + "," + SOFT, F(1.0, True, True))
check("pitch only", F(1.0, False, True) == PITCH)
check("soft only (pitch 0)", F(0.0, True, True) == SOFT)
check("no rubberband -> the pitch part is dropped, the soft EQ kept", F(1.0, True, False) == SOFT)
check("nothing to do -> None", F(0.0, False, True) is None and F(1.0, False, False) is None
      and F(0.0, False, False) is None)
check("a negative shift lowers the pitch (2 ** (-2/12))",
      F(-2.0, False, True) == "rubberband=pitch=0.890899:formant=preserved", F(-2.0, False, True))
LISTING = """Filters:
  T.. = Timeline support
  .S. = Slice threading
  A = Audio input/output
  ------
 TS aap               AA->A      Apply Affine Projection algorithm to first audio stream.
 T. deesser           A->A       Apply de-essing to the audio.
 .. rubberband        A->A       Apply time-stretching and pitch-shifting.
 ... testsrc          |->V       Generate test pattern.
"""
check("the FFmpeg filter list is read, both layouts",
      speech.parse_ffmpeg_filters(LISTING) == {"aap", "deesser", "rubberband", "testsrc"},
      speech.parse_ffmpeg_filters(LISTING))

print()
print("=== PiperEngine refuses clearly before importing anything ===")
with tempfile.TemporaryDirectory() as empty:
    model = os.path.join(empty, "v.onnx")
    check("missing files are listed", speech.missing_piper_files(model) == [model, model + ".json"])
    _real_pm = speech.PIPER_MODEL
    speech.PIPER_MODEL = model            # as if this were the downloaded default voice
    try:
        try:
            speech.PiperEngine(model).load()
            refused = ""
        except speech.SpeechUnavailable as e:
            refused = str(e)
        except Exception as e:
            refused = "guard bypassed: %s" % type(e).__name__
        check("no downloaded voice -> says to run download-piper", "python speech.py download-piper" in refused,
              refused)
    finally:
        speech.PIPER_MODEL = _real_pm
    try:
        speech.PiperEngine(model).load()
        refused_custom = ""
    except speech.SpeechUnavailable as e:
        refused_custom = str(e)
    check("a missing CUSTOM voice names its path and PIPER_VOICE, not download-piper",
          model in refused_custom and "PIPER_VOICE" in refused_custom and "download-piper" not in refused_custom,
          refused_custom)
    open(model, "w").close()
    open(model + ".json", "w").close()
    check("both files present -> nothing missing", speech.missing_piper_files(model) == [])
    _real_find = _iu.find_spec
    _iu.find_spec = lambda name, *a, **kw: None if name in ("piper", "onnxruntime") else _real_find(name, *a, **kw)
    try:
        speech.PiperEngine(model).load()
        refused = ""
    except speech.SpeechUnavailable as e:
        refused = str(e)
    except Exception as e:
        refused = "guard bypassed: %s" % type(e).__name__
    finally:
        _iu.find_spec = _real_find
    check("piper not installed -> says to install requirements-piper.txt",
          "requirements-piper.txt" in refused, refused)
    check("...and nothing was imported", "piper" not in sys.modules and "onnxruntime" not in sys.modules)
    try:
        speech.PiperEngine(model).synthesize("hi")
        unloaded = ""
    except speech.SpeechUnavailable as e:
        unloaded = str(e)
    check("synthesising before load() is refused", "isn't loaded" in unloaded, unloaded)


class _Records(logging.Handler):
    def __init__(self):
        super().__init__(logging.WARNING)
        self.messages = []

    def emit(self, record):
        self.messages.append(record.getMessage())


def with_fake_run(fake, fn):
    """Run fn() with subprocess.run replaced; return (result, warnings logged)."""
    handler = _Records()
    speech_log = logging.getLogger("amy.speech")
    speech_log.addHandler(handler)
    real = speech.subprocess.run
    speech.subprocess.run = fake
    try:
        return fn(), handler.messages
    finally:
        speech.subprocess.run = real
        speech_log.removeHandler(handler)


def listing_run(names):
    def run(cmd, **kw):
        body = "".join(" .. %s A->A  x\n" % n for n in names)
        return subprocess.CompletedProcess(cmd, 0, stdout="Filters:\n" + body, stderr="")
    return run


print()
print("=== PiperEngine decides its filter chain from what FFmpeg really has ===")
ALL = ["rubberband", "equalizer", "highshelf", "deesser"]
e = speech.PiperEngine("v.onnx", pitch=1.0, soft=True, ffmpeg="ffmpeg-x")
chain, warns = with_fake_run(listing_run(ALL), e._plan_chain)
check("full FFmpeg -> the whole chain, no warnings", chain == PITCH + "," + SOFT and not warns, (chain, warns))
chain, warns = with_fake_run(listing_run(["equalizer", "highshelf", "deesser"]), e._plan_chain)
check("no rubberband -> soft EQ only, one warning", chain == SOFT and len(warns) == 1 and "rubberband" in warns[0],
      (chain, warns))
chain, warns = with_fake_run(listing_run(["rubberband"]), e._plan_chain)
check("no EQ filters -> pitch only, one warning", chain == PITCH and len(warns) == 1, (chain, warns))
chain, warns = with_fake_run(listing_run([]), e._plan_chain)
check("neither -> no chain, two warnings", chain is None and len(warns) == 2, (chain, warns))
chain, warns = with_fake_run(listing_run(ALL), speech.PiperEngine("v.onnx", ffmpeg=None)._plan_chain)
check("no FFmpeg -> speak plain, with a warning", chain is None and len(warns) == 1 and "FFmpeg" in warns[0],
      (chain, warns))
chain, warns = with_fake_run(listing_run(ALL), speech.PiperEngine("v.onnx", pitch=0.0, soft=False,
                                                                ffmpeg=None)._plan_chain)
check("nothing asked for -> no chain and no warning even without FFmpeg", chain is None and not warns,
      (chain, warns))


seen_kw = {}


def listing_kw(cmd, **kw):
    seen_kw.update(kw)
    return subprocess.CompletedProcess(cmd, 0, stdout="Filters:" + chr(10), stderr="")


with_fake_run(listing_kw, e._plan_chain)
check("the FFmpeg filter list is decoded leniently (a stray byte can't fail the load)",
      seen_kw.get("errors") == "replace", seen_kw)


def boom(cmd, **kw):
    raise OSError("no such program")


chain, warns = with_fake_run(boom, e._plan_chain)
check("FFmpeg that won't run -> speak plain, with a warning", chain is None and len(warns) == 1, (chain, warns))

print()
print("=== PiperEngine.synthesize: text fixes, chain, and graceful fallback ===")


class _Chunk:
    def __init__(self, data):
        self.audio_int16_bytes = data


class _FakeVoice:
    def __init__(self):
        self.texts = []

    def synthesize(self, text, cfg):
        self.texts.append(text)
        yield _Chunk(b"\x01\x00" * 100)
        yield _Chunk(b"\x02\x00" * 100)


def loaded_engine(chain):
    eng = speech.PiperEngine("v.onnx", ffmpeg="ffmpeg-x")
    eng._voice = _FakeVoice()
    eng.chain = chain
    return eng


eng = loaded_engine(None)
out = eng.synthesize("Kygo feat. Conrad, #3 at 1:05.")
check("the text fixes run before Piper sees the text",
      eng._voice.texts == ["Kygo featuring Conrad, number 3 at 1 oh 5."], eng._voice.texts)
check("chunks are joined and, with no chain, returned as they are",
      out == b"\x01\x00" * 100 + b"\x02\x00" * 100)
check("text with nothing speakable gives no audio and doesn't call Piper",
      loaded_engine(None).synthesize("  ") == b"")

calls = []


def fake_ffmpeg(cmd, **kw):
    calls.append((cmd, kw))
    return subprocess.CompletedProcess(cmd, 0, stdout=b"\x07\x00" * 50, stderr=b"")


eng = loaded_engine("loudnorm-ish")
out, warns = with_fake_run(fake_ffmpeg, lambda: eng.synthesize("Hello there."))
cmd, kw = calls[0]
check("with a chain, the audio goes through FFmpeg and its output is returned",
      out == b"\x07\x00" * 50 and not warns, (out[:4], warns))
check("...as raw 16-bit mono over pipes, with the chain, rate and no console window",
      cmd[0] == "ffmpeg-x" and "-af" in cmd and cmd[cmd.index("-af") + 1] == "loudnorm-ish"
      and cmd[cmd.index("-ar") + 1] == str(eng.rate) and "pipe:0" in cmd and cmd[-1] == "pipe:1"
      and kw.get("input") == b"\x01\x00" * 100 + b"\x02\x00" * 100
      and kw.get("timeout") == speech.PIPER_FFMPEG_TIMEOUT
      and kw.get("creationflags") == speech._NO_WINDOW and not kw.get("shell"), (cmd, kw.keys()))


def failing_ffmpeg(cmd, **kw):
    return subprocess.CompletedProcess(cmd, 1, stdout=b"", stderr=b"Error: filter exploded")


eng = loaded_engine("x")
plain = loaded_engine(None).synthesize("Hello there.")
out1, warns = with_fake_run(failing_ffmpeg, lambda: eng.synthesize("Hello there."))
out2, warns2 = with_fake_run(failing_ffmpeg, lambda: eng.synthesize("Hello again."))
check("a failing filter run returns the unprocessed audio - speech is never dropped",
      out1 == plain and out2 == plain, (out1[:4], out2[:4]))
check("...with one warning that says why, not one per sentence",
      len(warns) == 1 and "filter exploded" in warns[0] and warns2 == [], (warns, warns2))
eng = loaded_engine("x")


def hung(cmd, **kw):
    raise subprocess.TimeoutExpired(cmd, 1)


hung_calls = []


def hung_counting(cmd, **kw):
    hung_calls.append(cmd)
    raise subprocess.TimeoutExpired(cmd, 1)


out, warns = with_fake_run(hung_counting, lambda: eng.synthesize("Hello there."))
check("an FFmpeg that hangs (timeout) also falls back to the unprocessed audio",
      out == plain and len(warns) == 1, (out[:4], warns))
check("...and says the polish is now off until restart", "until restart" in warns[0], warns)
out2, warns2 = with_fake_run(hung_counting, lambda: eng.synthesize("Hello again."))
check("...so the next sentence doesn't wait for FFmpeg again", len(hung_calls) == 1 and out2 == plain
      and eng.chain is None, (len(hung_calls), eng.chain))
eng = loaded_engine("x")
with_fake_run(failing_ffmpeg, lambda: eng.synthesize("Hello there."))   # uses up the one warning
_, warns = with_fake_run(hung, lambda: eng.synthesize("Hello again."))
check("a timeout after an earlier, different failure still logs that the polish is off",
      len(warns) == 1 and "until restart" in warns[0], warns)

print()
print("=== download-piper checks what it fetched ===")
check("both voice files are pinned by SHA-256", set(speech.PIPER_SHA256) ==
      {"en_GB-cori-medium.onnx", "en_GB-cori-medium.onnx.json"}
      and all(len(h) == 64 for h in speech.PIPER_SHA256.values()))
check("the revision is a full commit hash", re.fullmatch(r"[0-9a-f]{40}", speech.PIPER_REVISION) is not None)


class _FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


_real_open = urllib.request.urlopen
urls = []
urllib.request.urlopen = lambda url, timeout=0: (urls.append(url), _FakeResponse(b"not the real voice"))[1]
try:
    with tempfile.TemporaryDirectory() as d:
        try:
            speech.download_piper(d)
            refused = ""
        except RuntimeError as err:
            refused = str(err)
        check("a download that doesn't match its checksum is refused", "checksum" in refused, refused)
        check("...and leaves nothing behind (no half file to be loaded later)", os.listdir(d) == [],
              os.listdir(d))
        check("...fetched from the pinned revision",
              urls and speech.PIPER_REVISION in urls[0] and urls[0].endswith("/en_GB-cori-medium.onnx"), urls)
finally:
    urllib.request.urlopen = _real_open


class _DyingResponse(_FakeResponse):
    def read(self, n=-1):
        raise OSError("connection reset")


urllib.request.urlopen = lambda url, timeout=0: _DyingResponse(b"x")
try:
    with tempfile.TemporaryDirectory() as d:
        try:
            speech.download_piper(d)
            died = ""
        except OSError as err:
            died = str(err)
        check("a download that dies mid-stream raises", "connection reset" in died, died)
        check("...and leaves no .part file behind", os.listdir(d) == [], os.listdir(d))
finally:
    urllib.request.urlopen = _real_open

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

print()
print("=== voice level (VOICE_LEVEL) ===")


class LoudEngine(speech.FakeEngine):
    def synthesize(self, text):
        return struct.pack("<2h", 32767, -32767) * 2400     # full-scale samples, 0.2 s


def loudest(level):
    async def go():
        sp = speech.Speaker(LoudEngine(), level=level)
        await sp.start()
        frames = await sp.frames_for("Loud.")
        sp.close()
        return max(audioop.max(f, 2) for f in frames)
    return asyncio.run(go())

full, quieter = loudest(1.0), loudest(0.75)
check("level 1.0 puts her peak at PEAK_TARGET",
      abs(full - speech.PEAK_TARGET * 32767) <= 64, full)
check("level 0.75 makes her 25% quieter", abs(quieter / full - 0.75) < 0.01, quieter / full)
check("the shipped default is 25% below full", speech.DEFAULT_VOICE_LEVEL == 0.75)

print()
print("=== Amy's chosen voice (Phase 1E) ===")
check("the default is the blend chosen in the voice lab",
      speech.DEFAULT_RECIPE == [("af_heart", 0.4), ("af_bella", 0.6)] and speech.DEFAULT_SPEED == 1.05)
check("its weights add up to 1", abs(sum(w for _, w in speech.DEFAULT_RECIPE) - 1.0) < 1e-9)
check("it parses back to itself from the .env form",
      speech.parse_voice_recipe("af_heart:0.4,af_bella:0.6") == (speech.DEFAULT_RECIPE, None))

finish("ALL SPEECH TESTS PASSED")
