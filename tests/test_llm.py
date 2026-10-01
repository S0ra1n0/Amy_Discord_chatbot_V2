"""
The pure model helpers in llm.py, tested by importing that module alone.

No Discord client, no database, no network - which is the point of llm.py existing. These
used to be reachable only by importing the whole bot, which builds a client and opens its
database; that coupling is how a test once came close to overwriting a real saved setting.
"""
import io
import os
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJ)

import llm

# Importing llm must not drag the bot in with it.
assert "discord" not in sys.modules and "ollama" not in sys.modules, \
    "llm.py must stay free of Discord and Ollama: %s" % sorted(
        m for m in ("discord", "ollama") if m in sys.modules)
print("llm imports standalone: OK (no discord, no ollama)")

# ---- Reasoning modes -------------------------------------------------------------------
# The right `think` setting is NOT the same for every model. qwen3.5:2b needs think=False
# (2.3s, clean); the identical setting makes qwen3:4b write 3,800 characters of its own
# reasoning into the reply. /model probes and records the right mode per model.
assert llm.think_value("false") is False
assert llm.think_value("true") is True
assert llm.think_value("auto") is None, "auto must omit the argument, which ollama spells None"
for junk in ("banana", "", None, 5, "  AUTO  "):
    got = llm.think_value(junk)
    assert got in (False, True, None), "%r -> %r" % (junk, got)
assert llm.think_value("  AUTO  ") is None, "whitespace and case must not matter"

assert llm.normalise_think_mode("1") == "true"
assert llm.normalise_think_mode("off") == "false"
assert llm.normalise_think_mode("nonsense") == "false", "unknown falls back"
assert llm.normalise_think_mode("nonsense", fallback="auto") == "auto"
assert llm.normalise_think_mode("auto") == "auto"
assert llm.normalise_think_mode(None) == "false", "an unset OLLAMA_THINK reads as false"
assert llm.normalise_think_mode("  True ") == "true"
assert set(llm.THINK_MODES) == {"false", "true", "auto"}
print("think modes: OK (false/true/auto, junk falls back)")

# ---- Detecting a model that narrates its own reasoning ---------------------------------
# The detector decides whether /model switches a model's mode, so both directions matter.
LEAKS = [
    'Hmm, the user is asking "What is 2 + 2?" and wants the answer in one short sentence.',
    "Okay, the user wants a brief explanation of the difference between lists and tuples.",
    "Okay, let's see. I need to figure out what 17 times 23 is.",
    "The user wants a two-line birthday message for their friend Minh.",
]
ANSWERS = [
    "In Python, **lists** and **tuples** are both ordered collections.",
    "2 plus 2 equals 4.",
    "We are comparing two data structures in Python: lists and tuples.",
    "Happy Birthday, Minh! Wishing you a day filled with laughter.",
    "Hello there! It is my pleasure to assist you with your programming endeavors.",
    "Let me know if you'd like me to explain any part in more detail.",
    "",
]
for t in LEAKS:
    assert llm.looks_like_reasoning(t), "missed a real leak: %r" % t[:60]
for t in ANSWERS:
    assert not llm.looks_like_reasoning(t), "false positive on a real answer: %r" % t[:60]
print("looks_like_reasoning: OK (%d leaks caught, %d answers cleared)" % (len(LEAKS), len(ANSWERS)))

# ---- Judging one /model probe reply ----------------------------------------------------
LEAK = "Okay, the user wants to know what 2 + 2 is. Let me think about that."
judge = llm.judge_probe_reply
assert judge("2 + 2 = 4.", "") is True, "a clean answer is usable"
assert judge(LEAK, "") is False, "reasoning pasted into the reply is not"
assert judge("", "the model reasoned here") is True, \
    "empty content with separate reasoning: the budget ran out, but reasoning stays out of chat"
assert judge("", "") is False, "nothing at all is not usable"
assert judge(LEAK, "and some separate thinking") is False, "a leak is a leak either way"
print("judge_probe_reply: OK")

# ---- Choosing a model at startup -------------------------------------------------------
# A failed load used to be read as "the saved model was uninstalled": Ollama simply not
# being up yet at login was enough to switch to the default and erase the admin's choice.
pick = llm.startup_model_choice
D = "default:1"
assert pick(D, D, loaded=False, installed=None) == D, "the default is always fine"
assert pick("big:7b", D, loaded=True, installed=None) == "big:7b", "it loaded - keep it"
assert pick("big:7b", D, loaded=False, installed=None) == "big:7b", \
    "Ollama unreachable proves nothing about the model - keep the choice"
assert pick("big:7b", D, loaded=False, installed=["big:7b", D]) == "big:7b", \
    "installed but failed to load (VRAM, timeout) is transient - keep the choice"
assert pick("big:7b", D, loaded=False, installed=[D]) == D, \
    "only a model Ollama confirms is gone falls back"
assert pick("big:7b", D, loaded=False, installed=[]) == D
print("startup_model_choice: OK (falls back only when the model is confirmed gone)")

# ---- Reading what the model sends back -------------------------------------------------
assert llm.strip_think_tags("<think>hmm</think>Answer") == "Answer"
assert llm.strip_think_tags("no tags") == "no tags"
assert llm.get_display_text("<think>still going") == "", "an open think block shows nothing"
assert llm.get_display_text("<think>done</think>Hi") == "Hi"
assert llm.extract_model_names({"models": [{"model": "a:1"}, {"name": "b:2"}]}) == ["a:1", "b:2"]
assert llm.extract_model_names({"models": []}) == []
assert llm.extract_model_names({}) == []


class _ObjModel:
    def __init__(self, name):
        self.model = name


class _ObjList:
    models = [_ObjModel("c:3")]


assert llm.extract_model_names(_ObjList()) == ["c:3"], "object-shaped responses (ollama>=0.4)"
print("think tags and model listings: OK")

# ---- Assembling the system prompt ------------------------------------------------------
# It used to claim a working web_search tool unconditionally; with search off, Amy then
# answered current-events questions from memory 9 of 9 times without saying she couldn't.
BASE = "You are a test persona.\n\nRemember: You are here to help."
on = llm.build_system_prompt(BASE, True)
off = llm.build_system_prompt(BASE, False)
assert "working web_search" in on
assert "working web_search" not in off, "must not promise a tool that isn't offered"
assert "Never claim to have searched" in off
assert on.count("Knowledge:") == 1 and off.count("Knowledge:") == 1
for prompt in (on, off):
    assert prompt.startswith("You are a test persona."), "the opening must stay put"
    assert prompt.rstrip().endswith("Remember: You are here to help."), \
        "the closing line must stay last, after the knowledge block"
    assert prompt.index("Knowledge:") < prompt.index("Remember:")
print("build_system_prompt: OK (claims match the tools offered)")

print()
print("ALL LLM TESTS PASSED")
