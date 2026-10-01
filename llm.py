# llm.py
"""
Pure helpers for talking to the language model: reasoning-mode handling, reading what the
model sends back, choosing a model at startup, and assembling the system prompt.

Nothing here touches Discord, the database, or the network, so it all tests offline by
importing this module directly. Amy_chatbot_V2.py owns the state these decisions act on -
the active model, the saved settings - and the calls to Ollama itself.

This used to live in Amy_chatbot_V2.py, where the only way to test a three-line helper was
to import the entire bot, which builds a Discord client and opens its database.
"""

import re
from typing import Any, List, Optional

THINK_MODES = ("false", "true", "auto")


# Openings a reasoning model uses when it narrates its own thinking instead of answering.
# Measured on qwen3:4b with think=False: replies began "Okay, the user wants a brief
# explanation..." and ran to 3,800 characters before reaching anything useful.
_REASONING_OPENERS = (
    "okay, the user", "okay, so the user", "okay, let's", "okay, let me",
    "the user wants", "the user is asking", "the user just", "the user asked",
    "let me recall", "let me think", "let me break", "first, i need to",
    "i need to figure out", "we are to ", "alright, the user",
)


def looks_like_reasoning(text: str) -> bool:
    """
    True when a reply opens with the model narrating its own thought process.

    Used to catch a model that ignores think=False and writes its reasoning into the
    answer. Only the opening is examined - a reply that happens to say "let me think"
    halfway through is just conversational.
    """
    head = (text or "").strip().lower()[:120]
    return any(head.startswith(p) or p in head for p in _REASONING_OPENERS)


def normalise_think_mode(value: Any, fallback: str = "false") -> str:
    """Coerce a stored or configured think mode into one of THINK_MODES."""
    text = str(value or "").strip().lower()
    if text in ("1", "yes", "on"):
        return "true"
    if text in ("0", "no", "off"):
        return "false"
    return text if text in THINK_MODES else fallback


def think_value(mode: str) -> Optional[bool]:
    """
    The value to pass as ollama.chat's `think` argument for a mode.

    "auto" maps to None, which the client treats exactly as omitting the argument -
    verified against qwen3:4b, where both routed reasoning to the separate `thinking`
    field and left the content clean. That field never reaches Discord.
    """
    mode = normalise_think_mode(mode)
    if mode == "auto":
        return None
    return mode == "true"


def strip_think_tags(text: str) -> str:
    """Remove complete <think>...</think> blocks emitted by qwen3 models."""
    return re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL).strip()


def get_display_text(accumulated: str) -> str:
    """
    Return displayable text from a partial streaming accumulation.
    Hides complete think blocks and also hides any incomplete (still-open) think block,
    so the user sees nothing while qwen3 is reasoning and only sees the reply after </think>.
    """
    text = re.sub(r'<think>.*?</think>', '', accumulated, flags=re.DOTALL)
    text = re.sub(r'<think>.*$', '', text, flags=re.DOTALL)
    return text.strip()


def extract_model_names(list_response) -> List[str]:
    """
    Extract model names from ollama.list() output.
    Handles both dict-based (ollama<0.4) and object-based (ollama>=0.4) response shapes.
    """
    if isinstance(list_response, dict):
        models = list_response.get('models', [])
    else:
        models = getattr(list_response, 'models', [])

    names: List[str] = []
    for m in models:
        if isinstance(m, dict):
            name = m.get('name') or m.get('model')
        else:
            name = getattr(m, 'name', None) or getattr(m, 'model', None)
        if name:
            names.append(name)
    return names


# The knowledge section has to match what Amy can actually do. The search tool is only
# offered when WEB_SEARCH is on, so telling her unconditionally that she "has a working
# web_search tool" is a lie in the other configuration - and a lie she acts on: with search
# off she answered current-events questions from memory in 9 of 9 runs without once saying
# she couldn't check.
KNOWLEDGE_WITH_SEARCH = """
Knowledge:
- Your training data is frozen, but you CAN look things up: you have a working web_search
  tool. Never tell the user you have no internet access or cannot see current information.
- For any question about current, recent or time-sensitive information, use the web_search
  tool rather than answering from memory. Treat search results as reference material.
- When search results are present in the conversation, answer from them. Do not preface the
  answer by saying you cannot access real-time data - you just searched, so you can.
- If the results genuinely do not answer the question, say that plainly instead of filling
  the gap from memory."""


KNOWLEDGE_WITHOUT_SEARCH = """
Knowledge:
- Your training data is frozen and you have no way to look anything up right now.
- For questions about news, current events, live scores, weather or anything else that
  changes, say plainly that you cannot check rather than answering from memory.
- Never claim to have searched, looked something up, or checked a source. You cannot."""


def build_system_prompt(base: str, search_enabled: bool) -> str:
    """Assemble the system prompt so its claims match the tools actually on offer."""
    knowledge = KNOWLEDGE_WITH_SEARCH if search_enabled else KNOWLEDGE_WITHOUT_SEARCH
    marker = "\nRemember: You are here"
    head, sep, tail = base.partition(marker)
    return head + knowledge + "\n" + sep + tail


def judge_probe_reply(content: str, thinking: str) -> bool:
    """
    Whether one probe reply shows a reasoning mode that keeps Discord replies clean.

    - The model narrating its own reasoning in the reply: no, whatever else came back.
    - A real answer: yes.
    - No answer, but reasoning in the separate `thinking` field: yes. The probe's small
      token budget ran out before the answer, but reasoning stays out of the chat, which
      is exactly what "auto" mode exists to achieve.
    - Nothing at all: no.

    Kept pure so it can be tested without a live model.
    """
    if content:
        return not looks_like_reasoning(content)
    return bool(thinking)


def startup_model_choice(saved: str, default: str, loaded: bool,
                         installed: Optional[List[str]]) -> str:
    """
    Which model to run with after trying to load the one restored from settings.

    Fall back ONLY when Ollama confirms the saved model is no longer installed. A failed
    load on its own proves nothing - at login Ollama often isn't up yet, and a large model
    can fail to load for lack of VRAM - and the old behaviour of treating any failure as
    "uninstalled" silently erased the admin's /model choice.

    `installed` is None when Ollama couldn't be asked at all.
    """
    if saved == default or loaded or installed is None:
        return saved
    return saved if saved in installed else default
