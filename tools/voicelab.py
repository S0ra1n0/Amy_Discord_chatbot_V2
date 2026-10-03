"""
Voice lab: hear (and measure) Kokoro voices and blends side by side, to choose AMY_VOICE.

    python tools/voicelab.py af_heart af_bella af_nicole
    python tools/voicelab.py af_heart:0.7,af_bella:0.3 af_heart:0.5,af_bella:0.5 --speed 1.1

Each voice or blend says the same five lines - a greeting, a song title, a long answer,
numbers, a question - into one WAV in voicelab_out/, so you compare like with like. It also
prints each one's average pitch, since "lighter / higher" is part of the brief.
Needs the TTS install and the voices downloaded (python speech.py download a,b,c).
"""
import argparse
import os
import sys
import time
import wave

PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJ)
import speech  # noqa: E402

OUT_DIR = os.path.join(PROJ, "voicelab_out")
LINES = [
    "Hi everyone! I'm Amy. Let me know what you'd like to hear.",
    "Now playing Never Gonna Give You Up, by Rick Astley.",
    "The Great Wall of China is about twenty one thousand kilometres long, "
    "if you count every branch built over the centuries.",
    "Volume's at forty percent. That's number three in the queue, starting in 2 minutes 30.",
    "Did you want me to skip this one, or keep it going?",
]
GAP = b"\x00\x00" * int(speech.ENGINE_RATE * 0.6)        # 0.6 s between lines


def median_pitch_hz(pcm16: bytes, rate: int = speech.ENGINE_RATE) -> float:
    """
    Rough average voice pitch (median fundamental frequency of voiced 40 ms frames), by
    autocorrelation. Good enough to rank voices against each other, not a lab measurement.
    """
    import numpy as np
    x = np.frombuffer(pcm16, dtype="<i2").astype(np.float32) / 32768.0
    frame = int(rate * 0.04)
    lo, hi = int(rate / 400), int(rate / 70)                  # search 70-400 Hz
    loud = np.sqrt(np.mean(x ** 2)) * 0.5
    found = []
    for start in range(0, len(x) - frame, frame):
        f = x[start:start + frame]
        if np.sqrt(np.mean(f ** 2)) < loud:
            continue                                          # silence or a quiet consonant
        f = f - f.mean()
        ac = np.correlate(f, f, mode="full")[frame - 1:]
        if ac[0] <= 0:
            continue
        lag = lo + int(np.argmax(ac[lo:hi]))
        if ac[lag] / ac[0] > 0.3:                             # clearly periodic = voiced
            found.append(rate / lag)
    return float(np.median(found)) if found else 0.0


def write_wav(path: str, pcm16: bytes, rate: int = speech.ENGINE_RATE) -> None:
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm16)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    ap.add_argument("recipes", nargs="+", help="a voice (af_heart) or a blend (af_heart:0.6,af_bella:0.4)")
    ap.add_argument("--speed", type=float, default=1.0)
    args = ap.parse_args(argv)

    recipes = []
    for raw in args.recipes:
        recipe, problem = speech.parse_voice_recipe(raw)
        if problem:
            print(problem)
            return 2
        recipes.append((raw, recipe))

    # Check every voice is downloaded before spending a minute rendering the others
    needed = sorted({n for _, r in recipes for n, _ in r})
    missing = speech.missing_model_files(speech.MODEL_DIR, needed)
    if missing:
        print(f"Not downloaded: {', '.join(missing)}\n"
              f"Run: python speech.py download {','.join(needed)}")
        return 2

    os.makedirs(OUT_DIR, exist_ok=True)
    engine = speech.KokoroEngine(recipes[0][1], speed=args.speed)
    print("Loading Kokoro...")
    engine.load()
    print(f"{'#':>2}  {'voice / blend':<38} {'pitch':>8}  {'seconds':>7}  file")
    for i, (raw, recipe) in enumerate(recipes, 1):
        engine.set_recipe(recipe)
        t0 = time.perf_counter()
        pcm = GAP.join(engine.synthesize(line) for line in LINES)
        took = time.perf_counter() - t0
        name = f"{i:02d} {raw.replace(':', '-').replace(',', '+')}"
        if args.speed != 1.0:
            name += f" x{args.speed}"
        path = os.path.join(OUT_DIR, name + ".wav")
        write_wav(path, speech.normalise_peak(pcm))
        print(f"{i:>2}  {speech.format_recipe(recipe):<38} {median_pitch_hz(pcm):>5.0f} Hz"
              f"  {took:>7.1f}  {os.path.basename(path)}")
    print(f"\nWAVs in {OUT_DIR}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
