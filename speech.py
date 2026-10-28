"""
Amy's voice: turning reply text into audio for a voice call.

Pure helpers - text clean-up, the English check, sentence splitting, voice recipes, audio
framing - plus a swappable engine. Kokoro (and PyTorch) or Piper (and onnxruntime) are
imported only when TTS is turned on and that engine is chosen, so neither the bot nor the test
suite needs them installed; `tests/test_speech.py` checks that importing this module pulls in
none of them.

One-time setup for the Kokoro voice, after `pip install -r requirements-tts.txt`:

    python speech.py download                 # model + the voices in AMY_VOICE
    python speech.py download af_bella,af_sky # extra voices, e.g. for trying blends

One-time setup for the Piper voice (TTS_ENGINE=piper), after
`pip install -r requirements-piper.txt`:

    python speech.py download-piper           # the en_GB cori voice, into piper_model/

The rules this follows are in ARCHITECTURE.md under "Speech".
"""
import asyncio
import concurrent.futures
import hashlib
import importlib
import importlib.util
import json
import logging
import os
import re
import subprocess
import sys
import urllib.request
import warnings
from typing import Any, List, Optional, Sequence, Tuple

with warnings.catch_warnings():
    # Deprecated since 3.11 and removed in 3.13 - known, see requirements-tts.txt.
    warnings.simplefilter("ignore", DeprecationWarning)
    import audioop

log = logging.getLogger("amy.speech")

# Windows would flash a console window for every FFmpeg run without this.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# ---- The model ---------------------------------------------------------------------------
# Pinned to one revision and read from local files only. Left to itself, Kokoro checks Hugging
# Face on every start (measured: 8.6s online vs 7.5s offline) and follows whatever the repo's
# latest files are - so an upstream change could alter Amy's voice, or a Hugging Face outage
# could stop it loading. `python speech.py download` fetches exactly these files once.
KOKORO_REPO = "hexgrad/Kokoro-82M"
KOKORO_REVISION = "f3ff3571791e39611d31c381e3a41a3af07b4987"
MODEL_FILE = "kokoro-v1_0.pth"
CONFIG_FILE = "config.json"
MODEL_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "kokoro_model")

# The spaCy model Kokoro's English text handling needs. If it's missing, Kokoro runs
# `pip install` by itself at runtime; the bot checks for it first instead (see load()).
SPACY_MODEL = "en_core_web_sm"

ENGINE_RATE = 24000          # Kokoro outputs 24 kHz mono
DISCORD_RATE = 48000         # Discord takes 48 kHz stereo...
FRAME_BYTES = 3840           # ...in 20 ms frames of 16-bit samples: 960 x 2 channels x 2 bytes

# A single voice that is always downloaded: the reference the tests and the voice lab use.
DEFAULT_VOICE = "af_heart"
# PyTorch CPU threads (TTS_THREADS). Measured on this 16-thread CPU, per short sentence:
# 4 threads 1.41s / ~4 cores busy, 6 threads 1.11s / ~6, 8 threads 0.91s / ~8. Six leaves
# headroom for FFmpeg, Discord's audio thread and Ollama while staying near the 1s target.
DEFAULT_THREADS = 6
MAX_SPOKEN_CHARS = 400       # anything longer is cut at a sentence boundary
PEAK_TARGET = 0.7            # speech peak as a fraction of full scale - headroom for mixing
# How loud Amy's voice is, on top of PEAK_TARGET (VOICE_LEVEL). 0.75 since the first live
# test, where her voice drowned out the music.
DEFAULT_VOICE_LEVEL = 0.75

# ---- The Piper voice (optional engine, TTS_ENGINE=piper) -----------------------------------
# Same idea as the Kokoro model: one pinned revision, fetched once by `python speech.py
# download-piper` into piper_model/, read from local files only. The voice is "cori" (en_GB,
# female, medium quality, 22.05 kHz), trained on LibriVox public-domain recordings
# (the voice's MODEL_CARD). Both files are checked against these SHA-256 hashes.
PIPER_REPO = "rhasspy/piper-voices"
PIPER_REVISION = "5041a7a49ae5b3a0f30e6837769eb03fdbb231bd"
PIPER_VOICE_NAME = "en_GB-cori-medium"
PIPER_REPO_DIR = "en/en_GB/cori/medium"
PIPER_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "piper_model")
PIPER_MODEL = os.path.join(PIPER_DIR, PIPER_VOICE_NAME + ".onnx")
PIPER_SHA256 = {
    PIPER_VOICE_NAME + ".onnx": "1899f98e5fb8310154f3c2973f4b8a929ba7245e722b3d3a85680b833d95f10d",
    PIPER_VOICE_NAME + ".onnx.json": "e262c16d7f192f69d4edd6b4ef8a5915379e67495fcc402f1ab15eeb33da3d36",
}
# Amy's Piper voice, chosen by ear on 2026-10-28 (TTS_PLAN.md, T0.1 route 2): a little slower
# than the model's own pace, a semitone up with the formants (the "body" of the voice) kept,
# and a softened top end. noise_scale / noise_w are how much random variation the model adds
# to the sound and to the rhythm; they stay fixed.
DEFAULT_PIPER_LENGTH = 1.10          # length_scale: above 1 = slower
DEFAULT_PIPER_PITCH = 1.0            # semitones; 0 = leave the pitch alone
PIPER_NOISE_SCALE = 0.5
PIPER_NOISE_W = 0.5
PIPER_FFMPEG_TIMEOUT = 20.0          # seconds, for one sentence through the filter chain


