"""
voicelines.py on its own: every spoken confirmation, speakable titles and times, and how a
chat reply becomes speech (read / paraphrase / fallback / not English).
"""
import io
import os
import random
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJ)

import voicelines as V
import speech
from _check import check, finish

print("=== importing voicelines pulls in nothing heavy ===")
heavy = [m for m in ("discord", "ollama", "torch", "kokoro") if m in sys.modules]
check("no discord/ollama/torch/kokoro", not heavy, heavy)

print()
print("=== every phrasing of every event is clean, speakable English ===")
SAMPLE = {"t": "Never Gonna Give You Up", "pos": 3, "n": "12 songs", "name": "Chill Mix",
          "at": "1 minute 30", "level": 40}
bad = []
for event, lines in V.LINES.items():
    if len(lines) < 2:
        bad.append((event, "fewer than 2 phrasings"))
    for line in lines:
        try:
            text = line.format(**SAMPLE)
        except (KeyError, IndexError) as e:
            bad.append((event, "unknown placeholder %s" % e))
            continue
        if speech.clean_for_speech(text) != text:
            bad.append((event, "changes when cleaned: %r" % text))
        if not speech.is_english(text):
            bad.append((event, "not English: %r" % text))
        if text[-1] not in ".!?":
            bad.append((event, "no closing punctuation: %r" % text))
check("all %d events, every phrasing" % len(V.LINES), not bad, bad)
for needed in ("play_now", "queued", "playlist", "skip", "previous", "pause", "resume", "stop",
               "shuffle", "loop_off", "loop_track", "loop_queue", "seek", "replay", "remove",
               "skipto", "clearqueue", "volume", "join", "leave", "foreign", "up_next"):
    check("has a line for %s" % needed, needed in V.LINES)
check("up_next is calm: no '!' and every phrasing names the song",
      all("!" not in l and "{t}" in l for l in V.LINES["up_next"]), V.LINES["up_next"])
check("up_next formats with a title",
      V.confirmation("up_next", {"t": "Song X"}, random.Random(0)).count("Song X") == 1)

print()
print("=== should_announce_next: only when a song ends by itself and a different one starts ===")
PREV, NXT = object(), object()
base = dict(force_next=False, going_back=False, resumed=False)
check("natural end, different track -> announce", V.should_announce_next(PREV, NXT, **base) is True)
check("no previous track (the first one) -> no",
      V.should_announce_next(None, NXT, **base) is False)
check("nothing next -> no", V.should_announce_next(PREV, None, **base) is False)
check("the same track again (TRACK loop) -> no", V.should_announce_next(PREV, PREV, **base) is False)
for flag in ("force_next", "going_back", "resumed"):
    check("%s flips it off" % flag,
          V.should_announce_next(PREV, NXT, **{**base, flag: True}) is False)

rng = random.Random(7)
seen = {V.confirmation("pause", rng=rng) for _ in range(200)}
check("phrasings vary (every one gets used)", seen == set(V.LINES["pause"]), seen)
check("a seeded RNG is reproducible",
      V.confirmation("skip", {"t": "X"}, random.Random(1)) ==
      V.confirmation("skip", {"t": "X"}, random.Random(1)))
check("values fill the placeholders",
      "Song" in V.confirmation("queued", {"t": "Song", "pos": 2}, random.Random(0)))

print()
print("=== speakable_title ===")
for raw, want in [
    ("Rick Astley - Never Gonna Give You Up (Official Video) (4K Remaster)",
     "Rick Astley - Never Gonna Give You Up"),
    ("Lofi Hip Hop Radio | beats to relax to", "Lofi Hip Hop Radio"),
    ("Song (feat. Someone) [Lyrics]", "Song"),
    ("my_song.mp3", "my song"),
    ("(Official Video)", "this one"),
    ("Official Music Video", "this one"),
    ("", "this one"),
    ("Chill **mix** https://youtu.be/x", "Chill mix a link"),
]:
    check("%r -> %r" % (raw[:40], want), V.speakable_title(raw) == want, V.speakable_title(raw))
