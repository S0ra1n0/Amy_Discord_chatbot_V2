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

# ---- Streaming a chat reply: the decisions between Ollama and Discord -------------------
from datetime import datetime

LIM = llm.STREAM_CHUNK_LIMIT
assert LIM == 1990

# Overflow planning: nothing until the uncommitted text is OVER the limit.
assert llm.plan_overflow("", 0) == []
assert llm.plan_overflow("x" * LIM, 0) == [], "exactly at the limit still fits"
assert llm.plan_overflow("x" * (LIM + 1), 0) == ["x" * LIM], "one over splits off a full page"
_two = llm.plan_overflow("x" * (LIM * 2 + 5), 0)
assert _two == ["x" * LIM, "x" * LIM], "two pages finalized, 5 characters stay pending: %r" % [len(p) for p in _two]
# committed_len moves the window: only the uncommitted tail counts.
assert llm.plan_overflow("x" * (LIM + 500), 500) == []
assert llm.plan_overflow("x" * (LIM + 501), 500) == ["x" * LIM]
# Splits prefer a boundary, and the pieces always rebuild the original text.
_text = ("word " * 900).strip()
_pieces = llm.plan_overflow(_text, 0)
assert _pieces and all(len(p) <= LIM for p in _pieces)
assert "".join(_pieces) + _text[sum(map(len, _pieces)):] == _text
assert _pieces[0].endswith(" "), "split on a space, not mid-word"
assert llm.find_split_index("hello world", 2000) == 11
assert llm.find_split_index("x" * 2500, 1990) == 1990
print("plan_overflow: OK (exactly at limit, one over, multi-page, committed window)")

# Live and final message text.
assert llm.live_message_text("", 0) == "⏳ Thinking..."
assert llm.live_message_text("Hi", 0) == "Hi ▌"
assert llm.live_message_text("Hi there", 3) == "there ▌"
assert llm.live_message_text("Hi", 2) == "⏳ Thinking...", "nothing uncommitted yet"
assert llm.final_message_text("Hi there", 0) == "Hi there"
assert llm.final_message_text("Hi there", 3) == "there"
assert llm.final_message_text("Hi", 2) == "✅ Done.", "everything already committed"

# Think tags split across chunks: the display never shows a half-open block or a stray tag.
_acc = ""
_seen = []
for _chunk in ["<th", "ink>plan", " it</thi", "nk>Hel", "lo"]:
    _acc += _chunk
    _seen.append(llm.get_display_text(_acc))
assert _seen[0] == "<th", "a tag that has not finished yet is just text"
assert _seen[1] == _seen[2] == "", "an open think block shows nothing: %r" % _seen
assert _seen[3] == "Hel" and _seen[4] == "Hello", _seen
assert llm.final_reply(_acc, "stop") == ("Hello", False)
print("think tags across chunks: OK")

# Empty answers: explain the cause, and only the token ceiling is flagged.
assert llm.final_reply("", "length") == (llm.OUT_OF_ROOM_MESSAGE, True)
assert llm.final_reply("<think>all of it</think>", "length") == (llm.OUT_OF_ROOM_MESSAGE, True)
assert llm.final_reply("", "stop") == (llm.NO_ANSWER_MESSAGE, False)
assert llm.final_reply("  ", None) == (llm.NO_ANSWER_MESSAGE, False)
assert llm.final_reply("Four.", "length") == ("Four.", False), "an answer is never replaced"
# A reply cut off inside an open <think> block: raw reasoning must never come out.
assert llm.strip_think_tags("<think>reasoning") == "", "FAIL open think block must be dropped"
assert llm.strip_think_tags("Hi<think>reasoning") == "Hi", "FAIL open trailing think block"
assert llm.final_reply("<think>reasoning", "length") == (llm.OUT_OF_ROOM_MESSAGE, True), "FAIL open think + length"
assert llm.final_reply("<think>reasoning", "stop") == (llm.NO_ANSWER_MESSAGE, False), "FAIL open think + stop"
assert llm.final_reply("<think>a</think>Answer<think>more", "length") == ("Answer", False), "FAIL answer then open think"
assert "ran out of room" in llm.OUT_OF_ROOM_MESSAGE and "different way" in llm.NO_ANSWER_MESSAGE
assert "trouble connecting" in llm.CHAT_ERROR_MESSAGE
print("final_reply: OK (length vs stop, think-only output, real answer kept)")

# Tool-call round: which call, which query, what the second pass is sent.
_call = {"function": {"name": "web_search", "arguments": {"query": "weather in Hue"}}}
assert llm.tool_round_call([], False) is None
assert llm.tool_round_call([_call], True) is None, "a failed pass never searches"
assert llm.tool_round_call([_call, {"function": {}}], False) is _call, "first call only"
assert llm.tool_call_query(_call, "ask") == "weather in Hue"
assert llm.tool_call_query({"function": {"arguments": {}}}, "ask") == "ask"
assert llm.tool_call_query({"function": {}}, "ask") == "ask"
assert llm.tool_call_query({"function": {"arguments": "garbage"}}, "ask") == "ask"
_base = [{"role": "system", "content": "s"}, {"role": "user", "content": "q"}]
_second = llm.tool_round_messages(_base, _call, "web_search", "RESULTS")
assert _base == [{"role": "system", "content": "s"}, {"role": "user", "content": "q"}], "input untouched"
assert _second[:2] == _base
assert _second[2] == {"role": "assistant", "content": "", "tool_calls": [_call]}
assert _second[3] == {"role": "tool", "tool_name": "web_search", "content": "RESULTS"}, \
    "the result must carry tool_name or the model ignores it"
print("tool round helpers: OK")

_sys = llm.system_with_time("PROMPT", datetime(2026, 10, 29, 13, 5, 9))
assert _sys.startswith("PROMPT\n\n[IMPORTANT] Current date and time: 2026-10-29 13:05:09 (Thursday). ")
assert _sys.endswith("when answering questions about time.")
print("system_with_time: OK")

print()
print("ALL LLM TESTS PASSED")