# ---- Text clean-up -------------------------------------------------------------------------
_CODE_BLOCK = re.compile(r"```.*?(```|$)", re.S)
_INLINE_CODE = re.compile(r"`([^`\n]*)`")
_MD_LINK = re.compile(r"\[([^\]\n]+)\]\((?:https?://|www\.)[^)\s]+\)")
_URL = re.compile(r"(?:https?://|www\.)\S+")
_DISCORD_TOKEN = re.compile(r"<(?:@[!&]?\d+|#\d+|a?:\w+:\d+|t:\d+(?::\w)?)>")
_MASS_PING = re.compile(r"@(?:everyone|here)\b")
_LONG_NUMBER = re.compile(r"\b\d{15,}\b")             # Discord IDs, never worth reading out
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+", re.M)
_QUOTE = re.compile(r"^\s*>+\s?", re.M)
_BULLET = re.compile(r"^\s*(?:[-*+\u2022]|\d+[.)])\s+", re.M)
_MARKERS = re.compile(r"\*\*|__|~~|\|\|")
_EMPHASIS = re.compile(r"(?<!\w)[*_](\S(?:[^*_\n]*\S)?)[*_](?!\w)")
_EMOJI = re.compile(
    "[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U00002300-\U000023FF"
    "\U00002B00-\U00002BFF\U0001F1E6-\U0001F1FF\u200d\ufe0f\u20e3]+")
_SENTENCE_END = re.compile(r"[.!?\u2026:;,]$")

CODE_PLACEHOLDER = "I've put the code in the chat."


def clean_for_speech(text: str, max_chars: int = MAX_SPOKEN_CHARS) -> str:
    """
    Turn a chat reply into something worth saying out loud. Pure.

    Markdown goes, links become "a link", code blocks become one short line, emoji and
    Discord's <@id>/<#id>/<:emoji:id> tokens disappear, and @everyone/@here are never
    spoken. Each line becomes its own sentence, so a bulleted list reads as a list. The
    result is capped at `max_chars`, cut at a sentence boundary where one is near.
    """
    t = _CODE_BLOCK.sub(" " + CODE_PLACEHOLDER + "\n", text)
    t = _INLINE_CODE.sub(r"\1", t)
    t = _MD_LINK.sub(r"\1", t)
    t = _URL.sub("a link", t)
    t = _DISCORD_TOKEN.sub("", t)
    t = _MASS_PING.sub("", t)
    t = _LONG_NUMBER.sub("", t)
    t = _HEADING.sub("", t)
    t = _QUOTE.sub("", t)
    t = _BULLET.sub("", t)
    t = _MARKERS.sub("", t)
    t = _EMPHASIS.sub(r"\1", t)
    t = _EMOJI.sub("", t)

    sentences = []
    for line in t.splitlines():
        line = re.sub(r"\s+", " ", line).strip()
        if not line:
            continue
        if not _SENTENCE_END.search(line):
            line += "."
        sentences.append(line)
    t = " ".join(sentences)
    t = re.sub(r"\s+([.!?,;:])", r"\1", t)            # "word ." -> "word."
    t = re.sub(r"(?<!\.)\.\.(?!\.)", ".", t)           # a doubled full stop, not an ellipsis
    return _cap(t.strip(), max_chars)