long = V.speakable_title("word " * 40)
check("long titles stop at a word, within the cap",
      len(long) <= V.MAX_TITLE_CHARS and not long.endswith(" "), long)

print()
print("=== speakable_time / count_of ===")
for secs, want in [(0, "0 seconds"), (1, "1 second"), (45, "45 seconds"), (60, "1 minute"),
                   (90, "1 minute 30"), (125, "2 minutes 5"), (3600, "1 hour"),
                   (3723, "1 hour 2 minutes"), (-5, "0 seconds")]:
    check("%s -> %r" % (secs, want), V.speakable_time(secs) == want, V.speakable_time(secs))
check("1 song / 2 songs", V.count_of(1, "song") == "1 song" and V.count_of(2, "song") == "2 songs")

print()
print("=== plan_reply ===")
check("a reply that's only code -> the short code line",
      V.plan_reply("```\nx = 1\n```") == ("read", speech.CODE_PLACEHOLDER), V.plan_reply("```\nx = 1\n```"))
check("emoji only -> nothing", V.plan_reply("\U0001F389✅") == ("nothing", ""))
kind, line = V.plan_reply("Hôm nay trời đẹp quá, bạn khỏe không?",
                          random.Random(0))
check("Vietnamese -> a short English line instead", kind == "foreign" and line in V.LINES["foreign"],
      (kind, line))
check("a short reply is read as written, cleaned and calmed",
      V.plan_reply("Sure! The answer is **4**.") == ("read", "Sure. The answer is 4."))
edge = "a" * (V.SHORT_REPLY_CHARS - 1) + "."
check("exactly at the limit is still read", V.plan_reply(edge)[0] == "read")
longer = " ".join("This is sentence %d of a long answer." % i for i in range(20))
kind, text = V.plan_reply(longer)
check("a long reply is paraphrased", kind == "paraphrase")
check("...and the whole cleaned reply goes to the paraphrase, uncut", len(text) > 400, len(text))

print()
print("=== calm_reply (what is spoken; the written reply is untouched) ===")
for filler in ("How about you?", "How is your day going?", "How are you doing today?",
               "Is there anything else I can help you with?", "Is there anything I can assist with?",
               "Is there anything else?", "Can I help you with anything else?",
               "How can I help you today?", "Let me know if you need anything else.",
               "I'm at your service.", "And you?", "How's your week been so far?"):
    got = V.calm_reply("It has been fine. " + filler)
    check("trailing %r is dropped" % filler, got == "It has been fine.", got)
check("two fillers in a row both go", V.calm_reply(
    "Fine, thank you. How about you? Is there anything else I can help with?") == "Fine, thank you.")
check("the real reply that prompted this",
      V.calm_reply("It has been fine. Thank you for asking. How is your day going? "
                   "Is there anything I can assist with?") == "It has been fine. Thank you for asking.")
mid = "Is it the red one? Then it costs 5 dollars. It ships Monday."
check("a real question in the middle is kept", V.calm_reply(mid) == mid)
check("a real trailing question is kept",
      V.calm_reply("I found two. Which one do you want?") == "I found two. Which one do you want?")
check("a non-filler 'anything' question is kept",
      V.calm_reply("Done. Is there anything in the oven?") == "Done. Is there anything in the oven?")
check("a reply that is only a question is kept", V.calm_reply("How about you?") == "How about you?")
check("only fillers -> the first sentence survives, never empty",
      V.calm_reply("Is there anything else? How can I help?") == "Is there anything else?")
for keep in ("How is your setup?", "How was your build?", "Let me know if you have any errors when you run it.",
             "Let me know if you need the file."):
    got = V.calm_reply("Run it now. " + keep)
    check("genuine follow-up %r is kept" % keep, got == "Run it now. " + keep, got)
