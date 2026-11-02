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
from datetime import datetime
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

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
    """
    Remove <think>...</think> blocks emitted by qwen3 models, and a trailing block that was
    never closed (generation stopped at the token limit mid-reasoning), so raw reasoning can
    never be posted, stored or spoken.
    """
    text = re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL)
    text = re.sub(r'<think>.*$', '', text, flags=re.DOTALL)
    return text.strip()


def get_display_text(accumulated: str) -> str:
    """
    Return displayable text from a partial streaming accumulation.
    Hides complete think blocks and also hides any incomplete (still-open) think block,
    so the user sees nothing while qwen3 is reasoning and only sees the reply after </think>.
    """
    return strip_think_tags(accumulated)


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


# ---- Streaming a chat reply ---------------------------------------------------------------
# The decisions chat_streaming makes between Ollama and Discord, as pure functions. The thread,
# the queue and every Discord call stay in the bot.
STREAM_CHUNK_LIMIT: int = 1990   # Discord's 2000, minus room for the streaming cursor suffix

CHAT_ERROR_MESSAGE = (
    "I apologize, but I'm currently having trouble connecting to my knowledge system. "
    "Please try again in a moment."
)
OUT_OF_ROOM_MESSAGE = (
    "I ran out of room before I could finish that answer. "
    "Try asking for something shorter, or ask an admin to raise my limit."
)
NO_ANSWER_MESSAGE = (
    "I apologize, but I couldn't produce an answer for that one. "
    "Could you try asking a different way?"
)


def find_split_index(text: str, limit: int) -> int:
    """Find an index <= limit to split text at, preferring a newline or space boundary."""
    if len(text) <= limit:
        return len(text)
    split_at = text.rfind('\n', 0, limit)
    if split_at == -1:
        split_at = text.rfind(' ', 0, limit)
    if split_at <= 0:
        return limit
    return split_at + 1


def system_with_time(system_prompt: str, now: datetime) -> str:
    """The system prompt with the current date and time appended, so she can answer about it."""
    return (
        f"{system_prompt}\n\n[IMPORTANT] Current date and time: "
        f"{now.strftime('%Y-%m-%d %H:%M:%S')} ({now.strftime('%A')}). "
        "Use this information when answering questions about time."
    )


def plan_overflow(display: str, committed_len: int,
                  limit: int = STREAM_CHUNK_LIMIT) -> List[str]:
    """
    The pieces to finalize into their own Discord messages, in order, because the text not yet
    committed (display[committed_len:]) is longer than limit. Empty while it still fits. The
    caller adds len(piece) to committed_len for each piece it finalizes.
    """
    pieces: List[str] = []
    pending = display[committed_len:]
    while len(pending) > limit:
        idx = find_split_index(pending, limit)
        pieces.append(pending[:idx])
        pending = pending[idx:]
    return pieces


def live_message_text(display: str, committed_len: int) -> str:
    """What the live message shows mid-stream: the uncommitted text and a cursor."""
    pending = display[committed_len:]
    return (pending + " ▌") if pending else "⏳ Thinking..."


def final_message_text(final_response: str, committed_len: int) -> str:
    """What the last message holds once the stream is over."""
    pending = final_response[committed_len:]
    return pending if pending else "✅ Done."


def tool_round_call(tool_calls: List[Any], errored: bool) -> Optional[Any]:
    """The one tool call to run after a pass (no tool loops), or None for a plain reply."""
    if errored or not tool_calls:
        return None
    return tool_calls[0]


def tool_call_query(call: Any, user_message: str) -> str:
    """The search query a tool call asks for, falling back to the user's own message."""
    args = call["function"].get("arguments") or {}
    if not isinstance(args, dict):
        args = {}
    return args.get("query") or user_message


def tool_round_messages(messages: List[Dict[str, Any]], call: Any, tool_name: str,
                        result_text: str) -> List[Dict[str, Any]]:
    """The conversation for the second pass: the call, then its result tied back by tool_name."""
    return messages + [
        {"role": "assistant", "content": "", "tool_calls": [call]},
        {"role": "tool", "tool_name": tool_name, "content": result_text},
    ]