def _cap(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    head = text[:max_chars]
    cut = max(head.rfind(". "), head.rfind("! "), head.rfind("? "))
    if cut >= max_chars * 0.4:                         # a sentence end not too far back
        return head[:cut + 1]
    space = head.rfind(" ")
    return (head[:space] if space > 0 else head).rstrip(",;: ") + "\u2026"


# ---- English check -------------------------------------------------------------------------
# Kokoro is set up for English only (D6). A reply is treated as English unless more than this
# share of its words contain letters outside basic ASCII - Vietnamese marks nearly every word
# (most of a sentence), while English with a borrowed word like "pho" or "cafe" spelt with
# accents stays far below it. Non-Latin scripts count as non-ASCII letters too.
MAX_FOREIGN_WORD_SHARE = 0.25
_WORD = re.compile(r"[^\W\d_]+", re.UNICODE)


def is_english(text: str) -> bool:
    """Whether Kokoro's English voice can sensibly read `text`. Pure."""
    words = _WORD.findall(text)
    if not words:
        return True                                    # numbers and punctuation read fine
    foreign = sum(1 for w in words if any(ord(c) > 127 for c in w))
    return foreign / len(words) <= MAX_FOREIGN_WORD_SHARE


# ---- Sentence splitting ---------------------------------------------------------------------
_SPLIT = re.compile(r"(?<=[.!?\u2026])\s+")
_ABBREVIATIONS = ("mr.", "mrs.", "ms.", "dr.", "st.", "vs.", "e.g.", "i.e.", "jr.", "sr.",
                  "no.", "prof.", "approx.", "feat.", "ft.")
MIN_SENTENCE_CHARS = 12      # shorter pieces join the next, so speech isn't choppy


def split_sentences(text: str) -> List[str]:
    """
    Split cleaned text into sentences to synthesise one at a time, so Amy starts speaking
    after the first instead of waiting for the whole reply. Pure.

    Splits only where punctuation is followed by a space, so "3.14" stays whole. Pieces
    ending in a common abbreviation ("Dr.") or shorter than MIN_SENTENCE_CHARS join the next.
    """
    pieces = [p.strip() for p in _SPLIT.split(text.strip()) if p.strip()]
    out: List[str] = []
    carry = ""
    for piece in pieces:
        current = (carry + " " + piece).strip() if carry else piece
        # An opening bracket stays attached to the word: "(feat." must still count as "feat."
        last_word = current.rsplit(" ", 1)[-1].lower().lstrip("([{\"'")
        if last_word in _ABBREVIATIONS or len(current) < MIN_SENTENCE_CHARS:
            carry = current
            continue
        out.append(current)
        carry = ""
    if carry:
        if out and len(carry) < MIN_SENTENCE_CHARS:
            out[-1] = out[-1] + " " + carry
        else:
            out.append(carry)
    return out


# ---- Voice recipes --------------------------------------------------------------------------
# AMY_VOICE is one Kokoro voice ("af_heart") or a weighted blend ("af_heart:0.6,af_bella:0.4").
# Only English voices fit the English pipeline: American (a) or British (b), female or male.
_VOICE_NAME = re.compile(r"^[ab][fm]_[a-z]+$")
MAX_BLEND_VOICES = 5
Recipe = List[Tuple[str, float]]

# Amy's voice, chosen by ear in the voice lab on 2026-10-04 (Phase 1E): three rounds, from all
# 15 English female voices to a shortlist (heart, bella, isabella) to this blend of the two
# top-graded ones (A and A-), with no accent mismatch. Used when AMY_VOICE is unset or invalid.
DEFAULT_RECIPE: Recipe = [("af_heart", 0.4), ("af_bella", 0.6)]
DEFAULT_SPEED = 1.05
DEFAULT_RECIPE_NAME = "Amy's default voice (af_heart 40% + af_bella 60%)"


def parse_voice_recipe(raw: Optional[str]) -> Tuple[Recipe, Optional[str]]:
    """
    Read AMY_VOICE. Returns (recipe, problem) like config.parse_*: weights normalised to add
    up to 1; anything malformed falls back to the default voice and says why.
    """
    default: Recipe = list(DEFAULT_RECIPE)
    if raw is None or not raw.strip():
        return default, None
    recipe: Recipe = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        name, _, weight_text = part.partition(":")
        name = name.strip().lower()
        if not _VOICE_NAME.match(name):
            return default, (f"AMY_VOICE: {name!r} isn't an English Kokoro voice name "
                             f"(like af_heart or bf_lily); using {DEFAULT_RECIPE_NAME}.")
        try:
            weight = float(weight_text) if weight_text.strip() else 1.0
        except ValueError:
            return default, f"AMY_VOICE: {weight_text.strip()!r} isn't a number; using {DEFAULT_RECIPE_NAME}."
        if not weight > 0:
            return default, f"AMY_VOICE: weights must be above 0; using {DEFAULT_RECIPE_NAME}."
        if any(n == name for n, _ in recipe):
            return default, f"AMY_VOICE: {name} is listed twice; using {DEFAULT_RECIPE_NAME}."
        recipe.append((name, weight))
    if not recipe:
        return default, None
    if len(recipe) > MAX_BLEND_VOICES:
        return default, (f"AMY_VOICE: at most {MAX_BLEND_VOICES} voices in a blend; "
                         f"using {DEFAULT_RECIPE_NAME}.")
    total = sum(w for _, w in recipe)
    return [(n, w / total) for n, w in recipe], None


def format_recipe(recipe: Recipe) -> str:
    """'af_heart' or 'af_heart 60% + af_bella 40%', for /voice and the log."""
    if len(recipe) == 1:
        return recipe[0][0]
    return " + ".join(f"{n} {round(w * 100)}%" for n, w in recipe)


# ---- Choosing and describing the engine -----------------------------------------------------
ENGINES = ("kokoro", "piper")


def parse_engine(raw: Optional[str]) -> Tuple[str, Optional[str]]:
    """Read TTS_ENGINE. Returns (engine, problem) like config.parse_*; unknown -> kokoro."""
    if raw is None or not raw.strip():
        return "kokoro", None
    word = raw.strip().lower()
    if word in ENGINES:
        return word, None
    return "kokoro", f"TTS_ENGINE={raw.strip()!r} isn't one of {', '.join(ENGINES)}; using kokoro."


def resolve_piper_path(raw: Optional[str]) -> str:
    """PIPER_VOICE -> the .onnx path. Blank = the downloaded cori voice; relative = from the project."""
    if raw is None or not raw.strip():
        return PIPER_MODEL
    path = os.path.expanduser(raw.strip())
    if not os.path.isabs(path):
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), path)
    return os.path.normpath(path)