check("'Let me know if you need anything else.' still goes",
      V.calm_reply("Run it now. Let me know if you have any other questions.") == "Run it now.")
check("empty stays empty without crashing", V.calm_reply("") == "")
check("a spoken '!=' is not turned into '.='", V.calm_reply("Use a != b here!") == "Use a != b here.")
check("'!' becomes '.'", V.calm_reply("Hello! It is going well!") == "Hello. It is going well.")
check("'?!' becomes '?'", V.calm_reply("Really?! Yes.") == "Really? Yes.")
check("'!!!' becomes one '.'", V.calm_reply("Great!!! Next.") == "Great. Next.")

print()
print("=== plan_reply calms the CLEANED text (markdown must be stripped first) ===")
md = "Here are the steps.\n- Open settings.\n> Note: it restarts.\n\nIs there anything else I can help with?"
check("bullets and quotes lose their markers, trailing filler goes",
      V.plan_reply(md) == ("read", "Here are the steps. Open settings. Note: it restarts."), V.plan_reply(md))
check("a heading line is not left as '##'",
      V.plan_reply("Done.\n## Next steps\nRun it") == ("read", "Done. Next steps. Run it."),
      V.plan_reply("Done.\n## Next steps\nRun it"))
check("inline code `a != b` keeps its '!' (cleaned as before, not 'a.= b')",
      V.plan_reply("Use `a != b` here.") == ("read", "Use a!= b here."), V.plan_reply("Use `a != b` here."))

print()
print("=== fallback_summary ===")
fb = V.fallback_summary(longer, random.Random(0))
check("starts with the opening sentence", fb.startswith("This is sentence 0 of a long answer."))
check("ends by pointing to the chat", any(fb.endswith(r) for r in V.REST_IN_CHAT), fb[-40:])
check("stays short", len(fb) <= V.SHORT_REPLY_CHARS + 40, len(fb))
one_long = "word " * 120 + "end."
fb = V.fallback_summary(one_long, random.Random(0))
check("one huge sentence is still capped", len(fb) <= V.SHORT_REPLY_CHARS + 40, len(fb))

print()
print("=== judge_paraphrase ===")
check("None -> fallback", V.judge_paraphrase(None) is None)
check("empty -> fallback", V.judge_paraphrase("   ") is None)
check("a good paraphrase is kept, cleaned",
      V.judge_paraphrase("The Great Wall is about **21,196** km long.") ==
      "The Great Wall is about 21,196 km long.")
check("the paraphrase is calmed: '!' and a closing offer go",
      V.judge_paraphrase("Sure! It ships Monday! Anything else I can help with?") == "Sure. It ships Monday.")
check("think tags are stripped", V.judge_paraphrase("<think>hmm</think>It's four.") == "It's four.")
check("leaked reasoning -> fallback",
      V.judge_paraphrase("Okay, the user wants me to shorten this. Let me think.") is None)
check("too long -> fallback", V.judge_paraphrase("word " * 100) is None)
check("not English -> fallback",
      V.judge_paraphrase("Hôm nay trời đẹp quá, bạn khỏe không?") is None)

print()
print("=== the paraphrase prompt (load-bearing, measured) ===")
# A first version made the model answer the text as if the user had written it, and flip a
# fact. These cues fixed both (4/4 faithful); changing them needs re-measuring.
for cue in ("YOU already wrote", "Keep every fact exactly", "Do not reply to it",
            "do not change its meaning", "at most 40 words"):
    check("system prompt says %r" % cue, cue in V.PARAPHRASE_SYSTEM)
req = V.paraphrase_request("It's four.")
check("the answer is fenced off as Amy's own",
      req.startswith("My written answer:") and '"""\nIt\'s four.\n"""' in req, req)
check("a sane timeout", 2.0 <= V.PARAPHRASE_TIMEOUT <= 10.0)

finish("ALL VOICELINES TESTS PASSED")
