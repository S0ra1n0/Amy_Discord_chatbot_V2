"""
What Amy says out loud: spoken confirmations of music commands, and how a chat reply becomes
speech. Pure - no Discord, no Ollama - so every line is testable offline.

Confirmations come in several phrasings picked at random, so a run of commands doesn't sound
canned. They're plain text, generated instantly, never by the model. Track titles are cleaned
first: "Song Name (Official Video) [4K]" is read as "Song Name".
"""
import random
import re
from typing import Mapping, Optional, Tuple

import llm
import speech

# ---- Speakable pieces ---------------------------------------------------------------------------
_BRACKETS = re.compile(r"\s*[\(\[\{][^\)\]\}]*[\)\]\}]")
_TITLE_NOISE = re.compile(r"\b(official|music|lyric|lyrics|audio|video|hd|4k|mv|visualizer)\b",
                          re.I)
MAX_TITLE_CHARS = 60


def speakable_title(title: str) -> str:
    """
    A track title as it should be read aloud. Pure.

    Drops bracketed extras ("(Official Video)", "[4K Remaster]"), anything after " | " or
    a " // " separator, and file extensions; caps the length at a word boundary.
    """
    t = re.split(r"\s+(?:\||//)\s+", title, maxsplit=1)[0]
    t = re.sub(r"\.(mp3|m4a|opus|wav|flac|ogg)$", "", t, flags=re.I)
    t = t.replace("_", " ")                       # file names: "my_song" reads as "my song"
    stripped = _BRACKETS.sub("", t).strip(" -")
    if stripped:
        t = stripped
    t = re.sub(r"\s+", " ", speech.clean_for_speech(t, max_chars=200)).strip().rstrip(".")
    if not re.sub(r"[\W_]+", "", _TITLE_NOISE.sub("", t)):
        return "this one"                         # nothing left but "(Official Video)" etc.
    if len(t) > MAX_TITLE_CHARS:
        cut = t[:MAX_TITLE_CHARS].rsplit(" ", 1)[0]
        t = cut or t[:MAX_TITLE_CHARS]
    return t


def speakable_time(seconds: float) -> str:
    """A position as words: "45 seconds", "1 minute 30", "1 hour 2 minutes". Pure."""
    total = max(0, int(seconds))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h} hour{'s' if h != 1 else ''}" + (f" {m} minute{'s' if m != 1 else ''}" if m else "")
    if m:
        return f"{m} minute{'s' if m != 1 else ''}" + (f" {s}" if s else "")
    return f"{s} second{'s' if s != 1 else ''}"


def count_of(n: int, thing: str) -> str:
    return f"{n} {thing}{'' if n == 1 else 's'}"


# ---- Confirmations --------------------------------------------------------------------------------
# {t} = speakable title, {n} = a count, {pos} = queue position, {at} = a spoken time,
# {level} = volume percent, {name} = playlist name. Every phrasing must make sense on its own.
LINES = {
    "play_now": ["Now playing {t}.", "Here's {t}.", "Starting {t}."],
    "queued": ["Added {t}. It's number {pos} in the queue.", "{t} is queued, number {pos}.",
               "Got it, {t} is number {pos} in line."],
    "playlist": ["Queued {n} from {name}.", "Added {n} from {name}.", "{name} is queued, {n}."],
    "skip": ["Skipping {t}.", "Skipped.", "Moving on."],
    "previous": ["Going back to {t}.", "Back to {t}.", "Playing {t} again."],
    "pause": ["Paused.", "Pausing the music.", "Music's paused."],
    "resume": ["Resuming.", "And we're back.", "Picking up where we left off."],
    "stop": ["Stopped, and the queue's cleared.", "All stopped.", "Music's off, queue cleared."],
    "shuffle": ["Shuffled {n}.", "Queue shuffled.", "Mixed up {n}."],
    "loop_off": ["Loop off.", "No more repeating.", "Looping's off."],
    "loop_track": ["I'll repeat this song.", "Looping this track.", "This one's on repeat."],
    "loop_queue": ["Looping the whole queue.", "I'll repeat the queue.", "The queue's on repeat."],
    "seek": ["Jumping to {at}.", "Skipping to {at}.", "Moving to {at}."],
    "replay": ["Starting {t} over.", "From the top.", "Playing {t} from the start."],
    "remove": ["Removed {t} from the queue.", "Took {t} out.", "{t} is off the queue."],
    "skipto": ["Skipping ahead to {t}.", "Jumping to {t}.", "On to {t}."],
    "clearqueue": ["Cleared {n} from the queue.", "Queue cleared.", "Emptied the queue."],
    "volume": ["Volume {level} percent.", "Setting the volume to {level} percent.",
               "Volume's at {level} percent."],
    "join": ["Hi everyone!", "Hello! I'm here.", "Hey, good to be here."],
    "leave": ["Bye for now!", "See you later!", "Goodbye, everyone."],
    "foreign": ["I've answered that one in the chat.", "My reply's in the chat.",
                "Check the chat for that one."],
}