def format_piper(model_path: str, length: float, pitch: float, soft: bool) -> str:
    """'Piper en_GB-cori-medium (length 1.10, pitch +1.0 st, soft EQ)', for /voice and the log."""
    name = os.path.basename(model_path)
    if name.endswith(".onnx"):
        name = name[:-5]
    pitch_text = f"pitch {pitch:+.1f} st" if pitch else "pitch unchanged"
    return f"Piper {name} (length {length:.2f}, {pitch_text}, soft EQ {'on' if soft else 'off'})"


# ---- Piper: text fixes and the audio filter chain ---------------------------------------------
# Piper reads text through espeak-ng, which says some things the way a dictionary would rather
# than the way a song list does. These fixes come from listening to a round of test lines
# (TTS_PLAN.md, T0.1 route 2, round 4). They run only for Piper, inside PiperEngine.synthesize:
# Kokoro has its own text handling and was tuned without them.
#
# Names espeak gets wrong, as respellings it reads right. A respelling (not a raw `[[ ipa ]]`
# block) on purpose: Piper drops the full stop after a raw block and glues the next sentence on,
# and even turns "!" into the word "exclamation". Keys are lower case; accented and plain
# spellings are separate keys because song titles often lose the accents. To add a name, check
# how espeak reads the respelling (voice.phonemize) before trusting it.
PIPER_NAMES = {
    "sade": "Shah-day",
    "måneskin": "Mawneskin",
    "maneskin": "Mawneskin",
    "einaudi": "Eye-now-dee",
    "beyoncé": "Bee-yon-say",
    "beyonce": "Bee-yon-say",
    "dua lipa": "Dooah Leepah",
}
_PIPER_NAME_RE = re.compile(
    r"(?<!\w)(" + "|".join(re.escape(k).replace(r"\ ", r"\s+")
                           for k in sorted(PIPER_NAMES, key=len, reverse=True)) + r")(?!\w)",
    re.I)
_RAW_PHONEMES = re.compile(r"[\[\]]")
# "feat. Name" / "ft. Name": a capital or digit follows, and what comes before is a comma,
# bracket or dash, or a capitalised word (a title or name) - so the noun in "What a feat. Next
# up..." and "6 ft. tall" stay as they are. The full stop is swallowed so it can't end the
# sentence.
_FEATURING = re.compile(r"(?<![\w.])(?<!\d )(?<!\d)(?i:feat|ft)\.?(?=\s+[A-Z0-9])")


def _featuring(m: "re.Match[str]") -> str:
    before = m.string[:m.start()].rstrip()
    if not before or before[-1] in ",([-–—&":
        return "featuring"
    return "featuring" if before.split()[-1][:1].isupper() else m.group(0)
_HASH_NUMBER = re.compile(r"(?<![\w&])#(\d+)\b")
_DURATION_UNIT = re.compile(r"(?<!\w)(\d+(?:\.\d+)?)\s*(mins?|secs?)\b", re.I)
_ZERO_MINUTES = re.compile(r"(?<![\d:.])(\d{1,2}):0([1-9])(?![\d:])")


def _duration(m: "re.Match[str]") -> str:
    unit = "minute" if m.group(2).lower().startswith("min") else "second"
    return f"{m.group(1)} {unit}" + ("" if m.group(1) == "1" else "s")


def piper_text(text: str) -> str:
    """
    Fix what espeak-ng mispronounces in song-list text before Piper reads it. Pure.

    "feat." and "ft." become "featuring" (and no longer end the sentence), "#3" becomes
    "number 3", "2 min" / "30 secs" become minutes / seconds, "1:05" becomes "1 oh 5" ("2:30"
    is read fine as it is), and the names in PIPER_NAMES are respelt. Words are matched whole
    and case-insensitively, so nothing inside another word changes. `[[` and `]]` are removed:
    Piper treats text between them as raw phonemes, which chat text must not be able to do.
    Every `[` and `]` is removed (not just the pairs): removing pairs in one pass lets "[]][x][[]"
    collapse into a fresh "[[x]]".
    """
    t = _RAW_PHONEMES.sub("", text)
    t = _FEATURING.sub(_featuring, t)
    t = _HASH_NUMBER.sub(r"number \1", t)
    t = _DURATION_UNIT.sub(_duration, t)
    t = _ZERO_MINUTES.sub(r"\1 oh \2", t)
    t = _PIPER_NAME_RE.sub(
        lambda m: PIPER_NAMES.get(re.sub(r"\s+", " ", m.group(1).lower()), m.group(0)), t)
    return re.sub(r"\s+", " ", t).strip()


