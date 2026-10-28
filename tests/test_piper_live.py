"""
The real Piper engine: the pinned cori voice from piper_model/, on the CPU, with the FFmpeg
post-processing chain.

Needs `pip install -r requirements-piper.txt` and `python speech.py download-piper` (and FFmpeg
for the post-processing checks). Without the package or the voice, exits 77 so the runner
reports SKIP - never a pass that tested nothing.
"""
import asyncio
import importlib.util
import io
import logging
import os
import sys
import time

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJ)

import speech
from _check import check, finish

if importlib.util.find_spec("piper") is None or importlib.util.find_spec("onnxruntime") is None:
    print("SKIP: Piper isn't installed (pip install -r requirements-piper.txt)")
    sys.exit(77)
if speech.missing_piper_files(speech.PIPER_MODEL):
    print("SKIP: the Piper voice isn't downloaded (python speech.py download-piper)")
    sys.exit(77)

import music

# A short sentence takes ~0.12s of synthesis plus ~0.1s of FFmpeg on the dev machine; this
# bound catches a real regression (threads unbounded, the model reloading per call, a hung
# filter) without flaking on a busy machine.
MAX_SENTENCE_SECONDS = 3.0
THREADS = 4
SENTENCE = "Skipping that one. Up next is Never Gonna Give You Up."


class _Warnings(logging.Handler):
    def __init__(self):
        super().__init__(logging.WARNING)
        self.messages = []

    def emit(self, record):
        self.messages.append(record.getMessage())


async def main():
    ffmpeg = music.find_ffmpeg()
    warnings = _Warnings()
    logging.getLogger("amy.speech").addHandler(warnings)
    engine = speech.PiperEngine(ffmpeg=ffmpeg, threads=THREADS)
    sp = speech.Speaker(engine)
    t0 = time.perf_counter()
    ok = await sp.start()
    print("  load took %.1fs" % (time.perf_counter() - t0))
    check("the engine loads from the local files", ok and sp.ready, sp.problem)
    if not sp.ready:
        return
    check("the voice's own sample rate is used (22050 Hz)", engine.rate == 22050, engine.rate)
    check("TTS_THREADS bounds onnxruntime's threads",
          engine._voice.session.get_session_options().intra_op_num_threads == THREADS)
    # An FFmpeg build without rubberband (or the EQ filters) is supported: Amy warns and skips
    # that part. Only other warnings mean something is wrong.
    unexpected = [m for m in warnings.messages
                  if "no rubberband filter" not in m and "skipping the soft EQ" not in m]
    check("no unexpected warnings while loading", not unexpected, unexpected)

    if ffmpeg:
        check("with FFmpeg, a filter chain was planned", engine.chain is not None, engine.chain)
        print("  chain:", engine.chain)
    else:
        print("  (FFmpeg not found: post-processing checks skipped)")

    t0 = time.perf_counter()
    frames = await sp.frames_for(SENTENCE)
    took = time.perf_counter() - t0
    seconds = len(frames) * 0.02
    print("  short sentence: %.2fs to make %.1fs of speech" % (took, seconds))
    check("a short sentence is ready in under %.0fs" % MAX_SENTENCE_SECONDS, took < MAX_SENTENCE_SECONDS, took)
    check("it produces real speech, not silence or a blip", 2 < seconds < 9, seconds)
    check("every frame is 20 ms of 48 kHz stereo (22.05 kHz resampled)",
          all(len(f) == speech.FRAME_BYTES for f in frames))
    loudest = max(speech.audioop.max(f, 2) for f in frames)
    target = int(speech.PEAK_TARGET * 32767)
    check("peaks are normalised to the target (headroom for mixing over music)",
          abs(loudest - target) <= target * 0.02, (loudest, target))

    # Raw Piper output vs the same audio through the chain
    chain, engine.chain = engine.chain, None
    raw = engine.synthesize(SENTENCE)
    engine.chain = chain
    check("Piper itself returns audio", len(raw) > 22050 * 2 * 1.5, len(raw))
    if ffmpeg and chain:
        t0 = time.perf_counter()
        processed = engine._filter(raw)
        print("  FFmpeg chain: %.2fs for %.1fs of audio" % (time.perf_counter() - t0, len(raw) / 2 / engine.rate))
        check("post-processing ran: the chain changed the audio", processed != raw and len(processed) > 0)
        check("...without changing its length by more than 10%", abs(len(processed) / len(raw) - 1) < 0.10,
              (len(raw), len(processed)))
        diff = speech.audioop.rms(processed, 2) / max(1, speech.audioop.rms(raw, 2))
        check("...and kept it audible (RMS within 0.3x to 3x of the original)", 0.3 < diff < 3.0, diff)
        check("no warnings so far", not warnings.messages, warnings.messages)

        # a chain FFmpeg rejects must fall back to the raw audio, never drop the sentence
        engine.chain, saved = "nosuchfilter=x=1", engine.chain
        engine._ffmpeg_warned = False
        fallback = engine._filter(raw)
        engine.chain = saved
        check("a chain FFmpeg rejects falls back to the unprocessed audio", fallback == raw)
        check("...with one warning", len(warnings.messages) == 1 and "FFmpeg" in warnings.messages[0],
              warnings.messages)

    # The pronunciation fixes, through the real espeak-ng that Piper uses
    voice = engine._voice

    def phonemes(text):
        return " | ".join("".join(s) for s in voice.phonemize(speech.piper_text(text)))

    check("'feat.' is said as 'featuring' and doesn't end the sentence",
          "fˈiːtʃəɹɪŋ" in phonemes("Kygo, feat. Conrad Sewell.") and "|" not in phonemes("Kygo, feat. Conrad Sewell."),
          phonemes("Kygo, feat. Conrad Sewell."))
    check("1:05 is said 'one oh five'", "wˈɒn ˈəʊ fˈaɪv" in phonemes("At 1:05."), phonemes("At 1:05."))
    check("#3 is said 'number three'", "nˈʌmbə θɹˈiː" in phonemes("It's #3."), phonemes("It's #3."))
    check("Sade is said 'shah-day'", "ʃˈɑːdˈeɪ" in phonemes("By Sade."), phonemes("By Sade."))
    unprocessed = " | ".join("".join(s) for s in voice.phonemize("By Sade."))
    check("...which the plain text isn't (so the dictionary matters)", "ʃˈɑːdˈeɪ" not in unprocessed, unprocessed)

    # Text path a chat reply takes: clean, check, split, speak
    cleaned = speech.clean_for_speech("**Sure!** Playing Firestone feat. Conrad Sewell, number #3 in the queue.")
    parts = speech.split_sentences(cleaned)
    spoken = [await sp.frames_for(p) for p in parts]
    check("a cleaned reply with a credit and a number is spoken", spoken and all(spoken), parts)
    sp.close()
    logging.getLogger("amy.speech").removeHandler(warnings)

asyncio.run(main())
finish("ALL LIVE PIPER TESTS PASSED")