def confirmation(event: str, values: Optional[Mapping[str, object]] = None,
                 rng: Optional[random.Random] = None) -> str:
    """
    One spoken line for `event`, phrasing picked at random. Pure apart from the RNG, which
    tests pass in seeded. Unknown events raise KeyError - every caller names a fixed event.
    """
    choices = LINES[event]
    line = (rng or random).choice(choices)
    return line.format(**(values or {}))


# ---- Chat replies --------------------------------------------------------------------------------
# A reply up to this long (after clean-up) is read as written; longer ones are paraphrased.
SHORT_REPLY_CHARS = 200
# A paraphrase longer than this, or one that isn't usable speech, is replaced by the fallback.
MAX_PARAPHRASE_CHARS = 300
REST_IN_CHAT = ["The rest is in the chat.", "There's more in the chat.",
                "I've put the details in the chat."]

# How long to wait for a paraphrase before using the fallback. Measured on qwen3.5:2b with
# the chat model warm: 2.2-3.3s for four long replies.
PARAPHRASE_TIMEOUT = 6.0

# The framing matters. A first version ("turn a chat answer into what you'd say") made the
# model answer the text as if the user had written it ("You're right about lists being
# mutable, though I prefer tuples"), and flipped a fact in another ("rain coming Thursday"
# when the answer said Thursday was the dry day). Telling it the text is its OWN answer and
# to keep every fact fixed both: four of four faithful on the same replies.
PARAPHRASE_SYSTEM = (
    "You are Amy. Below is an answer YOU already wrote in a chat. Say the same answer out "
    "loud, shorter: one or two spoken sentences, at most 40 words. Keep every fact exactly "
    "as written - same numbers, same days, same yes or no. Do not reply to it, do not add "
    "opinions or questions, do not change its meaning. No lists, markdown, links or emoji.")


def paraphrase_request(cleaned: str) -> str:
    """The user message for the paraphrase call: the answer, fenced off as Amy's own. Pure."""
    fence = '"""'
    return f"My written answer:\n{fence}\n{cleaned}\n{fence}\nSay it out loud, shorter."


def plan_reply(reply: str, rng: Optional[random.Random] = None) -> Tuple[str, str]:
    """
    How to speak a chat reply. Returns (kind, text):
      ("nothing", "")       - nothing speakable in it
      ("foreign", line)     - not English: a short English line instead (D6)
      ("read", text)        - short enough to read as written
      ("paraphrase", text)  - too long: `text` is the cleaned reply to paraphrase
    """
    cleaned = speech.clean_for_speech(reply, max_chars=10_000)
    if not cleaned:
        return "nothing", ""
    if not speech.is_english(cleaned):
        return "foreign", confirmation("foreign", rng=rng)
    if len(cleaned) <= SHORT_REPLY_CHARS:
        return "read", cleaned
    return "paraphrase", cleaned


def fallback_summary(cleaned: str, rng: Optional[random.Random] = None) -> str:
    """
    When paraphrasing is slow or fails: the opening sentences, up to about SHORT_REPLY_CHARS,
    then a pointer to the chat. Always at least one sentence.
    """
    taken = []
    for sentence in speech.split_sentences(cleaned):
        if taken and len(" ".join(taken + [sentence])) > SHORT_REPLY_CHARS:
            break
        taken.append(sentence)
    head = speech.clean_for_speech(" ".join(taken), max_chars=SHORT_REPLY_CHARS)
    return f"{head} {(rng or random).choice(REST_IN_CHAT)}"


def judge_paraphrase(text: Optional[str]) -> Optional[str]:
    """The model's paraphrase if it's usable speech, else None (use the fallback). Pure."""
    if not text:
        return None
    cleaned = speech.clean_for_speech(llm.strip_think_tags(text), max_chars=10_000)
    if not cleaned or len(cleaned) > MAX_PARAPHRASE_CHARS:
        return None
    if not speech.is_english(cleaned) or llm.looks_like_reasoning(cleaned):
        return None
    return cleaned