def piper_filter_chain(pitch_semitones: float, soft: bool, has_rubberband: bool) -> Optional[str]:
    """
    The FFmpeg -af chain applied to Piper's audio, or None when there is nothing to do. Pure.

    Pitch: rubberband shifts it by `pitch_semitones` and keeps the formants (the vocal-tract
    "body"), so she sounds higher, not smaller. Needs an FFmpeg built with rubberband; without
    it the pitch step is dropped. Soft: a gentle cut around 4.5 kHz, a shelf above 6.5 kHz, and
    a de-esser take the edge off the "s" sounds and hiss.
    """
    parts: List[str] = []
    if pitch_semitones and has_rubberband:
        parts.append(f"rubberband=pitch={2 ** (pitch_semitones / 12):.6f}:formant=preserved")
    if soft:
        parts.append("equalizer=f=4500:t=q:w=1:g=-3")
        parts.append("highshelf=f=6500:g=-3")
        parts.append("deesser=i=0.4:m=0.5:f=0.5:s=o")
    return ",".join(parts) if parts else None


_FILTER_LINE = re.compile(r"^\s*[A-Z.]{2,3}\s+(\S+)\s+\S+->\S+", re.M)
SOFT_FILTERS = ("equalizer", "highshelf", "deesser")


def parse_ffmpeg_filters(listing: str) -> set:
    """The filter names in `ffmpeg -filters` output. Pure."""
    return set(_FILTER_LINE.findall(listing))


# ---- Audio -----------------------------------------------------------------------------------
def normalise_peak(pcm16: bytes, target: float = PEAK_TARGET) -> bytes:
    """
    Scale 16-bit audio so its loudest sample sits at `target` of full scale. Pure.

    Mixing adds the voice to the (lowered) music, and audioop.add saturates - a voice that
    peaks at full scale would clip. A fixed peak also keeps every sentence at one loudness.
    Near-silence isn't boosted more than 4x, so it can't be turned into loud noise.
    """
    if not pcm16:
        return pcm16
    peak = audioop.max(pcm16, 2)
    if peak == 0:
        return pcm16
    factor = min(target * 32767 / peak, 4.0)
    return audioop.mul(pcm16, 2, factor)


def to_discord_frames(pcm16_mono: bytes, rate: int = ENGINE_RATE) -> List[bytes]:
    """
    Engine audio (16-bit mono at `rate`) -> 20 ms frames of 48 kHz stereo, ready to send.
    Pure. The last frame is padded with silence so every frame is exactly FRAME_BYTES.
    """
    if not pcm16_mono:
        return []
    if rate != DISCORD_RATE:
        pcm16_mono, _ = audioop.ratecv(pcm16_mono, 2, 1, rate, DISCORD_RATE, None)
    stereo = audioop.tostereo(pcm16_mono, 2, 1, 1)
    if len(stereo) % FRAME_BYTES:
        stereo += b"\x00" * (FRAME_BYTES - len(stereo) % FRAME_BYTES)
    return [stereo[i:i + FRAME_BYTES] for i in range(0, len(stereo), FRAME_BYTES)]


# ---- Engines ---------------------------------------------------------------------------------
class SpeechUnavailable(RuntimeError):
    """The engine can't run here; the message says how to fix it."""


def missing_model_files(model_dir: str, voices: Sequence[str]) -> List[str]:
    """The files `python speech.py download` still needs to fetch, as repo-relative paths."""
    needed = [CONFIG_FILE, MODEL_FILE] + [f"voices/{v}.pt" for v in voices]
    return [f for f in needed if not os.path.isfile(os.path.join(model_dir, f))]


class SpeechEngine:
    """Text in, 16-bit mono audio out. Both methods block; Speaker runs them on its worker."""
    name = "engine"
    rate = ENGINE_RATE

    def load(self) -> None:
        raise NotImplementedError

    def synthesize(self, text: str) -> bytes:
        raise NotImplementedError