def should_chat(is_system: bool, author_is_bot: bool, content: str) -> bool:
    """
    True when a message is conversation Amy should answer. Discord's own system messages
    ("X joined the server", pins, boosts), anything from a bot (no bot-to-bot loops) and
    messages with no text (stickers, images, attachments only) are not.
    """
    if is_system or author_is_bot:
        return False
    return bool((content or "").strip())


def is_addressed(is_dm: bool, mentioned_user_ids: Iterable[int], mentioned_role_ids: Iterable[int],
                 replied_to_author_id: Optional[int], my_id: int, my_role_id: Optional[int]) -> bool:
    """
    True when a message is meant for Amy: a DM, a mention of her user or of her own bot role, or
    a Discord reply to one of her messages. @everyone / @here and mentions of other people are
    not (the caller passes only explicit user and role ids).
    """
    if is_dm:
        return True
    if my_id in set(mentioned_user_ids):
        return True
    if my_role_id is not None and my_role_id in set(mentioned_role_ids):
        return True
    return replied_to_author_id is not None and replied_to_author_id == my_id


_USER_MENTION = re.compile(r"<@!?(\d+)>")
_ROLE_MENTION = re.compile(r"<@&(\d+)>")
_CHANNEL_MENTION = re.compile(r"<#(\d+)>")
# Same, with the horizontal whitespace around it: groups 1 and 4 are the spaces, 3 is the id
_OWN_USER = re.compile(r"([ \t]*)(<@!?(\d+)>)([ \t]*)")
_OWN_ROLE = re.compile(r"([ \t]*)(<@&(\d+)>)([ \t]*)")


def chat_text(content: str, my_id: int, my_role_id: Optional[int],
              user_name: Callable[[int], Optional[str]], role_name: Callable[[int], Optional[str]],
              channel_name: Callable[[int], Optional[str]]) -> str:
    """
    The message as the model should see it. Amy's own mentions (user and role, matched by id)
    are removed; other mentions become readable (`@Name`, `@role`, `#channel`, via the
    resolvers, which return None for unknown ids). Other whitespace, including the indentation of
    pasted code, is left alone; only the ends are stripped. Empty when nothing but a tag was left.
    """
    # Whitespace is touched only around her own tag (pasted code keeps its indentation): the tag
    # goes with the spaces/tabs beside it, leaving one space only if it sat between two words.
    def drop_own(m: "re.Match[str]") -> str:
        if int(m.group(3)) != my_id:
            return m.group(0)
        return " " if m.group(1) and m.group(4) else ""

    def drop_own_role(m: "re.Match[str]") -> str:
        if my_role_id is None or int(m.group(3)) != my_role_id:
            return m.group(0)
        return " " if m.group(1) and m.group(4) else ""

    text = _OWN_USER.sub(drop_own, content or "")
    text = _OWN_ROLE.sub(drop_own_role, text)
    text = _USER_MENTION.sub(lambda m: "@" + (user_name(int(m.group(1))) or "deleted-user"), text)
    text = _ROLE_MENTION.sub(lambda m: "@" + (role_name(int(m.group(1))) or "deleted-role"), text)
    text = _CHANNEL_MENTION.sub(lambda m: "#" + (channel_name(int(m.group(1))) or "deleted-channel"), text)
    return text.strip()


def final_reply(accumulated: str, done_reason: Optional[str]) -> Tuple[str, bool]:
    """
    The reply to store and show, and whether the model hit its token limit. Empty output almost
    always means the ceiling rather than having nothing to say, so that case says so instead of
    telling the user to rephrase.
    """
    text = strip_think_tags(accumulated)
    if text:
        return text, False
    if done_reason == "length":
        return OUT_OF_ROOM_MESSAGE, True
    return NO_ANSWER_MESSAGE, False
