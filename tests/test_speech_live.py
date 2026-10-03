"""
The real speech engine: Kokoro from the pinned local files, on the CPU.

Needs `pip install -r requirements-tts.txt` and `python speech.py download`. Without either,
exits 77 so the runner reports SKIP - never a pass that tested nothing.
"""
import asyncio
import importlib.util
import io
import os
import sys
import time

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJ)

import speech
from _check import check, finish

if importlib.util.find_spec("kokoro") is None:
    print("SKIP: Kokoro isn't installed (pip install -r requirements-tts.txt)")
    sys.exit(77)
if speech.missing_model_files(speech.MODEL_DIR, [speech.DEFAULT_VOICE]):
    print("SKIP: the voice model isn't downloaded (python speech.py download)")
    sys.exit(77)

# A short sentence measured 1.11s at 6 threads on the dev machine; this bound catches a real
# regression (a thread setting ignored, the model loading per call) without flaking on load.
MAX_SENTENCE_SECONDS = 3.0

REPLY = ("**Sure!** The capital of Australia is Canberra, not Sydney. "
         "It was chosen as a compromise in 1908. More at https://en.wikipedia.org/wiki/Canberra")


async def main():
    sp = speech.Speaker(speech.KokoroEngine([(speech.DEFAULT_VOICE, 1.0)]))
    t0 = time.perf_counter()
    ok = await sp.start()
    print("  load took %.1fs" % (time.perf_counter() - t0))
    check("the engine loads from the local files", ok and sp.ready, sp.problem)
    check("...with Hugging Face lookups switched off", os.environ.get("HF_HUB_OFFLINE") == "1")
    if not sp.ready:
        return

    t0 = time.perf_counter()
    frames = await sp.frames_for("Skipping that one. Up next is Never Gonna Give You Up.")
    took = time.perf_counter() - t0
    seconds = len(frames) * 0.02
    print("  short sentence: %.2fs to make %.1fs of speech" % (took, seconds))
    check("a short sentence is ready in under %.0fs" % MAX_SENTENCE_SECONDS,
          took < MAX_SENTENCE_SECONDS, took)
    check("it produces real speech, not silence or a blip", 1.5 < seconds < 8, seconds)
    check("every frame is 20 ms of 48 kHz stereo",
          all(len(f) == speech.FRAME_BYTES for f in frames))
    loudest = max(speech.audioop.max(f, 2) for f in frames)
    target = int(speech.PEAK_TARGET * 32767)
    check("peaks are normalised to the target (headroom for mixing over music)",
          abs(loudest - target) <= target * 0.02, (loudest, target))

    # The whole text path a chat reply will take in 1C: clean, check, split, speak
    cleaned = speech.clean_for_speech(REPLY)
    check("the reply is speakable English", speech.is_english(cleaned), cleaned)
    parts = speech.split_sentences(cleaned)
    check("it splits into sentences", len(parts) == 3, parts)
    spoken = [await sp.frames_for(p) for p in parts]
    check("each sentence becomes speech", all(spoken), [len(f) for f in spoken])

    # A blend, if a second voice has been downloaded (python speech.py download af_bella)
    if not speech.missing_model_files(speech.MODEL_DIR, ["af_bella"]):
        blend = speech.Speaker(speech.KokoroEngine([("af_heart", 0.6), ("af_bella", 0.4)]))
        check("a weighted blend loads and speaks", await blend.start()
              and bool(await blend.frames_for("This is a blended voice.")), blend.problem)
        blend.close()
    else:
        print("  (blend check skipped: af_bella not downloaded)")

    # The voice lab switches voices without reloading the model
    if not speech.missing_model_files(speech.MODEL_DIR, ["af_bella"]):
        eng = sp.engine
        before = eng.synthesize("Testing one voice.")
        t0 = time.perf_counter()
        eng.set_recipe([("af_bella", 1.0)])
        switch = time.perf_counter() - t0
        after = eng.synthesize("Testing one voice.")
        check("set_recipe switches voice in well under a second", switch < 1.0, switch)
        check("...and the voice really changes", before != after and eng.recipe == [("af_bella", 1.0)])
        try:
            eng.set_recipe([("af_zzzz", 1.0)])
            refused = ""
        except speech.SpeechUnavailable as e:
            refused = str(e)
        check("a voice that isn't downloaded is refused with the command to fetch it",
              "python speech.py download" in refused, refused)

    # The lab's pitch measurement, against a tone of known pitch
    sys.path.insert(0, os.path.join(PROJ, "tools"))
    import math
    import struct
    import voicelab
    tone = b"".join(struct.pack("<h", int(12000 * math.sin(2 * math.pi * 220 * i / 24000)))
                    for i in range(24000))
    measured = voicelab.median_pitch_hz(tone)
    check("the voice lab measures a 220 Hz tone as about 220 Hz", abs(measured - 220) < 6, measured)
    check("silence measures as no pitch", voicelab.median_pitch_hz(b"\x00\x00" * 24000) == 0.0)
    sp.close()

asyncio.run(main())
finish("ALL LIVE SPEECH TESTS PASSED")