class KokoroEngine(SpeechEngine):
    """Kokoro on the CPU, from the pinned local files only."""
    name = "Kokoro"

    def __init__(self, recipe: Recipe, speed: float = 1.0, threads: int = DEFAULT_THREADS,
                 model_dir: str = MODEL_DIR) -> None:
        self.recipe = recipe
        self.speed = speed
        self.threads = threads
        self.model_dir = model_dir
        self._pipeline: Any = None
        self._voice: Any = None
        self._np: Any = None
        self._torch: Any = None

    def load(self) -> None:
        missing = missing_model_files(self.model_dir, [n for n, _ in self.recipe])
        if missing:
            raise SpeechUnavailable(
                f"Kokoro's files aren't downloaded ({', '.join(missing[:3])}"
                f"{'...' if len(missing) > 3 else ''}). Run: python speech.py download")
        if importlib.util.find_spec("kokoro") is None:
            raise SpeechUnavailable("Kokoro isn't installed. Run: pip install -r requirements-tts.txt")
        # Checked before Kokoro loads it: a missing spaCy model makes Kokoro pip-install one
        # at runtime, which the bot must never do.
        if importlib.util.find_spec(SPACY_MODEL) is None:
            raise SpeechUnavailable(f"spaCy's {SPACY_MODEL} isn't installed. "
                                    "Run: pip install -r requirements-tts.txt")
        # Everything comes from local paths below; this makes sure nothing else reaches out.
        os.environ["HF_HUB_OFFLINE"] = "1"
        torch = importlib.import_module("torch")
        kokoro = importlib.import_module("kokoro")
        self._np = importlib.import_module("numpy")
        torch.set_num_threads(self.threads)

        model = kokoro.KModel(repo_id=KOKORO_REPO,
                              config=os.path.join(self.model_dir, CONFIG_FILE),
                              model=os.path.join(self.model_dir, MODEL_FILE)).to("cpu").eval()
        self._pipeline = kokoro.KPipeline(lang_code="a", repo_id=KOKORO_REPO, model=model)
        self._torch = torch
        self.set_recipe(self.recipe)
        self.synthesize("Ready.")       # the first call is slow; pay it here, not mid-call

    def set_recipe(self, recipe: Recipe) -> None:
        """
        Switch voice without reloading the model (~0.1s instead of ~9s). Used by the voice lab
        to compare blends; the files must already be downloaded.

        A weighted blend, computed here: Kokoro's own "a,b" syntax only takes equal weights.
        Each voice is a 510 x 256 block of numbers, so a blend is simply the weighted sum.
        """
        missing = missing_model_files(self.model_dir, [n for n, _ in recipe])
        if missing:
            raise SpeechUnavailable(f"Not downloaded: {', '.join(missing)}. "
                                    f"Run: python speech.py download {','.join(n for n, _ in recipe)}")
        torch = self._torch
        packs = [torch.load(os.path.join(self.model_dir, "voices", f"{n}.pt"),
                            weights_only=True) * w for n, w in recipe]
        self._voice = torch.stack(packs).sum(dim=0)
        self.recipe = recipe

    def synthesize(self, text: str) -> bytes:
        if self._pipeline is None:
            raise SpeechUnavailable("Kokoro isn't loaded")
        np = self._np
        chunks = []
        for result in self._pipeline(text, voice=self._voice, speed=self.speed):
            audio = result.audio
            if audio is not None:
                chunks.append(audio.numpy() if hasattr(audio, "numpy") else audio)
        if not chunks:
            return b""
        samples = np.clip(np.concatenate(chunks), -1.0, 1.0)
        return (samples * 32767).astype("<i2").tobytes()


def missing_piper_files(model_path: str) -> List[str]:
    """The Piper voice files (.onnx and its .json) that don't exist yet."""
    return [p for p in (model_path, model_path + ".json") if not os.path.isfile(p)]


class PiperEngine(SpeechEngine):
    """
    Piper (a small ONNX voice, GPL-3.0 package, optional) on the CPU, from local files only.

    Text -> `piper_text` -> Piper -> optionally FFmpeg's filter chain (`piper_filter_chain`)
    -> 16-bit mono at the voice's own rate. Everything after Piper degrades quietly: no FFmpeg,
    no rubberband or a failing filter run all mean "speak without that step", with one warning.
    """
    name = "Piper"
    rate = 22050                 # replaced by the voice's own rate in load()

    def __init__(self, model_path: str = PIPER_MODEL, length: float = DEFAULT_PIPER_LENGTH,
                 pitch: float = DEFAULT_PIPER_PITCH, soft: bool = True,
                 threads: int = DEFAULT_THREADS, ffmpeg: Optional[str] = None) -> None:
        self.model_path = model_path
        self.length = length
        self.pitch = pitch
        self.soft = soft
        self.threads = threads
        self.ffmpeg = ffmpeg                    # injected: speech.py must not import music.py
        self.chain: Optional[str] = None        # decided in load()
        self._voice: Any = None
        self._cfg: Any = None
        self._ffmpeg_warned = False

    def load(self) -> None:
        missing = missing_piper_files(self.model_path)
        if missing:
            names = ', '.join(os.path.basename(m) for m in missing)
            if os.path.normpath(self.model_path) == os.path.normpath(PIPER_MODEL):
                raise SpeechUnavailable(f"Piper's voice files aren't downloaded ({names}). "
                                        "Run: python speech.py download-piper")
            raise SpeechUnavailable(f"Piper's voice file isn't there ({names} at {self.model_path}). "
                                    "Check PIPER_VOICE in .env, or leave it blank to use the "
                                    "downloaded cori voice.")
        if importlib.util.find_spec("piper") is None or importlib.util.find_spec("onnxruntime") is None:
            raise SpeechUnavailable("Piper isn't installed. Run: pip install -r requirements-piper.txt")
        piper = importlib.import_module("piper")
        ort = importlib.import_module("onnxruntime")
        with open(self.model_path + ".json", encoding="utf-8") as f:
            config = piper.PiperConfig.from_dict(json.load(f))
        # Built here rather than with PiperVoice.load(): that one makes its own onnxruntime
        # session, which takes every core, and TTS_THREADS has to bound speech's CPU use.
        options = ort.SessionOptions()
        options.intra_op_num_threads = self.threads
        options.inter_op_num_threads = 1
        session = ort.InferenceSession(self.model_path, sess_options=options,
                                       providers=["CPUExecutionProvider"])
        self._voice = piper.PiperVoice(config=config, session=session)
        self._cfg = piper.SynthesisConfig(length_scale=self.length, noise_scale=PIPER_NOISE_SCALE,
                                          noise_w_scale=PIPER_NOISE_W)
        self.rate = int(config.sample_rate)
        self.chain = self._plan_chain()
        self.synthesize("Ready.")       # the first call is slow; pay it here, not mid-call

    def _plan_chain(self) -> Optional[str]:
        """Work out which filters FFmpeg can really run here; warn once about each gap."""
        if not self.pitch and not self.soft:
            return None
        if not self.ffmpeg:
            log.warning("Piper: FFmpeg not found - speaking without the pitch/soft-EQ polish")
            return None
        try:
            listing = subprocess.run(
                [self.ffmpeg, "-hide_banner", "-filters"], capture_output=True, text=True,
                errors="replace", timeout=PIPER_FFMPEG_TIMEOUT, creationflags=_NO_WINDOW).stdout
        except (OSError, subprocess.SubprocessError) as e:
            log.warning(f"Piper: couldn't ask FFmpeg for its filters ({e}) - "
                        "speaking without the pitch/soft-EQ polish")
            return None
        names = parse_ffmpeg_filters(listing)
        has_rubberband = "rubberband" in names
        soft = self.soft
        if self.pitch and not has_rubberband:
            log.warning("Piper: this FFmpeg has no rubberband filter - skipping the pitch shift")
        if soft and not all(f in names for f in SOFT_FILTERS):
            log.warning("Piper: this FFmpeg lacks equalizer/highshelf/deesser - skipping the soft EQ")
            soft = False
        return piper_filter_chain(self.pitch, soft, has_rubberband)

    def _filter(self, pcm: bytes) -> bytes:
        """Run `pcm` through the chain. On any failure, say so once and return it as it was."""
        if not self.ffmpeg or not self.chain:
            return pcm
        try:
            r = subprocess.run(
                [self.ffmpeg, "-hide_banner", "-loglevel", "error",
                 "-f", "s16le", "-ar", str(self.rate), "-ac", "1", "-i", "pipe:0",
                 "-af", self.chain,
                 "-f", "s16le", "-ar", str(self.rate), "-ac", "1", "pipe:1"],
                input=pcm, capture_output=True, timeout=PIPER_FFMPEG_TIMEOUT,
                creationflags=_NO_WINDOW)
            if r.returncode == 0 and r.stdout:
                return r.stdout
            reason = (r.stderr or b"").decode("utf-8", "replace").strip()[-200:] or f"exit {r.returncode}"
        except subprocess.TimeoutExpired:
            # A hung FFmpeg would otherwise stall every sentence for the full timeout on the
            # one speech thread. Switch the polish off until restart.
            # Logged on its own, not behind the once-only flag: an earlier, different failure
            # mustn't hide from amy.log that the polish has now been switched off.
            self.chain = None
            log.warning(f"Piper: FFmpeg timed out after {PIPER_FFMPEG_TIMEOUT:.0f}s - speaking "
                        "unprocessed, post-processing is now off until restart")
            return pcm
        except (OSError, subprocess.SubprocessError) as e:
            reason = str(e) or type(e).__name__
        if not self._ffmpeg_warned:
            self._ffmpeg_warned = True
            log.warning(f"Piper: the FFmpeg filter step failed ({reason}) - speaking unprocessed")
        return pcm

    def synthesize(self, text: str) -> bytes:
        if self._voice is None:
            raise SpeechUnavailable("Piper isn't loaded")
        spoken = piper_text(text)
        if not spoken:
            return b""
        pcm = b"".join(chunk.audio_int16_bytes for chunk in self._voice.synthesize(spoken, self._cfg))
        if not pcm or not self.chain:
            return pcm
        return self._filter(pcm)


class FakeEngine(SpeechEngine):
    """For tests: 'speaks' 50 ms of silence per character, and records what it was given."""
    name = "fake"

    def __init__(self, fail_load: Optional[str] = None) -> None:
        self.fail_load = fail_load
        self.loaded = False
        self.loads = 0
        self.spoken: List[str] = []
        self.threads_used: List[str] = []

    def load(self) -> None:
        self.loads += 1
        if self.fail_load:
            raise SpeechUnavailable(self.fail_load)
        self.loaded = True

    def synthesize(self, text: str) -> bytes:
        import threading
        self.spoken.append(text)
        self.threads_used.append(threading.current_thread().name)
        return b"\x00\x00" * int(self.rate * 0.05 * len(text))


# ---- Speaker: one engine, one worker thread ----------------------------------------------------
class Speaker:
    """
    Owns the engine's lifecycle. Loading and every synthesis run on ONE dedicated thread:
    synthesis takes most of a second of CPU and must never block the event loop, and Kokoro
    isn't documented as safe to call from two threads at once.

    Never raises: a failed load leaves Amy working without a voice, with the reason in
    `problem` and the log.
    """

    def __init__(self, engine: SpeechEngine, level: float = 1.0) -> None:
        self.engine = engine
        self.level = level                       # voice volume: 1.0 = PEAK_TARGET
        self.state = "off"                       # off -> loading -> ready | failed
        self.problem: Optional[str] = None
        self._pool = concurrent.futures.ThreadPoolExecutor(max_workers=1,
                                                           thread_name_prefix="speech")

    @property
    def ready(self) -> bool:
        return self.state == "ready"

    async def start(self) -> bool:
        """Load the engine in the background. Safe to call again; loads once."""
        if self.state in ("loading", "ready"):
            return self.ready
        self.state = "loading"
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(self._pool, self.engine.load)
        except Exception as e:
            self.state, self.problem = "failed", str(e) or type(e).__name__
            log.warning(f"Voice unavailable, Amy will stay text-only: {self.problem}")
            return False
        self.state, self.problem = "ready", None
        log.info(f"Voice ready ({self.engine.name})")
        return True

    async def frames_for(self, text: str) -> List[bytes]:
        """Synthesise one sentence into Discord frames. [] if not ready or it fails."""
        if not self.ready or not text.strip():
            return []
        log.debug(f"Speaking: {text}")           # DEBUG only: speech is chat text
        loop = asyncio.get_running_loop()
        try:
            pcm = await loop.run_in_executor(self._pool, self.engine.synthesize, text)
        except Exception as e:
            log.warning(f"Couldn't synthesise a sentence: {e}")
            return []
        return to_discord_frames(normalise_peak(pcm, PEAK_TARGET * self.level),
                                 self.engine.rate)

    def close(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)


# ---- One-time download -------------------------------------------------------------------------
def download(voices: Sequence[str], model_dir: str = MODEL_DIR) -> List[str]:
    """Fetch the pinned model and `voices` into `model_dir`. Returns what was fetched."""
    hub = importlib.import_module("huggingface_hub")
    fetched = []
    for f in missing_model_files(model_dir, voices):
        hub.hf_hub_download(repo_id=KOKORO_REPO, filename=f, revision=KOKORO_REVISION,
                            local_dir=model_dir)
        fetched.append(f)
    return fetched


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def download_piper(model_dir: str = PIPER_DIR) -> List[str]:
    """
    Fetch the pinned Piper voice (.onnx + .onnx.json) into `model_dir`. Returns what was
    fetched. Plain HTTPS from the pinned Hugging Face revision - no extra package needed - and
    each file is checked against PIPER_SHA256 before it is put in place, so a damaged or
    swapped download can never become Amy's voice.
    """
    os.makedirs(model_dir, exist_ok=True)
    fetched = []
    for name, want in PIPER_SHA256.items():
        dest = os.path.join(model_dir, name)
        if os.path.isfile(dest) and _sha256(dest) == want:
            continue
        url = f"https://huggingface.co/{PIPER_REPO}/resolve/{PIPER_REVISION}/{PIPER_REPO_DIR}/{name}"
        part = dest + ".part"
        try:
            with urllib.request.urlopen(url, timeout=60) as resp, open(part, "wb") as out:
                for block in iter(lambda: resp.read(1 << 20), b""):
                    out.write(block)
        except BaseException:
            if os.path.exists(part):
                os.remove(part)
            raise
        if _sha256(part) != want:
            os.remove(part)
            raise RuntimeError(f"{name} downloaded but its checksum doesn't match the pinned one")
        os.replace(part, dest)
        fetched.append(name)
    return fetched


def _main(argv: Sequence[str]) -> int:
    if len(argv) >= 2 and argv[1] == "download-piper":
        print(f"Fetching the Piper voice {PIPER_VOICE_NAME} ({PIPER_REVISION[:7]}) into {PIPER_DIR} ...")
        fetched = download_piper()
        print("Fetched: " + (", ".join(fetched) if fetched else "nothing - already up to date"))
        return 0
    if len(argv) < 2 or argv[1] != "download":
        print(__doc__)
        return 2
    if len(argv) > 2:
        voices = [v.strip() for v in argv[2].split(",") if v.strip()]
    else:
        from dotenv import dotenv_values
        env = dotenv_values(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
        recipe, problem = parse_voice_recipe(os.environ.get("AMY_VOICE") or env.get("AMY_VOICE"))
        if problem:
            print(problem)
        voices = [n for n, _ in recipe]
    voices = sorted(set(voices) | {DEFAULT_VOICE} | {n for n, _ in DEFAULT_RECIPE})
    bad = [v for v in voices if not _VOICE_NAME.match(v)]
    if bad:
        print("Not English Kokoro voice names:", ", ".join(bad))
        return 2
    print(f"Fetching Kokoro ({KOKORO_REVISION[:7]}) and voices {', '.join(voices)} "
          f"into {MODEL_DIR} ...")
    fetched = download(voices)
    print("Fetched: " + (", ".join(fetched) if fetched else "nothing - already up to date"))
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv))
