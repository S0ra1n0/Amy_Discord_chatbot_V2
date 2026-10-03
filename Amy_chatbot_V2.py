# Amy_chatbot_V2.py

#-------Import Libraries----------
import os
import re
import time
import asyncio
import logging
import random
import httpx
from collections import defaultdict
from datetime import datetime
from dataclasses import dataclass
from typing import (Any, Awaitable, Callable, Dict, List, Optional, Tuple,
                    Union)

from dotenv import dotenv_values, load_dotenv
import ollama
import discord
from discord.ext import tasks
from discord import app_commands

from commands_help import HELP_EVERYONE, HELP_ADMIN
from database import (DEFAULT_MAX_MESSAGES, MIN_MAX_MESSAGES, ConversationDB,
                      SchemaTooNewError)
import config
import voice
from voice import VoiceManager
import music
import mixer
from music import LoopMode, MusicManager
import websearch
import llm
import logs
import models
from models import ModelManager
import speech
import voicelines
# Re-exported so the rest of this file - and tests reaching in as amy.<name> - keep working.
from llm import (  # noqa: F401
    _REASONING_OPENERS,
    build_system_prompt,
    extract_model_names,
    get_display_text,
    judge_probe_reply,
    KNOWLEDGE_WITH_SEARCH,
    KNOWLEDGE_WITHOUT_SEARCH,
    looks_like_reasoning,
    normalise_think_mode,
    startup_model_choice,
    strip_think_tags,
    THINK_MODES,
    think_value,
)
import ui
from ui import Reply
#----------------------------------

#----Utility Functions------
MAX_DICE_SIDES: int = 1000     # Guard rails for /dice - the roll runs on the
MAX_DICE_AMOUNT: int = 100     # event loop, so an unbounded amount freezes the bot
MAX_DISCORD_LEN: int = 2000
STREAM_CHUNK_LIMIT: int = 1990  # Leaves room for the streaming cursor suffix

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

#--------------------------------------

#----Setup Discord Bot and Ollama Model------
# Snapshot first: python-dotenv never overrides a variable that already exists, so a
# setting defined both system-wide and in .env silently takes the system value. Report any.
_env_before_dotenv = dict(os.environ)
load_dotenv()
# Console as it always looked, plus a rotating file (logs.py). Set up before anything
# below can warn. AMY_LOG_FILE blank turns the file off; the tests point it at a temp file.
log = logging.getLogger("amy")
_log_problem = logs.setup_logging(os.getenv("AMY_LOG_FILE", "amy.log").strip())
if _log_problem:
    log.warning(_log_problem)
for _name in config.shadowed_settings(_env_before_dotenv, dotenv_values()):
    # Names only - the values may be secrets.
    log.warning(f"{_name} is set both in your system environment and in .env, "
                f"with different values. The system value wins; the .env line is ignored.")
del _env_before_dotenv


def _setting(parsed):
    """Unpack a config.parse_* result, logging the problem if the raw value was unusable."""
    value, problem = parsed
    if problem:
        log.warning(problem)
    return value


discord_token = os.getenv("DISCORD_TOKEN")
ADMIN_ROLE_NAME: str = os.getenv("ADMIN_ROLE_NAME", "Admin")
DB_PRUNE_DAYS: int = _setting(config.parse_int("DB_PRUNE_DAYS", os.getenv("DB_PRUNE_DAYS"),
                                                default=30, minimum=1))
# Guild-scoped command sync is instant; global sync can take an hour to appear.
# How the model's reasoning phase is handled. This is NOT one-size-fits-all - the right
# answer differs per model, which is why it can be overridden per model at runtime:
#   "false" - send think=False. Correct for qwen3.5:2b (2.3s and a clean answer).
#   "auto"  - send nothing and let Ollama decide. Reasoning then arrives in a separate
#             `thinking` field that never reaches Discord. Correct for qwen3:4b, where
#             think=False makes it dump 3,800 characters of reasoning into the reply.
#   "true"  - force reasoning on. Rarely wanted: on qwen3.5:2b it burned the whole budget
#             thinking and returned 0 characters of content (done_reason="length").
# The default suits the default model; /model detects and records the right mode for others.
_raw_think = os.getenv("OLLAMA_THINK")
OLLAMA_THINK: str = normalise_think_mode(_raw_think, fallback="")
if not OLLAMA_THINK:
    if _raw_think and _raw_think.strip():
        log.warning(f"OLLAMA_THINK={_raw_think.strip()!r} isn't false, true or auto; "
                    f"using false.")
    OLLAMA_THINK = "false"
# Web search is on by default. Turn it off if DuckDuckGo starts refusing requests -
# it scrapes their HTML page, so it can break the way yt-dlp does.
# An unrecognised word keeps search on and says so. It used to read as "off", so a typo
# like WEB_SEARCH=ture silently disabled the feature.
WEB_SEARCH: bool = _setting(config.parse_bool("WEB_SEARCH", os.getenv("WEB_SEARCH"),
                                              default=True))
# Ollama unloads an idle model after ~5 minutes, and reloading it costs 4.3 seconds before
# the first character appears - measured cold 4.31s vs warm 0.03s. For a bot that is used in
# bursts that penalty lands on almost every conversation, so ask Ollama to keep the model
# resident. The cost is real: the model holds its weights in memory (2.4 GB for qwen3.5:2b)
# for this long after the last message. Set "0" to restore the old unload-immediately
# behaviour, or a longer window like "2h" on a dedicated machine.
OLLAMA_KEEP_ALIVE: str = os.getenv("OLLAMA_KEEP_ALIVE", "30m").strip() or "30m"
# Amy's voice in calls (speech.py). Off unless TTS=on: it needs `pip install -r
# requirements-tts.txt` (about 1.2 GB) and `python speech.py download` first. With it off,
# Kokoro and PyTorch are never imported.
TTS: bool = _setting(config.parse_bool("TTS", os.getenv("TTS"), default=False))
AMY_VOICE: speech.Recipe = _setting(speech.parse_voice_recipe(os.getenv("AMY_VOICE")))
TTS_SPEED: float = _setting(config.parse_float("TTS_SPEED", os.getenv("TTS_SPEED"),
                                               default=1.0, minimum=0.5, maximum=2.0))
TTS_THREADS: int = _setting(config.parse_int("TTS_THREADS", os.getenv("TTS_THREADS"),
                                             default=speech.DEFAULT_THREADS, minimum=1))
# How loud the music stays while Amy speaks over it: 0.3 = 30%, 1.0 = not lowered at all.
DUCK_LEVEL: float = _setting(config.parse_float("DUCK_LEVEL", os.getenv("DUCK_LEVEL"),
                                                default=mixer.DEFAULT_DUCK, minimum=0.0,
                                                maximum=1.0))
# How loud Amy's voice is: 1.0 = full (normalised) level, 0.75 = 25% quieter.
VOICE_LEVEL: float = _setting(config.parse_float("VOICE_LEVEL", os.getenv("VOICE_LEVEL"),
                                                 default=speech.DEFAULT_VOICE_LEVEL,
                                                 minimum=0.1, maximum=1.0))
# How far back Amy remembers, and how much is replayed to the model each reply. The cap
# itself lives in database.py because storage enforces it too; this is the single source of
# truth for both. It is a speed knob as well as a memory one - every stored message is sent
# on every reply, and prompt processing grew from 0.15s at 10 messages to 0.95s at 200.
HISTORY_LIMIT: int = _setting(config.parse_int("HISTORY_LIMIT", os.getenv("HISTORY_LIMIT"),
                                                default=DEFAULT_MAX_MESSAGES,
                                                minimum=MIN_MAX_MESSAGES))

_raw_guild = os.getenv("GUILD_ID", "").strip()
GUILD_ID: Optional[int] = int(_raw_guild) if _raw_guild.isdigit() else None

if not discord_token:
    log.error("DISCORD_TOKEN not found in .env file. Please add it and try again.")
    exit(1)

intents = discord.Intents.default()
intents.message_content = True
# Privileged: must also be enabled in the Discord Developer Portal.
# Needed so VoiceChannel.members resolves reliably (it looks up guild.get_member),
# which is what empty-channel auto-disconnect depends on.
intents.members = True

# Nothing Amy sends may ping @everyone, @here, a role, or an arbitrary user. She repeats
# text she doesn't control - model replies (steerable by fetched web pages, or by simply
# asking her to), YouTube titles in "Loading **...**" - and without this discord.py sends
# no restriction at all, so Discord parses every mention in it. Set client-wide so it
# covers channel messages, their streamed edits, and slash-command follow-ups alike.
# Replies still notify the person replied to, as they always have.
NO_MASS_PINGS = discord.AllowedMentions(everyone=False, users=False, roles=False,
                                        replied_user=True)
bot = discord.Client(intents=intents, allowed_mentions=NO_MASS_PINGS)

# The model Amy talks with. Set OLLAMA_MODEL in .env to change it without editing source.
# This is the fallback: the value actually used is restored from saved settings below, and
# comes back to this if nothing was saved or the saved model is no longer installed.
DEFAULT_MODEL: str = os.getenv("OLLAMA_MODEL", "").strip() or "qwen3.5:2b"
BASE_SYSTEM_PROMPT = '''You are Amy, a sophisticated and helpful personal assistant with the demeanor of a professional secretary.

Personality Traits:
- Poised and professional, yet warm and approachable
- Efficient and resourceful in solving problems
- Respectful and courteous in all interactions

Your Capabilities:
- Answer questions accurately and thoughtfully on virtually any topic
- Execute commands when instructed (command details will be provided during the conversation)
- Manage schedules, reminders, and personal tasks

Limitations:
- You should decline requests that are harmful, illegal, or unethical
- Always prioritize security and privacy

Remember: You are here to make your master's life easier, more organized, and more productive. Approach each interaction with dedication and a desire to be helpful.
'''


system_prompt = build_system_prompt(BASE_SYSTEM_PROMPT, WEB_SEARCH)
#----------------------------------------------

#----Database------
# Where conversation memory, settings and the saved queue live. Overridable so a second
# instance - or the test suite - can use its own file. The tests set this before importing
# the bot: settings are read from the database at import time, so swapping `db` afterwards
# is too late, and the suite used to run against the operator's real file.
DB_PATH: str = os.getenv("AMY_DB_PATH", "").strip() or "amy_memory.db"
try:
    db = ConversationDB(DB_PATH, max_messages=HISTORY_LIMIT)
except SchemaTooNewError as e:
    # Written by a newer Amy (an older checkout is running). Refusing beats corrupting.
    log.error(f"{DB_PATH}: {e}")
    exit(1)
#----------------------------------------------

#----Voice & Music------
voice_manager = VoiceManager(db)
# With TTS on, every player gets a speech queue and every track plays through a Mixer.
# The hold callback is looked up at call time: it's defined further down.
music_manager = MusicManager(speech=TTS, duck=DUCK_LEVEL,
                             on_hold_done=lambda gid: _on_hold_done(gid))
#----------------------------------------------

#----Bot State------
# /toggle and /model are persisted. As plain globals they reset on every restart, so an
# admin could switch to a bigger model, restart, and be silently back on the default - with
# nothing but /status to reveal it. The saved model is verified against Ollama at startup
# (ModelManager.verify_restored), because checking needs a network call this code can't make.
model_manager = ModelManager(db, DEFAULT_MODEL, OLLAMA_THINK, OLLAMA_KEEP_ALIVE)
bot_enabled: bool = db.get_bool_setting("bot_enabled", True)
if model_manager.current != DEFAULT_MODEL:
    log.info(f"Restored saved model: {model_manager.current}")
if not bot_enabled:
    log.info("Restored saved state: responses are OFF (use /toggle to enable)")

# Loaded in the background from on_ready, never here: a cold load takes ~9 seconds.
speaker: Optional[speech.Speaker] = (
    speech.Speaker(speech.KokoroEngine(AMY_VOICE, speed=TTS_SPEED, threads=TTS_THREADS),
                   level=VOICE_LEVEL)
    if TTS else None)
if speaker is not None:
    log.info(f"Voice on: {speech.format_recipe(AMY_VOICE)} at {TTS_SPEED}x - loading after login")
#----------------------------------------------

#----Slash Command Tree------
tree = app_commands.CommandTree(bot)


async def deny(interaction: discord.Interaction, message: str) -> None:
    """Refuse a command privately, so permission errors don't clutter the channel."""
    embed = ui.error_embed(message)
    if interaction.response.is_done():
        await interaction.followup.send(embed=embed, ephemeral=True)
    else:
        await interaction.response.send_message(embed=embed, ephemeral=True)


def admin_only():
    """
    Gate a slash command behind Amy's admin rule.

    Discord's own permission checks can't express this: admin means server owner OR the
    Administrator permission OR the configured role, so it reuses is_admin_member.
    """
    async def predicate(interaction: discord.Interaction) -> bool:
        if is_admin_member(interaction.guild, interaction.user.id):
            return True
        await deny(interaction, "You don't have permission to use this command. (Admin only)")
        return False
    return app_commands.check(predicate)


async def send_reply(interaction: discord.Interaction, result,
                     ephemeral: bool = False) -> None:
    """
    Deliver whatever a handler returned.

    Handlers return a str, a ui.Reply, or "" when they already sent their own message.
    Uses followup when the interaction was deferred, otherwise responds directly.
    """
    if not result:
        return

    if isinstance(result, Reply):
        kwargs = {}
        if result.content:
            kwargs["content"] = result.content
        if result.embed is not None:
            kwargs["embed"] = result.embed
        if result.view is not None:
            kwargs["view"] = result.view
        if ephemeral:
            kwargs["ephemeral"] = True
        if interaction.response.is_done():
            await interaction.followup.send(**kwargs)
        else:
            await interaction.response.send_message(**kwargs)
        return

    # Plain text: split at a word boundary rather than truncating. Discord rejects a
    # message over 2000 characters outright, so a growing /help would start erroring.
    remaining = result
    while remaining:
        idx = find_split_index(remaining, MAX_DISCORD_LEN)
        chunk, remaining = remaining[:idx], remaining[idx:]
        if interaction.response.is_done():
            await interaction.followup.send(chunk, ephemeral=ephemeral)
        else:
            await interaction.response.send_message(chunk, ephemeral=ephemeral)
#--------------------------------------

#----Admin Helper------
def is_admin_member(guild: Optional[discord.Guild], user_id: int) -> bool:
    """
    True if the user owns the guild, has Administrator, or holds the admin role.

    Takes ids rather than a Message so both text commands and button interactions can
    share one definition of "admin".
    """
    if guild is None:
        return False
    if guild.owner_id == user_id:
        return True
    # Resolve to Member for roles/permissions (the cache may not hold every user)
    member = guild.get_member(user_id)
    if member is None:
        return False
    if member.guild_permissions.administrator:
        return True
    return any(role.name == ADMIN_ROLE_NAME for role in member.roles)


def is_admin(msg: discord.Message) -> bool:
    """Admin check for a text command."""
    return is_admin_member(msg.guild, msg.author.id)
#----------------------------------------------

#----Rate Limiting------
rate_limit_store: Dict[int, List[float]] = defaultdict(list)
RATE_LIMIT_MAX: int = 5
RATE_LIMIT_WINDOW: int = 3600  # 1 hour in seconds

# Voice commands get their own, much shorter window so /join can't be used to make Amy flap
voice_cmd_store: Dict[int, List[float]] = defaultdict(list)
VOICE_CMD_MAX: int = 3
VOICE_CMD_WINDOW: int = 60  # 1 minute in seconds

def _sliding_window_check(
    store: Dict[int, List[float]], user_id: int, max_calls: int, window: int
) -> Tuple[bool, int]:
    """
    Shared sliding-window limiter.
    Returns (allowed, seconds_until_reset), pruning timestamps outside the window first.
    """
    now = time.time()
    store[user_id] = [t for t in store[user_id] if now - t < window]
    if len(store[user_id]) >= max_calls:
        oldest = store[user_id][0]
        return False, int(window - (now - oldest))
    store[user_id].append(now)
    return True, 0

def check_rate_limit(user_id: int) -> Tuple[bool, int]:
    """Chat message limiter."""
    return _sliding_window_check(rate_limit_store, user_id, RATE_LIMIT_MAX, RATE_LIMIT_WINDOW)

def check_voice_cooldown(user_id: int) -> Tuple[bool, int]:
    """Voice command limiter."""
    return _sliding_window_check(voice_cmd_store, user_id, VOICE_CMD_MAX, VOICE_CMD_WINDOW)

def get_rate_limited_count() -> int:
    """Return how many users are currently at or over the rate limit."""
    now = time.time()
    return sum(
        1 for timestamps in rate_limit_store.values()
        if len([t for t in timestamps if now - t < RATE_LIMIT_WINDOW]) >= RATE_LIMIT_MAX
    )
#----------------------------------------------

#----Auto-Prune Old Messages------
def prune_rate_limit_stores() -> int:
    """
    Drop users whose rate-limit windows have fully expired.

    The sliding-window check trims each user's timestamps but never removes the entry
    itself, so without this every user who ever spoke keeps a dict slot forever.
    """
    now = time.time()
    removed = 0
    for store, window in ((rate_limit_store, RATE_LIMIT_WINDOW),
                          (voice_cmd_store, VOICE_CMD_WINDOW)):
        stale = [uid for uid, ts in store.items()
                 if not any(now - t < window for t in ts)]
        for uid in stale:
            del store[uid]
            removed += 1
    return removed


@tasks.loop(hours=24)
async def prune_old_messages_task() -> None:
    loop = asyncio.get_running_loop()
    deleted = await loop.run_in_executor(None, db.prune_old_messages, DB_PRUNE_DAYS)
    if deleted:
        log.info(f"Pruned {deleted} message(s) older than {DB_PRUNE_DAYS} days")

    dropped = prune_rate_limit_stores()
    if dropped:
        log.info(f"Dropped {dropped} expired rate-limit entr(ies)")


#----Queue Persistence------
# The queue only ever lived in memory, so restarting Amy threw away whatever was lined up -
# including a 50-track playlist someone had just added. A snapshot is taken periodically and
# at every track change, and restored on startup.
SNAPSHOT_INTERVAL: int = 20   # seconds; also the worst-case drift of the saved position


def snapshot_player(guild_id: int) -> None:
    """
    Save one guild's queue and player state. Never raises - persistence is a convenience,
    and a database problem must not interrupt playback.
    """
    try:
        player = music_manager.player_for(guild_id)
        guild = bot.get_guild(guild_id)
        vc = voice.get_voice_client(guild) if guild else None
        channel = voice.active_channel(vc)

        # Slot 0 is the track playing right now, so a restore knows where to pick up.
        tracks = []
        if player.current is not None:
            tracks.append(music.track_to_dict(player.current))
        tracks.extend(music.track_to_dict(t) for t in player.queue)

        if not tracks:
            db.clear_player_state(guild_id)
            return

        db.save_player_state(
            guild_id,
            tracks,
            loop_mode=player.loop_mode.value,
            volume=player.volume,
            voice_channel_id=channel.id if channel else None,
            text_channel_id=player.text_channel_id,
            # Only meaningful when there is a current track occupying slot 0
            resume_position=player.position() if player.current is not None else 0.0,
        )
    except Exception as e:
        log.warning(f"Could not save the queue for guild {guild_id}: {e}")


@tasks.loop(seconds=SNAPSHOT_INTERVAL)
async def snapshot_queues_task() -> None:
    """
    Keep every active guild's snapshot fresh, including the playback position.

    Skips a player whose lock is held. advance_playback pops the next track into a local
    and spends seconds resolving it while player.current still holds the finished one; a
    snapshot taken then saved neither, so a crash in that window lost the next track. Once
    the change completes, advance_playback snapshots it itself.
    """
    for guild_id, player in list(music_manager.players.items()):
        if player.lock.locked():
            continue
        snapshot_player(guild_id)


async def restore_player(guild: discord.Guild) -> bool:
    """
    Put a saved queue back for one guild. Returns True if anything was restored.

    Amy rejoins and resumes only when people are still sitting in the voice channel she was
    in. Coming back from a restart to an empty channel and playing music to nobody would be
    worse than waiting - so in that case the queue is restored silently, and the next /play
    or /join starts it (from where it stopped, the same as a resume would).

    A player that is already in use always wins: the snapshot is dropped and nothing is
    touched. Restore runs while on_ready is still syncing commands, so a /play can get in
    first - and applying a stale snapshot on top used to replace the live queue outright.
    """
    state = db.load_player_state(guild.id)
    if not state:
        return False

    tracks = [t for t in (music.track_from_dict(d) for d in state["tracks"]) if t]
    channel = guild.get_channel(state["voice_channel_id"] or 0)
    if not isinstance(channel, (discord.VoiceChannel, discord.StageChannel)):
        channel = None

    player = music_manager.player_for(guild.id)
    player_active = (player.current is not None or bool(player.queue)
                     or voice.active_channel(voice.get_voice_client(guild)) is not None)

    # The decision itself lives in voice.py, free of Discord objects, so every branch can
    # be covered offline - this one only runs at startup and is otherwise awkward to reach.
    action = voice.decide_restore_action(
        restorable_tracks=len(tracks),
        channel_found=channel is not None,
        channel_has_humans=bool(channel is not None and voice.humans_in(channel)),
        player_active=player_active,
    )

    if action is voice.RestoreAction.NOTHING:
        if player_active:
            log.info(f"Guild {guild.id} is already playing - saved queue discarded")
        db.clear_player_state(guild.id)
        return False

    player.queue.clear()                        # empty by construction; NOTHING covers live
    player.queue.extend(tracks)                 # slot 0 replays from the front
    player.loop_mode = music.loop_mode_from(state["loop_mode"])
    player.volume = state["volume"]
    player.text_channel_id = state["text_channel_id"]
    # Kept for whichever starts it - an immediate resume below, or a later /play or /join
    player.resume_position = max(0.0, float(state["resume_position"]))
    db.clear_player_state(guild.id)             # consumed; the snapshot task rewrites it
    log.info(f"Restored {len(tracks)} track(s) for guild {guild.id}")

    if action is voice.RestoreAction.QUEUE_ONLY:
        where = f"{channel.name} is empty" if channel else "the channel is gone"
        log.info(f"{where} - queue restored but not resumed")
        return True

    assert channel is not None                  # RESUME implies a channel with people in it
    # Same lock /play and /join connect under, re-checked inside, so a command that connects
    # while this is waiting doesn't race it into a second connect.
    async with voice_manager.lock_for(guild.id):
        if voice.active_channel(voice.get_voice_client(guild)) is None:
            try:
                await voice.connect_to(channel)
            except Exception as e:
                log.warning(f"Could not rejoin {channel.name} to resume: {e}")
                return True

    await advance_playback(guild)               # no-op if a command already started playback
    log.info(f"Resumed playback in {channel.name}")
    return True


# Restoring is a once-per-process event. discord.py fires on_ready again whenever a RESUME
# fails, and by then the snapshot holds the track that is currently playing - restoring
# again would queue it a second time and revert any edits made in the last 20 seconds.
_queues_restored: bool = False


async def restore_saved_queues() -> int:
    """Restore every saved queue, once per process. Returns how many guilds were restored."""
    global _queues_restored
    if _queues_restored:
        return 0
    _queues_restored = True                     # set first, so an overlapping READY can't
                                                # start a second pass while this one runs
    restored = 0
    for saved_id in db.saved_guild_ids():
        saved_guild = bot.get_guild(saved_id)
        if saved_guild is None:
            db.clear_player_state(saved_id)     # she is no longer in that server
            continue
        try:
            if await restore_player(saved_guild):
                restored += 1
        except Exception as e:
            log.warning(f"Could not restore the queue for {saved_id}: {e}")
    return restored


async def start_waiting_queue(guild: discord.Guild) -> int:
    """
    If tracks are queued but nothing is playing, start them. Returns how many were waiting.

    This is how a quietly restored queue gets picked up when someone joins: the docs always
    said /join would do it, but /join only connected and went silent.
    """
    player = music_manager.player_for(guild.id)
    vc = voice.get_voice_client(guild)
    if player.current is not None or not player.queue:
        return 0
    if music.music_active(vc, player):
        return 0
    waiting = len(player.queue)
    await advance_playback(guild)
    return waiting


def cleanup_guild_music(guild_id: int) -> None:
    """
    Forget all playback state for a guild that Amy has left - in memory and on disk.

    music_manager.cleanup alone left the saved snapshot in place until the next 20-second
    tick, so a restart inside that window rejoined the channel she had been told to leave.
    """
    music_manager.cleanup(guild_id)
    try:
        db.clear_player_state(guild_id)
    except Exception as e:                      # never let persistence break leaving voice
        log.warning(f"Could not clear the saved queue for {guild_id}: {e}")
#----------------------------------------------

#----Streaming Chat------
STREAM_EDIT_INTERVAL: float = 1.5  # Seconds between Discord message edits during streaming

async def chat_streaming(
    user_message: str,
    server_id: Union[int, str],
    channel_id: int,
    discord_msg: discord.Message,
) -> Optional[str]:
    """
    Stream an Ollama response and progressively edit discord_msg as tokens arrive.
    Handles <think> blocks by showing 'Thinking...' until actual content begins.
    Returns the final reply text (so it can also be spoken), or None if it failed.
    """
    loop = asyncio.get_running_loop()
    chunk_queue: asyncio.Queue = asyncio.Queue()
    error_flag: Dict[str, Any] = {"occurred": False, "done_reason": None, "tool_calls": []}

    db.store_message(server_id, channel_id, "user", user_message)
    # Only the most recent turns are replayed; the full conversation stays in the database.
    history = db.get_messages(server_id, channel_id, limit=HISTORY_LIMIT)

    current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    current_day = datetime.now().strftime("%A")
    system_with_time = (
        f"{system_prompt}\n\n[IMPORTANT] Current date and time: {current_time} ({current_day}). "
        "Use this information when answering questions about time."
    )
    messages = [{"role": "system", "content": system_with_time}] + history

    def stream_worker(convo, offer_tools: bool) -> None:
        """Pump one Ollama stream into the queue. Tool calls are captured, not queued."""
        try:
            # Passed explicitly rather than via **kwargs: the ollama stubs are overloaded
            # and a kwargs dict defeats overload matching. The mode is looked up per model,
            # because think=False is right for one model and ruinous for another.
            active = model_manager.current     # once, so the name and its mode agree
            thinking = think_value(model_manager.think_mode_for(active))
            if offer_tools:
                stream = ollama.chat(model=active, messages=convo, stream=True,
                                     think=thinking,
                                     keep_alive=OLLAMA_KEEP_ALIVE,
                                     tools=[websearch.WEB_SEARCH_TOOL])
            else:
                stream = ollama.chat(model=active, messages=convo, stream=True,
                                     think=thinking,
                                     keep_alive=OLLAMA_KEEP_ALIVE)
            for chunk in stream:
                message = chunk['message']
                calls = message.get('tool_calls') or []
                if calls:
                    error_flag["tool_calls"] = list(calls)
                content = message['content']
                # Remember why generation stopped, so an empty answer can be explained
                reason = chunk.get('done_reason')
                if reason:
                    error_flag["done_reason"] = reason
                loop.call_soon_threadsafe(chunk_queue.put_nowait, content)
        except httpx.ConnectError as e:
            log.warning(f"Ollama unavailable during stream: {e}")
            error_flag["occurred"] = True
        except Exception as e:
            log.error(f"Streaming error: {e}")
            error_flag["occurred"] = True
        finally:
            loop.call_soon_threadsafe(chunk_queue.put_nowait, None)  # Sentinel: stream done

    active_msg = discord_msg
    committed_len = 0  # Length of display text already finalized into prior messages
    accumulated = ""
    last_edit = time.monotonic()

    async def flush_overflow(display: str) -> None:
        """While pending text overflows the Discord limit, finalize the active message and start a new one."""
        nonlocal active_msg, committed_len
        pending = display[committed_len:]
        while len(pending) > STREAM_CHUNK_LIMIT:
            idx = find_split_index(pending, STREAM_CHUNK_LIMIT)
            chunk = pending[:idx]
            try:
                await active_msg.edit(content=chunk)
            except discord.HTTPException:
                pass
            committed_len += idx
            active_msg = await active_msg.channel.send("⏳ Thinking...")
            pending = display[committed_len:]

    async def consume_stream(convo, offer_tools: bool) -> None:
        """Run one streaming pass, editing the live message as tokens arrive."""
        nonlocal accumulated, last_edit
        future = loop.run_in_executor(None, stream_worker, convo, offer_tools)
        while True:
            chunk = await chunk_queue.get()
            if chunk is None:
                break
            accumulated += chunk

            now = time.monotonic()
            if now - last_edit >= STREAM_EDIT_INTERVAL:
                display = get_display_text(accumulated)
                await flush_overflow(display)
                pending = display[committed_len:]
                content = (pending + " ▌") if pending else "⏳ Thinking..."
                try:
                    await active_msg.edit(content=content)
                    last_edit = now
                except discord.HTTPException:
                    pass
        await future  # Re-raise any thread exception

    # Pass one. Tools are offered only when search is enabled; if the model asks to search
    # we run it and stream a second pass with the findings appended.
    await consume_stream(messages, offer_tools=WEB_SEARCH)

    calls = error_flag.get("tool_calls") or []
    if calls and not error_flag["occurred"]:
        call = calls[0]                      # one search per message - no tool loops
        args = call["function"].get("arguments") or {}
        if not isinstance(args, dict):
            args = {}
        query = args.get("query") or user_message
        # Freshness is inferred from the query wording rather than asked of the model - a
        # second tool parameter measurably cost search decisions (see websearch.py).
        recency = websearch.infer_recency(query)
        log.info(f"Web search requested: {query!r} (recency={recency or 'any'})")

        try:
            await active_msg.edit(content=f"🔍 Searching the web for **{query[:80]}**...")
        except discord.HTTPException:
            pass

        # Two phases so the live message reflects what's actually happening: the result
        # list arrives in about a second, reading the pages themselves takes a few more.
        results = await websearch.search(query, recency=recency, with_content=False)
        if results:
            try:
                await active_msg.edit(
                    content=f"📖 Reading {len(results)} source(s) for **{query[:60]}**...")
            except discord.HTTPException:
                pass
            await websearch.enrich(results)
        # tool_name ties the result back to the call. Without it the model treats the
        # results as an unattributed blob and may insist it has no web access at all.
        messages = messages + [
            {"role": "assistant", "content": "", "tool_calls": [call]},
            {"role": "tool",
             "tool_name": websearch.WEB_SEARCH_TOOL["function"]["name"],
             "content": websearch.format_for_model(query, results, recency=recency)},
        ]

        # Reset for the second pass; the first produced a tool call, not prose
        accumulated = ""
        error_flag["tool_calls"] = []
        error_flag["done_reason"] = None
        await consume_stream(messages, offer_tools=False)

    if error_flag["occurred"]:
        db.pop_last_message(server_id, channel_id)
        try:
            await active_msg.edit(
                content="I apologize, but I'm currently having trouble connecting to my knowledge system. Please try again in a moment."
            )
        except discord.HTTPException:
            pass
        return None

    final_response = strip_think_tags(accumulated)
    if not final_response:
        # Empty output almost always means the model hit its token ceiling rather than
        # having nothing to say, so don't tell the user to rephrase - it won't help.
        if error_flag.get("done_reason") == "length":
            log.warning("Model hit its token limit before producing an answer")
            final_response = (
                "I ran out of room before I could finish that answer. "
                "Try asking for something shorter, or ask an admin to raise my limit."
            )
        else:
            final_response = (
                "I apologize, but I couldn't produce an answer for that one. "
                "Could you try asking a different way?"
            )

    db.store_message(server_id, channel_id, "assistant", final_response)

    await flush_overflow(final_response)
    pending = final_response[committed_len:]
    try:
        await active_msg.edit(content=pending if pending else "✅ Done.")
    except discord.HTTPException as e:
        log.error(f"Failed to edit final message: {e}")
    return final_response
#----------------------------------------------

#----Voice Command Handlers------
async def execute_voice_command(
    command: str,
    parts: List[str],
    interaction: discord.Interaction,
    guild: discord.Guild,
) -> Union[str, Reply]:
    """
    Handle /join, /create and /leave.

    Takes an already-narrowed, non-optional guild so every access below is type-safe
    rather than relying on a guard in a different branch.
    """
    if command == "join":
        member = guild.get_member(interaction.user.id)
        state = member.voice if member else None
        if state is None or state.channel is None:
            return Reply(embed=ui.error_embed("You're not in a voice channel. Join one first, then use `/join`."))

        target = state.channel
        perms = target.permissions_for(guild.me)
        if not perms.connect:
            return Reply(embed=ui.error_embed(f"I don't have permission to connect to **{target.name}**."))
        if not perms.speak:
            return Reply(embed=ui.error_embed(f"I can join **{target.name}**, but I'm not allowed to speak there."))

        async with voice_manager.lock_for(guild.id):
            vc = voice.get_voice_client(guild)
            current = voice.active_channel(vc)
            current_id = current.id if current else None
            has_humans = voice.humans_in(current) > 0 if current else False

            action = voice.decide_join_action(current_id, has_humans, target.id, is_admin_member(interaction.guild, interaction.user.id))

            if action is voice.JoinAction.ALREADY_THERE:
                return Reply(embed=ui.info_embed(f"I'm already in **{target.name}**."))
            if action is voice.JoinAction.BLOCKED_OCCUPIED:
                where = current.name if current else "another channel"
                return Reply(embed=ui.error_embed(
                    f"I'm currently in **{where}** with other people. "
                    "Join that channel, or ask an admin to move me."
                ))
            try:
                if action is voice.JoinAction.MOVE and vc is not None:
                    await vc.move_to(target)
                    verb = "Moved to"
                else:
                    await voice.connect_to(target)
                    verb = "Joined"
            except RuntimeError as e:
                # discord.py raises this when a voice dependency is missing (PyNaCl or davey).
                # Report what it actually said rather than guessing which one.
                log.error(f"Voice connect failed: {e}")
                return Reply(embed=ui.error_embed(
                    f"Voice support isn't fully installed on my host: {e}\n"
                    "Fix: `pip install \"discord.py[voice]\"`"
                ))
            except (discord.ClientException, asyncio.TimeoutError) as e:
                log.error(f"Voice connect failed: {e}")
                return Reply(embed=ui.error_embed(f"I couldn't connect to **{target.name}**. Please try again."))

        # A queue may be waiting with nothing playing - typically one restored quietly after
        # a restart. Joining is the moment to start it. Outside the voice lock, since
        # advance_playback can take seconds to resolve the first track.
        player = music_manager.player_for(guild.id)
        if player.text_channel_id is None:
            player.text_channel_id = interaction.channel_id
        waiting = await start_waiting_queue(guild)
        note = f" Picking up {waiting} queued track(s)." if waiting else ""
        announce(guild, "join")
        return Reply(embed=ui.voice_embed(f"{verb} **{target.name}**.{note}"))

    if command == "create":
        if not is_admin_member(interaction.guild, interaction.user.id):
            return Reply(embed=ui.error_embed("You don't have permission to use this command. (Admin only)"))
        if not guild.me.guild_permissions.manage_channels:
            return Reply(embed=ui.error_embed("I need the **Manage Channels** permission to create a voice channel."))

        # Only guild text channels have a category to inherit
        category = interaction.channel.category if isinstance(interaction.channel, discord.TextChannel) else None
        name = voice.sanitize_channel_name(" ".join(parts[1:]))

        async with voice_manager.lock_for(guild.id):
            try:
                channel = await guild.create_voice_channel(
                    name,
                    category=category,
                    reason=f"/create requested by {interaction.user}",
                )
            except discord.Forbidden:
                return Reply(embed=ui.error_embed("Discord refused that. Check my **Manage Channels** permission."))
            except discord.HTTPException as e:
                log.error(f"Channel creation failed: {e}")
                return Reply(embed=ui.error_embed("I couldn't create that channel. Please try again."))

            try:
                vc = voice.get_voice_client(guild)
                if vc is not None and vc.is_connected():
                    await vc.move_to(channel)
                else:
                    await voice.connect_to(channel)
            except Exception as e:
                log.error(f"Could not join newly created channel: {e}")
                # Channel exists but Amy couldn't join - don't orphan it
                try:
                    await channel.delete(reason="Amy could not join the channel she created")
                except discord.HTTPException:
                    pass
                if isinstance(e, RuntimeError):
                    # Missing voice dependency - say which, so it's actionable
                    return Reply(embed=ui.error_embed(
                        f"I created the channel but voice support isn't fully installed: {e}\n"
                        "Fix: `pip install \"discord.py[voice]\"` (channel removed again)"
                    ))
                return Reply(embed=ui.error_embed("I created the channel but couldn't join it, so I removed it again."))

            voice_manager.mark_created(channel.id, guild.id)
            announce(guild, "join")
            return Reply(embed=ui.voice_embed(
                f"Created {channel.mention} and joined — hop in!", heading="Voice channel created"))

    if command == "leave":
        vc = voice.get_voice_client(guild)
        current = voice.active_channel(vc)
        if current is None:
            return Reply(embed=ui.error_embed("I'm not in a voice channel."))

        member = guild.get_member(interaction.user.id)
        in_same_channel = bool(
            member and member.voice and member.voice.channel
            and member.voice.channel.id == current.id
        )
        if not (is_admin_member(interaction.guild, interaction.user.id) or in_same_channel):
            return Reply(embed=ui.error_embed("You need to be in my voice channel (or an admin) to make me leave."))

        await say_goodbye(guild)     # waits for it to play, at most GOODBYE_WAIT seconds
        async with voice_manager.lock_for(guild.id):
            left = await voice.leave_voice(guild, voice_manager, on_cleanup=cleanup_guild_music)
        if not left:
            return Reply(embed=ui.error_embed("I'm not in a voice channel."))
        return Reply(embed=ui.voice_embed(f"Left **{left}**.", heading="Voice"))

    return f"Unknown voice command: `{command}`."
#--------------------------------------

#----Reply Emoji------
# Written as escapes so the source stays ASCII-safe on Windows consoles
SEARCH: str = "🔍"     # magnifying glass
DENY: str = "🚫"       # prohibited
HOURGLASS: str = "⏳"      # hourglass
NEXT: str = "⏭"           # next track
#--------------------------------------

#----Command Groups------
# Harmless reads - exempt from the voice cooldown so checking the queue never costs a slot
#--------------------------------------

#----Player Buttons------
def may_control_playback(guild: Optional[discord.Guild], user_id: int) -> bool:
    """
    The one playback-permission rule: you must be in Amy's voice channel, or be an admin.
    Used by both the music commands (MusicContext.may_control) and the player buttons, so
    neither can become a way around the other.
    """
    if guild is None:
        return False
    if is_admin_member(guild, user_id):
        return True
    current = voice.active_channel(voice.get_voice_client(guild))
    if current is None:
        return False
    member = guild.get_member(user_id)
    return bool(
        member and member.voice and member.voice.channel
        and member.voice.channel.id == current.id
    )


class PlayerControls(discord.ui.View):
    """
    Buttons under the now-playing message.

    Persistent: timeout=None with fixed custom_ids, registered once via bot.add_view().
    A default View expires after 180s, which would leave dead buttons partway through a
    song. Every handler resolves state from interaction.guild_id, so one instance serves
    every message and the buttons keep working across a restart.
    """

    def __init__(self, paused: bool = False) -> None:
        super().__init__(timeout=None)
        # Reflect current state on the toggle rather than showing two buttons
        self.pause_button.label = "Resume" if paused else "Pause"
        self.pause_button.emoji = "▶" if paused else "⏸"

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if may_control_playback(interaction.guild, interaction.user.id):
            return True
        await interaction.response.send_message(
            embed=ui.error_embed("You need to be in my voice channel (or an admin) to use these."),
            ephemeral=True,
        )
        return False

    @discord.ui.button(label="Previous", emoji="⏮", style=discord.ButtonStyle.secondary,
                       custom_id="amy:previous")
    async def previous_button(self, interaction: discord.Interaction,
                              button: discord.ui.Button) -> None:
        if interaction.guild is None:
            return
        went_back, message = go_back(interaction.guild)
        if not went_back:
            await interaction.response.send_message(embed=ui.error_embed(message),
                                                    ephemeral=True)
            return
        await interaction.response.defer()   # the now-playing card updates itself

    @discord.ui.button(label="Pause", emoji="⏸", style=discord.ButtonStyle.secondary,
                       custom_id="amy:pause")
    async def pause_button(self, interaction: discord.Interaction,
                           button: discord.ui.Button) -> None:
        guild = interaction.guild
        vc = voice.get_voice_client(guild) if guild else None
        if guild is None or vc is None or not vc.is_connected():
            await interaction.response.send_message(
                embed=ui.error_embed("I'm not in a voice channel."), ephemeral=True)
            return

        player = music_manager.player_for(guild.id)
        # Through the music helpers: they keep the position clock honest, and know the
        # music is "paused" while Amy speaks over it with the track held.
        if music.resume_music(vc, player):
            paused = False
            announce(guild, "resume")
        elif music.pause_music(vc, player):
            paused = True
            announce(guild, "pause")
        else:
            await interaction.response.send_message(
                embed=ui.error_embed("Nothing is playing."), ephemeral=True)
            return

        if player.current is None:
            await interaction.response.defer()
            return
        await interaction.response.edit_message(
            embed=ui.now_playing_embed(player.current, len(player.queue),
                                       player.loop_mode, player.volume, paused=paused,
                                       elapsed=player.position()),
            view=PlayerControls(paused=paused),
        )

    @discord.ui.button(label="Skip", emoji="⏭", style=discord.ButtonStyle.primary,
                       custom_id="amy:skip")
    async def skip_button(self, interaction: discord.Interaction,
                          button: discord.ui.Button) -> None:
        guild = interaction.guild
        vc = voice.get_voice_client(guild) if guild else None
        player = music_manager.player_for(guild.id) if guild else None
        if guild is None or player is None or not music.music_active(vc, player) or vc is None:
            await interaction.response.send_message(
                embed=ui.error_embed("Nothing is playing."), ephemeral=True)
            return

        player.skip_requested = True  # so TRACK loop can't swallow an explicit skip
        skipped = player.current.title if player.current else "this one"
        await interaction.response.defer()
        vc.stop()  # after-callback advances the queue and refreshes the message
        announce(guild, "skip", title=skipped)

    @discord.ui.button(label="Stop", emoji="⏹", style=discord.ButtonStyle.danger,
                       custom_id="amy:stop")
    async def stop_button(self, interaction: discord.Interaction,
                          button: discord.ui.Button) -> None:
        guild = interaction.guild
        vc = voice.get_voice_client(guild) if guild else None
        if guild is None or vc is None or not vc.is_connected():
            await interaction.response.send_message(
                embed=ui.error_embed("I'm not in a voice channel."), ephemeral=True)
            return

        player = music_manager.player_for(guild.id)
        finished = player.current or player.last_played
        player.stop_all()       # also cancels a track that is still being prepared
        # Matches /stop: stop and stay. The idle timer still disconnects her later.
        vc.stop()
        announce(guild, "stop")

        embed = (ui.now_playing_embed(finished, stopped=True) if finished
                 else ui.info_embed("Playback stopped."))
        await interaction.response.edit_message(embed=embed, view=None)


class SearchResults(discord.ui.View):
    """
    Dropdown of /search hits.

    Unlike PlayerControls this holds per-message state (the specific results), so it
    cannot be a persistent view. It expires after SEARCH_TIMEOUT and disables itself.

    The timeout used to be 60s, which ran out while people were still reading five titles.
    A disabled select menu gives no error - Discord just greys it out and shows a
    "not allowed" cursor - so it read as the bot being broken rather than as an expiry.
    Hence the longer window and the footer that says what happened.
    """

    SEARCH_TIMEOUT: float = 180.0

    def __init__(self, tracks, requester_id: int, guild: discord.Guild,
                 query: str = "") -> None:
        super().__init__(timeout=self.SEARCH_TIMEOUT)
        self.tracks = tracks
        self.requester_id = requester_id
        self.guild = guild
        self.query = query
        self.message: Optional[discord.Message] = None

        options = []
        for i, track in enumerate(tracks):
            options.append(discord.SelectOption(
                label=ui._clip(track.title, ui.MAX_SELECT_LABEL),
                description=music.format_duration(track.duration),
                value=str(i),
            ))
        self.picker.options = options

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        # Only whoever ran /search may choose from their own results
        if interaction.user.id == self.requester_id:
            return True
        await interaction.response.send_message(
            embed=ui.error_embed("Only the person who searched can pick from this list."),
            ephemeral=True,
        )
        return False

    async def on_timeout(self) -> None:
        self.picker.disabled = True
        if self.message is None:
            return
        # Replace the "Pick one from the menu below" footer as well. Leaving it in place
        # tells the user to do something the greyed-out menu no longer allows.
        embed = ui.search_results_embed(self.query, self.tracks)
        embed.set_footer(text="This search expired — run /search again to pick a track.")
        try:
            await self.message.edit(embed=embed, view=self)
        except discord.HTTPException:
            pass

    @discord.ui.select(placeholder="Choose a track...", min_values=1, max_values=1)
    async def picker(self, interaction: discord.Interaction,
                     select: discord.ui.Select) -> None:
        track = self.tracks[int(select.values[0])]
        guild = self.guild
        player = music_manager.player_for(guild.id)

        if len(player.queue) >= music.MAX_QUEUE_SIZE:
            await interaction.response.edit_message(
                embed=ui.error_embed(f"The queue is full ({music.MAX_QUEUE_SIZE} tracks)."),
                view=None)
            return

        # The searcher must be in a voice channel, exactly as /play requires
        member = guild.get_member(interaction.user.id)
        state = member.voice if member else None
        if voice.active_channel(voice.get_voice_client(guild)) is None:
            if state is None or state.channel is None:
                await interaction.response.edit_message(
                    embed=ui.error_embed("You're not in a voice channel."), view=None)
                return
            perms = state.channel.permissions_for(guild.me)
            if not perms.connect or not perms.speak:
                await interaction.response.edit_message(
                    embed=ui.error_embed(
                        f"I need Connect and Speak permissions in **{state.channel.name}**."),
                    view=None)
                return
            try:
                async with voice_manager.lock_for(guild.id):
                    await voice.connect_to(state.channel)
            except Exception as e:
                log.error(f"Voice connect failed during search pick: {e}")
                await interaction.response.edit_message(
                    embed=ui.error_embed("I couldn't join your voice channel."), view=None)
                return

        player.text_channel_id = interaction.channel_id
        player.queue.append(track)
        player.cancel_idle()
        self.stop()

        vc = voice.get_voice_client(guild)
        playing = music.music_active(vc, player)
        if not music.starts_immediately(len(player.queue), playing):
            # Acknowledge first: starting a backlog resolves a stream URL, which can outlast
            # the 3-second interaction window.
            if not playing:
                await interaction.response.edit_message(
                    embed=ui.queued_embed(track, len(player.queue) - 1), view=None)
                await advance_playback(guild)
                announce(guild, "queued", title=track.title, pos=len(player.queue))
                return
            await interaction.response.edit_message(
                embed=ui.queued_embed(track, len(player.queue)), view=None)
            announce(guild, "queued", title=track.title, pos=len(player.queue))
            return

        await interaction.response.edit_message(
            embed=ui.info_embed(f"Loading **{track.title}**..."), view=None)
        await advance_playback(guild)   # posts the now-playing card
        announce(guild, "play_now", title=track.title)


async def refresh_now_playing(guild: discord.Guild, stopped: bool = False,
                              paused: bool = False) -> None:
    """
    Keep one now-playing message per guild, edited in place as tracks change, so a long
    queue doesn't post a message per track. Posts a fresh one if the old is unreachable.
    """
    player = music_manager.player_for(guild.id)
    channel = bot.get_channel(player.text_channel_id) if player.text_channel_id else None
    if not isinstance(channel, discord.TextChannel):
        return

    if stopped or player.current is None:
        last = player.last_played
        if last is None:
            return
        embed = ui.now_playing_embed(last, stopped=True)
        view = None
    else:
        embed = ui.now_playing_embed(player.current, len(player.queue),
                                     player.loop_mode, player.volume, paused=paused,
                                     elapsed=player.position())
        view = PlayerControls(paused=paused)

    if player.now_playing_msg is not None:
        try:
            await player.now_playing_msg.edit(embed=embed, view=view)
            return
        except discord.HTTPException:
            player.now_playing_msg = None  # deleted or unreachable; fall through

    try:
        player.now_playing_msg = await channel.send(
            embed=embed, view=view or discord.utils.MISSING)
    except discord.HTTPException as e:
        log.warning(f"Could not post now-playing message: {e}")
#--------------------------------------

#----Music Playback Engine------
async def finish_playback(guild: discord.Guild, player: music.GuildPlayer) -> None:
    """Nothing left to play: show the finished card, forget the snapshot, start idling."""
    if player.current is not None:
        player.last_played = player.current
    player.current = None
    player.mark_stopped()      # nothing playing, so the position clock resets
    snapshot_player(guild.id)  # nothing left to restore; drops the saved snapshot
    await refresh_now_playing(guild, stopped=True)
    log.info("Queue empty - starting idle timer")
    player.cancel_idle()  # never stack timers; a stale one could disconnect later
    player.idle_task = asyncio.create_task(idle_disconnect(guild))


async def advance_playback(guild: discord.Guild) -> None:
    """
    Play the next track. Called when one finishes, and to kick off the first track.
    Runs under the player lock so a finishing track and a new /play can't race.
    """
    player = music_manager.player_for(guild.id)
    async with player.lock:
        vc = voice.get_voice_client(guild)
        if vc is None or not vc.is_connected():
            player.reset()
            return

        # Another call may have started a track while this one waited for the lock
        # (resolving a stream URL holds it for seconds). Starting a second one would
        # raise "Already playing audio" and silently drop the track.
        # A track already loaded - Amy's voice playing alone doesn't count: _start hands
        # over from it.
        if music.music_active(vc, player):
            return

        # A user-requested skip overrides TRACK loop for this one advance; a /previous has
        # already arranged the queue. Both are one-shot.
        force_next, going_back = player.skip_requested, player.back_requested
        player.skip_requested = player.back_requested = False
        next_track = music.advance_with_history(
            player.current, player.queue, player.history, player.loop_mode,
            force_next=force_next, going_back=going_back,
        )
        if next_track is None:
            await finish_playback(guild, player)
            return

        loop = asyncio.get_running_loop()
        after = music.make_after_callback(loop, lambda: advance_playback(guild))
        # A restored queue starts mid-track; every other advance starts at zero.
        start_at = player.resume_position
        player.resume_position = 0.0
        try:
            if not await music.play_track(vc, player, next_track, after, start_at=start_at):
                # /stop landed while the track was loading. Nothing was playing for its
                # vc.stop() to interrupt, so no after-callback will tidy up - do it here.
                await finish_playback(guild, player)
                return
            player.last_played = next_track
            await refresh_now_playing(guild)
            snapshot_player(guild.id)   # record the new track and a fresh position
        except Exception as e:
            log.error(f"Could not play '{next_track.title}': {e}")
            # Skip the bad track rather than stalling the whole queue
            player.current = None
            music.spawn(advance_playback(guild))


async def idle_disconnect(guild: discord.Guild) -> None:
    """Leave after IDLE_DISCONNECT_DELAY if the queue is still empty."""
    try:
        await asyncio.sleep(music.IDLE_DISCONNECT_DELAY)
    except asyncio.CancelledError:
        return

    player = music_manager.player_for(guild.id)
    if player.current is not None or player.queue:
        return
    vc = voice.get_voice_client(guild)
    if vc is None or not vc.is_connected() or vc.is_playing():
        return

    async with voice_manager.lock_for(guild.id):
        left = await voice.leave_voice(guild, voice_manager, on_cleanup=cleanup_guild_music)
    if left:
        log.info(f"Left {left} after being idle")
#--------------------------------------

#----Speaking in the Call------
# Amy's voice reaches the call through the guild's speech queue (music.GuildPlayer.speech).
# Whatever is playing picks it up: a track's Mixer ducks the music under it; with nothing
# playing a speech-only Mixer plays it; over paused music the track is held while she speaks.
# Rules: ARCHITECTURE.md, "Speech".
NO_VOICE = "My voice is off. Set TTS=on in .env (see the README)."
VOICE_LOADING = "My voice is still loading - try again in a few seconds."
NOT_ENGLISH = "I can only speak English for now."
NOTHING_TO_SAY = "There's nothing in that I can say out loud."


def voice_problem() -> Optional[str]:
    """Why Amy can't speak right now, or None if she can."""
    if speaker is None:
        return NO_VOICE
    if speaker.state == "failed":
        return f"My voice couldn't load: {speaker.problem}"
    if not speaker.ready:
        return VOICE_LOADING
    return None


def start_saying(guild: discord.Guild, text: str) -> Tuple[bool, str]:
    """
    Check Amy can say `text` here, then speak it in the background. Returns (started, reply).
    Quick on purpose - synthesis takes about a second per sentence, so it never runs inside
    an interaction's 3-second window.
    """
    problem = voice_problem()
    if problem:
        return False, problem
    vc = voice.get_voice_client(guild)
    if vc is None or not vc.is_connected():
        return False, NOT_CONNECTED
    spoken = speech.clean_for_speech(text)
    if not spoken:
        return False, NOTHING_TO_SAY
    if not speech.is_english(spoken):
        return False, NOT_ENGLISH
    music.spawn(_speak_sentences(guild, speech.split_sentences(spoken)))
    return True, "Saying it now."


async def _speak_sentences(guild: discord.Guild, sentences: List[str]) -> None:
    """Synthesise one sentence at a time and queue it, so she starts after the first."""
    assert speaker is not None
    player = music_manager.player_for(guild.id)
    for sentence in sentences:
        frames = await speaker.frames_for(sentence)
        vc = voice.get_voice_client(guild)
        if not frames or player.speech is None or vc is None or not vc.is_connected():
            return                                  # she left, or synthesis failed (logged)
        dropped = player.speech.add(frames)
        if dropped:
            log.info(f"Speech backlog full: dropped {dropped} older sentence(s)")
        await ensure_speaking(guild)


async def ensure_speaking(guild: discord.Guild) -> None:
    """Make sure queued speech is heard: start a speech-only source, or hold paused music."""
    player = music_manager.player_for(guild.id)
    if player.speech is None or not player.speech:
        return
    vc = voice.get_voice_client(guild)
    if vc is None or not vc.is_connected():
        player.speech.clear()
        return
    if vc.is_playing():
        return                       # a Mixer is running and will pick the speech up
    if vc.is_paused():
        held = mixer.mixer_of(vc)
        if held is not None:
            # Speak over paused music: resume with the track held, so it doesn't move on.
            # The position clock stays paused - the music isn't being read.
            held.hold_music = True
            vc.resume()
        return
    # Nothing playing: not even a track loading has started yet. Speak on our own; if a track
    # starts meanwhile, music._start hands over and the rest of the speech plays over it.
    loop = asyncio.get_running_loop()
    player.speech_only = True
    vc.play(mixer.Mixer(None, player.speech, player.duck),
            after=lambda error: loop.call_soon_threadsafe(
                lambda: music.spawn(_after_speech_only(guild.id))))


async def _after_speech_only(guild_id: int) -> None:
    """A speech-only source ended (it ran out, or a track took over). Never touches music."""
    guild = bot.get_guild(guild_id)
    if guild is None:
        return
    player = music_manager.player_for(guild_id)
    vc = voice.get_voice_client(guild)
    if vc is None or not vc.is_playing():
        player.speech_only = False
    await ensure_speaking(guild)     # more speech may have arrived as it ended


def _on_hold_done(guild_id: int) -> None:
    """From the audio thread: speech over paused music has run out - pause the music again."""
    try:
        bot.loop.call_soon_threadsafe(lambda: music.spawn(_pause_after_speech(guild_id)))
    except Exception as e:                       # the loop is closing during shutdown
        log.warning(f"Could not re-pause after speaking: {e}")


async def _pause_after_speech(guild_id: int) -> None:
    guild = bot.get_guild(guild_id)
    vc = voice.get_voice_client(guild) if guild else None
    held = mixer.mixer_of(vc)
    if vc is None or held is None or not held.hold_music:
        return                       # someone resumed the music meanwhile
    player = music_manager.player_for(guild_id)
    if player.speech:
        return                       # more speech arrived; the Mixer will call again
    vc.pause()
    held.hold_music = False          # paused for real again, exactly as before she spoke


def can_speak(guild: discord.Guild) -> bool:
    """Whether Amy can speak here right now: voice loaded and in a call."""
    vc = voice.get_voice_client(guild)
    return voice_problem() is None and vc is not None and vc.is_connected()


def speak(guild: discord.Guild, text: str) -> None:
    """
    Say `text` in the background, or do nothing if she can't. For Amy's own lines -
    confirmations, replies - where a reason to stay quiet isn't worth reporting.
    """
    if not can_speak(guild):
        return
    spoken = speech.clean_for_speech(text)
    if spoken and speech.is_english(spoken):
        music.spawn(_speak_sentences(guild, speech.split_sentences(spoken)))


def announce(guild: Optional[discord.Guild], event: str, title: Optional[str] = None,
             **values: object) -> None:
    """Speak a confirmation of a music action (voicelines.LINES), if Amy can speak."""
    if guild is None or not can_speak(guild):
        return
    if title is not None:
        values["t"] = voicelines.speakable_title(title)
    speak(guild, voicelines.confirmation(event, values))


def in_call_with_amy(guild: discord.Guild, user_id: int) -> bool:
    """Whether this user is in Amy's voice channel right now - who her replies are spoken to."""
    channel = voice.active_channel(voice.get_voice_client(guild))
    member = guild.get_member(user_id)
    return bool(channel and member and member.voice and member.voice.channel
                and member.voice.channel.id == channel.id)


async def speak_reply(guild: discord.Guild, reply: str) -> None:
    """
    Speak a chat reply (D5): short ones as written; long ones as a model paraphrase - or,
    if that's slow, fails or isn't usable, the opening sentences and "the rest is in the
    chat". Non-English replies get a short English line instead (D6).
    """
    if not can_speak(guild):
        return
    kind, text = voicelines.plan_reply(reply)
    if kind == "nothing":
        return
    if kind == "paraphrase":
        raw = await model_manager.paraphrase(voicelines.PARAPHRASE_SYSTEM,
                                             voicelines.paraphrase_request(text),
                                             timeout=voicelines.PARAPHRASE_TIMEOUT)
        text = voicelines.judge_paraphrase(raw) or voicelines.fallback_summary(text)
    speak(guild, text)


# How long /leave waits for the goodbye before leaving anyway: synthesis (~1s) plus a short line.
GOODBYE_WAIT = 4.5


async def say_goodbye(guild: discord.Guild) -> None:
    """Say goodbye and wait (up to GOODBYE_WAIT) until it has played, so leaving doesn't cut it."""
    if not can_speak(guild):
        return
    player = music_manager.player_for(guild.id)

    async def speak_and_drain() -> None:
        await _speak_sentences(guild, [voicelines.confirmation("leave")])
        while player.speech:
            await asyncio.sleep(0.05)
        await asyncio.sleep(0.1)     # the last frame or two still in flight to Discord

    try:
        await asyncio.wait_for(speak_and_drain(), timeout=GOODBYE_WAIT)
    except asyncio.TimeoutError:
        log.info("Goodbye took too long; leaving anyway")
#--------------------------------------

#----Music Command Handlers------
# One typed function per command. The slash layer converts and bounds every argument through
# Discord's own typing (ranges, choices), so these receive ints and enums, never raw text.
# They used to share a ~380-line dispatcher keyed on the command name, which turned typed
# slash arguments back into strings and parsed them again - a holdover from prefix commands,
# whose "must be a number" checks could no longer fire.

NOT_CONNECTED = "I'm not in a voice channel."
NOT_WITH_AMY = "You need to be in my voice channel to do that."
ADMIN_ONLY_MSG = "You don't have permission to use this command. (Admin only)"
MusicResult = Union[str, Reply]


def _error(text: str) -> Reply:
    return Reply(embed=ui.error_embed(text))


def _info(text: str) -> Reply:
    return Reply(embed=ui.info_embed(text))


@dataclass
class MusicContext:
    """What every music command needs, resolved once per invocation."""
    interaction: discord.Interaction
    guild: discord.Guild
    player: music.GuildPlayer
    vc: Optional[discord.VoiceClient]
    member: Optional[discord.Member]

    @classmethod
    def build(cls, interaction: discord.Interaction, guild: discord.Guild) -> "MusicContext":
        return cls(interaction, guild, music_manager.player_for(guild.id),
                   voice.get_voice_client(guild), guild.get_member(interaction.user.id))

    @property
    def user_id(self) -> int:
        return self.interaction.user.id

    def is_admin(self) -> bool:
        return is_admin_member(self.guild, self.user_id)

    def may_control(self) -> bool:
        # The same rule the player buttons use, so neither can become a way around the other.
        return may_control_playback(self.guild, self.user_id)

    def connected(self) -> Optional[discord.VoiceClient]:
        """The voice client if Amy is connected, else None - narrows for the type checker."""
        return self.vc if self.vc is not None and self.vc.is_connected() else None


# --- read-only, no connection needed ---

async def music_queue(ctx: MusicContext, page: int = 1) -> MusicResult:
    p = ctx.player
    return Reply(embed=ui.queue_embed(p.current, p.queue, p.loop_mode, page=page))


async def music_nowplaying(ctx: MusicContext) -> MusicResult:
    p = ctx.player
    if p.current is None:
        return _info("Nothing is playing right now.")
    paused = music.music_paused(ctx.vc)
    return Reply(
        embed=ui.now_playing_embed(p.current, len(p.queue), p.loop_mode, p.volume,
                                   paused=paused, elapsed=p.position()),
        view=PlayerControls(paused=paused),
    )


def _shown(query: str) -> str:
    # Echoed back into a Discord message, so keep it well under the 2000-char limit
    return query if len(query) <= 100 else query[:100] + "..."


async def music_search(ctx: MusicContext, query: str) -> MusicResult:
    query = query.strip()
    if not query:
        return _error("What should I search for? Usage: `/search <song name>`")
    if music.find_ffmpeg() is None:
        return _error("FFmpeg isn't available on my host, so I can't play audio.")

    shown = _shown(query)
    status_msg = await ctx.interaction.followup.send(
        SEARCH + " Searching for **" + shown + "**...", wait=True)
    try:
        results = await music.search_tracks(query, requested_by=str(ctx.interaction.user))
    except Exception as e:
        log.error("Search failed: " + str(e))
        await status_msg.edit(content=None,
                              embed=ui.error_embed("That search went wrong. Try again."))
        return ""

    if not results:
        await status_msg.edit(content=None, embed=ui.error_embed(
            "Nothing found for `" + shown + "`."))
        return ""

    view = SearchResults(results, ctx.user_id, ctx.guild, query=query)
    await status_msg.edit(content=None,
                          embed=ui.search_results_embed(query, results), view=view)
    view.message = status_msg   # so on_timeout can grey out the menu
    return ""


# --- /play: joins if needed, then queues ---

async def music_play(ctx: MusicContext, query: str) -> MusicResult:
    # Passed through untouched. The old dispatcher split the query on whitespace and joined
    # it back with single spaces, so a local file named "My  Song.mp3" could never be found.
    if not query.strip():
        return _error("What should I play? Usage: `/play <song name, URL, or file path>`")
    query = query.strip()
    guild, player, member = ctx.guild, ctx.player, ctx.member

    if music.find_ffmpeg() is None:
        return _error(
            "FFmpeg isn't available on my host, so I can't play audio.\n"
            "Install it, or set `FFMPEG_PATH` in `.env` to the full path of `ffmpeg.exe`.")

    # Checked before joining, so Amy doesn't connect only to refuse the track
    if len(player.queue) >= music.MAX_QUEUE_SIZE:
        return _error(f"The queue is full ({music.MAX_QUEUE_SIZE} tracks). "
                      "Try again once it drains.")

    # Join the requester's channel if not already connected
    vc = ctx.vc
    if voice.active_channel(vc) is None:
        state = member.voice if member else None
        if state is None or state.channel is None:
            return _error("You're not in a voice channel. Join one first, then use `/play`.")
        perms = state.channel.permissions_for(guild.me)
        if not perms.connect or not perms.speak:
            return _error(f"I need Connect and Speak permissions in **{state.channel.name}**.")
        try:
            async with voice_manager.lock_for(guild.id):
                await voice.connect_to(state.channel)
            vc = ctx.vc = voice.get_voice_client(guild)
        except RuntimeError as e:
            log.error(f"Voice connect failed: {e}")
            return _error(f"Voice support isn't fully installed on my host: {e}\n"
                          "Fix: `pip install \"discord.py[voice]\"`")
        except Exception as e:
            log.error(f"Voice connect failed: {e}")
            return _error("I couldn't join your voice channel. Please try again.")
    elif not ctx.may_control():
        return _error("You need to be in my voice channel to queue tracks.")

    # advance_playback has no message context, so remember where to post the card
    player.text_channel_id = ctx.interaction.channel_id

    shown = _shown(query)
    author = str(ctx.interaction.user)

    # Resolving hits the network and can take a few seconds, so acknowledge
    # immediately and edit this message once we know the result.
    status_msg = await ctx.interaction.followup.send(
        SEARCH + " Searching for **" + shown + "**...", wait=True)

    # --- playlist URL: queue many tracks at once ---
    if music.is_playlist_url(query):
        await status_msg.edit(content=SEARCH + " Loading playlist...")
        try:
            title, tracks, skipped = await music.resolve_playlist(query, author)
        except Exception as e:
            log.error("Could not load playlist: " + str(e))
            await status_msg.edit(content=DENY + " I couldn't load that playlist.")
            return ""

        if not tracks:
            await status_msg.edit(content=DENY + " That playlist had no playable tracks.")
            return ""

        # Never exceed the overall queue cap, even if the playlist cap allowed more
        room = music.MAX_QUEUE_SIZE - len(player.queue)
        if len(tracks) > room:
            skipped += len(tracks) - room
            tracks = tracks[:room]

        player.queue.extend(tracks)
        player.cancel_idle()

        await status_msg.edit(content=None,
                              embed=ui.playlist_embed(title, len(tracks), skipped))
        if not music.music_active(vc, player):
            await advance_playback(guild)   # posts its own now-playing card
        announce(guild, "playlist", n=voicelines.count_of(len(tracks), "song"),
                 name=voicelines.speakable_title(title))
        return ""

    # --- single track ---
    try:
        track = await music.resolve_metadata(query, requested_by=author)
    except Exception as e:
        log.error("Could not resolve query: " + str(e))
        await status_msg.edit(content=DENY + " I couldn't find anything for `" + shown + "`.")
        return ""

    player.queue.append(track)
    player.cancel_idle()

    playing = music.music_active(vc, player)
    if not music.starts_immediately(len(player.queue), playing):
        if not playing:
            # Idle with a backlog in front of this track (e.g. a restored queue): start
            # the backlog, and say truthfully where the requested track landed.
            await advance_playback(guild)
        await status_msg.edit(content=None, embed=ui.queued_embed(track, len(player.queue)))
        announce(guild, "queued", title=track.title, pos=len(player.queue))
        return ""

    await status_msg.edit(content=HOURGLASS + " Loading **" + track.title + "**...")
    await advance_playback(guild)   # posts the now-playing card with controls
    announce(guild, "play_now", title=track.title)
    try:
        await status_msg.delete()   # the card replaces this status line
    except discord.HTTPException:
        pass
    return ""


# --- everything below needs an active connection ---

async def music_pause(ctx: MusicContext) -> MusicResult:
    vc = ctx.connected()
    if vc is None:
        return _error(NOT_CONNECTED)
    if not ctx.may_control():
        return _error(NOT_WITH_AMY)
    if not music.pause_music(vc, ctx.player):
        return _error("Nothing is playing.")
    announce(ctx.guild, "pause")
    await refresh_now_playing(ctx.guild, paused=True)
    return _info("Paused.")


async def music_resume(ctx: MusicContext) -> MusicResult:
    vc = ctx.connected()
    if vc is None:
        return _error(NOT_CONNECTED)
    if not ctx.may_control():
        return _error(NOT_WITH_AMY)
    if not music.resume_music(vc, ctx.player):
        return _error("Nothing is paused.")
    announce(ctx.guild, "resume")
    await refresh_now_playing(ctx.guild, paused=False)
    return _info("Resumed.")


async def music_skip(ctx: MusicContext) -> MusicResult:
    vc = ctx.connected()
    if vc is None:
        return _error(NOT_CONNECTED)
    if not ctx.may_control():
        return _error("You need to be in my voice channel to skip.")
    if not music.music_active(vc, ctx.player):
        return _error("Nothing is playing.")
    player = ctx.player
    skipped = player.current.title if player.current else "the current track"
    player.skip_requested = True
    vc.stop()  # triggers the after-callback, which advances the queue
    announce(ctx.guild, "skip", title=skipped)
    return _info(f"Skipped **{skipped}**.")


NO_HISTORY = "There's no previous track to go back to."
BUSY_LOADING = "I'm still loading the next track - try again in a moment."


def go_back(guild: discord.Guild) -> Tuple[bool, str]:
    """
    Play the previous track; the one it interrupts plays straight after. Returns (went back,
    message to show). The one implementation behind /previous and the Previous button, so
    their checks and wording can't drift apart. Synchronous on purpose: nothing here awaits,
    so nothing can change between the checks and the queue edit.

    Refused while the player lock is held - an advance loading the next track, or a /seek
    rebuilding the stream. In that window nothing is audible and `current` still names the
    track just left, so going back used to queue the previous song behind the one already
    loading (or behind an endless TRACK-loop repeat) while replying that it had gone back.

    Never waits on the stream: while something plays, vc.stop() hands over to the
    after-callback; when nothing does, the advance runs in the background, since resolving
    takes seconds and a slash reply must answer within three.
    """
    vc = voice.get_voice_client(guild)
    if vc is None or not vc.is_connected():
        return False, NOT_CONNECTED
    player = music_manager.player_for(guild.id)
    if player.lock.locked():
        return False, BUSY_LOADING
    playing = music.music_active(vc, player)
    prev = music.step_back(player.current if playing else None, player.queue, player.history)
    if prev is None:
        return False, NO_HISTORY
    player.cancel_idle()
    if playing:
        player.back_requested = True
        vc.stop()   # the after-callback plays `prev`
    else:
        music.spawn(advance_playback(guild))
    announce(guild, "previous", title=prev.title)
    return True, f"Going back to **{prev.title}**."


async def music_previous(ctx: MusicContext) -> MusicResult:
    if ctx.connected() is None:
        return _error(NOT_CONNECTED)
    if not ctx.may_control():
        return _error("You need to be in my voice channel to go back.")
    went_back, message = go_back(ctx.guild)
    return _info(message) if went_back else _error(message)


async def music_seek(ctx: MusicContext, position: str) -> MusicResult:
    return await _restart_current(ctx, position)


async def music_replay(ctx: MusicContext) -> MusicResult:
    return await _restart_current(ctx, None)


async def _restart_current(ctx: MusicContext, position: Optional[str]) -> MusicResult:
    """/seek to `position`, or /replay from the start when it is None."""
    vc = ctx.connected()
    if vc is None:
        return _error(NOT_CONNECTED)
    if not ctx.may_control():
        return _error(NOT_WITH_AMY)
    if not music.music_active(vc, ctx.player):
        return _error("Nothing is playing.")
    player, guild = ctx.player, ctx.guild
    track = player.current
    if track is None:
        return _error("Nothing is playing.")
    if track.is_local is False and not track.query.startswith("http"):
        return _error("I can't seek in this track.")

    if position is None:
        target = 0.0
    else:
        seconds = music.parse_timestamp(position)
        if seconds is None:
            return _error("I couldn't read that position. Use `1:30`, `1:02:03` or a number "
                          "of seconds.")
        if track.duration and seconds >= track.duration:
            return _error(f"That's past the end of the track "
                          f"({music.format_duration(track.duration)}). Use `/skip` to move on.")
        target = music.clamp_seek(float(seconds), track.duration)

    loop = asyncio.get_running_loop()
    after = music.make_after_callback(loop, lambda: advance_playback(guild))
    try:
        # The lock is what makes this safe: stopping the old source fires the
        # after-callback, which calls advance_playback and would otherwise pull the
        # next track off the queue. Holding the lock keeps that advance waiting until
        # the new source is playing, at which point its is_playing() guard returns.
        async with player.lock:
            moved = await music.restart_at(vc, player, track, after, target)
    except Exception as e:
        # The new source is built before the old one is touched, so a failure here
        # leaves the track playing where it was - and the message says exactly that.
        log.error(f"Seek failed on '{track.title}': {e}")
        return _error(f"I couldn't jump there, so **{track.title}** is still playing where it was.")
    if not moved:
        return _info("Playback was stopped, so I didn't jump.")

    await refresh_now_playing(guild, paused=music.music_paused(vc))
    await ensure_speaking(guild)    # a seek on paused music re-pauses; speech waiting resumes
    if position is None:
        announce(guild, "replay", title=track.title)
    else:
        announce(guild, "seek", at=voicelines.speakable_time(target))
    if position is None:
        return _info(f"Replaying **{track.title}** from the start.")
    return _info(f"Jumped to **{music.format_duration(int(target))}** in **{track.title}**.")


async def music_stop(ctx: MusicContext) -> MusicResult:
    vc = ctx.connected()
    if vc is None:
        return _error(NOT_CONNECTED)
    if not ctx.may_control():
        return _error("You need to be in my voice channel (or be an admin) to stop playback.")
    ctx.player.stop_all()       # also cancels a track that is still being prepared
    # Stop only. vc.stop() fires the after-callback, which finds an empty queue,
    # shows the finished card and starts the idle timer - so Amy still leaves on her
    # own after 5 minutes. /leave is the command for disconnecting straight away.
    vc.stop()
    snapshot_player(ctx.guild.id)   # a deliberate clear shouldn't come back on restart
    announce(ctx.guild, "stop")       # after stop_all, which clears any queued speech
    return _info("Stopped and cleared the queue. I'll stay here — use `/leave` to send me away.")


async def music_volume(ctx: MusicContext, level: Optional[int] = None) -> MusicResult:
    vc = ctx.connected()
    if vc is None:
        return _error(NOT_CONNECTED)
    if not ctx.is_admin():
        return _error(ADMIN_ONLY_MSG)
    player = ctx.player
    if level is None:
        return _info(f"Current volume: **{int(player.volume * 100)}%**\nUsage: `/volume 0-100`")
    player.volume = level / 100
    announce(ctx.guild, "volume", level=level)
    # Applies instantly only on the PCM path; an opus-passthrough track has no
    # volume stage, so the change lands when the next track starts.
    source = mixer.music_source(vc)    # looks through the speech Mixer when TTS is on
    if isinstance(source, discord.PCMVolumeTransformer):
        source.volume = player.volume
        return _info(f"Volume set to **{level}%**.")

    if level >= 100:
        return _info("Volume set to **100%** (full quality, lowest CPU).")
    return _info(
        f"Volume set to **{level}%** — applies from the next track.\n"
        "_Note: below 100% Amy has to decode audio rather than pass it through, "
        "which costs more CPU and can stutter on a busy machine._")


async def music_loop(ctx: MusicContext, mode: LoopMode) -> MusicResult:
    if ctx.connected() is None:
        return _error(NOT_CONNECTED)
    if not ctx.may_control():
        return _error(NOT_WITH_AMY)
    ctx.player.loop_mode = mode
    announce(ctx.guild, f"loop_{mode.value}")
    return _info(f"Loop set to **{mode.value}**.")


async def music_remove(ctx: MusicContext, position: int) -> MusicResult:
    if ctx.connected() is None:
        return _error(NOT_CONNECTED)
    if not ctx.may_control():
        return _error(NOT_WITH_AMY)
    queue = ctx.player.queue
    target = music.peek_at(queue, position)
    if target is None:
        return _error("There's no track at position " + str(position) + ".")
    # Users may only remove what they queued; admins may remove anything
    if target.requested_by != str(ctx.interaction.user) and not ctx.is_admin():
        return (DENY + " That track was queued by " + target.requested_by
                + ". You can only remove your own.")

    music.remove_at(queue, position)  # `target` above already proved it exists
    announce(ctx.guild, "remove", title=target.title)
    return _info("Removed **" + target.title + "** from the queue.")


async def music_shuffle(ctx: MusicContext) -> MusicResult:
    if ctx.connected() is None:
        return _error(NOT_CONNECTED)
    if not ctx.may_control():
        return _error(NOT_WITH_AMY)
    queue = ctx.player.queue
    if len(queue) < 2:
        return _error("Not enough tracks queued to shuffle.")
    music.shuffle_queue(queue)
    announce(ctx.guild, "shuffle", n=voicelines.count_of(len(queue), "song"))
    return _info("Shuffled **" + str(len(queue)) + "** queued track(s).")


async def music_clearqueue(ctx: MusicContext) -> MusicResult:
    if ctx.connected() is None:
        return _error(NOT_CONNECTED)
    if not ctx.is_admin():
        return _error(ADMIN_ONLY_MSG)
    queue = ctx.player.queue
    count = len(queue)
    if count == 0:
        return _error("The queue is already empty.")
    queue.clear()
    announce(ctx.guild, "clearqueue", n=voicelines.count_of(count, "song"))
    return _info("Cleared **" + str(count) + "** queued track(s). Current track keeps playing.")


async def music_skipto(ctx: MusicContext, position: int) -> MusicResult:
    vc = ctx.connected()
    if vc is None:
        return _error(NOT_CONNECTED)
    if not ctx.may_control():
        return _error(NOT_WITH_AMY)
    player = ctx.player
    target = music.peek_at(player.queue, position)
    if target is None:
        return _error("There's no track at position " + str(position) + ".")

    dropped = music.drop_before(player.queue, position)
    player.skip_requested = True
    # stop() fires the after-callback, which pulls the next track off the queue
    vc.stop()
    announce(ctx.guild, "skipto", title=target.title)
    reply = NEXT + " Skipping to **" + target.title + "**"
    if dropped:
        reply += " (" + str(dropped) + " track(s) skipped)"
    return reply + "."
#--------------------------------------

#----Status------
async def build_status(interaction: discord.Interaction) -> str:
    """Assemble the /status report. Kept out of the command so it stays readable."""
    bot_state = "Enabled ✅" if bot_enabled else "Disabled ❌"

    try:
        await asyncio.get_running_loop().run_in_executor(None, ollama.list)
        ollama_state = "Connected ✅"
    except Exception:
        ollama_state = "Unavailable ❌"

    stats = db.get_stats()
    throttled = get_rate_limited_count()
    guild = interaction.guild

    current = voice.active_channel(voice.get_voice_client(guild)) if guild else None
    if current is not None:
        voice_state = f"In **{current.name}** ({voice.humans_in(current)} listener(s))"
    else:
        voice_state = "Not connected"

    ffmpeg_state = "Found ✅" if music.find_ffmpeg() else "Missing ❌"
    if guild:
        mp = music_manager.player_for(guild.id)
        playing = mp.current.title if mp.current else "nothing"
        music_state = f"{playing} | {len(mp.queue)} queued | loop {mp.loop_mode.value}"
    else:
        music_state = "n/a"

    return (
        f"\U0001F4CA **Amy Status**\n"
        f"Bot: {bot_state}\n"
        f"Ollama: {ollama_state} | Model: `{model_manager.current}`\n"
        f"Voice: {voice_state}\n"
        f"Music: {music_state} | FFmpeg: {ffmpeg_state}\n"
        f"Memory: {stats['total_messages']} messages across {stats['active_channels']} channel(s)\n"
        f"Rate limits: {throttled} user(s) currently throttled"
    )
#--------------------------------------

#----Forget Confirmation------
class ForgetConfirm(discord.ui.View):
    """
    Confirm/cancel buttons for /forget.

    Replaces the old reaction flow: buttons need no `reactions` intent, no pending-state
    dictionary keyed by message id, and can be scoped to the requester directly.
    """

    TIMEOUT: float = 60.0

    def __init__(self, requester_id: int, server_id, channel_id: int) -> None:
        super().__init__(timeout=self.TIMEOUT)
        self.requester_id = requester_id
        self.server_id = server_id
        self.channel_id = channel_id
        self.message: Optional[discord.Message] = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.requester_id:
            return True
        await interaction.response.send_message(
            embed=ui.error_embed("Only the person who ran the command can confirm this."),
            ephemeral=True)
        return False

    async def on_timeout(self) -> None:
        if self.message is None:
            return
        try:
            await self.message.edit(
                embed=ui.info_embed("Request timed out. Nothing was deleted."), view=None)
        except discord.HTTPException:
            pass

    @discord.ui.button(label="Wipe memory", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction,
                      button: discord.ui.Button) -> None:
        db.clear_channel(self.server_id, self.channel_id)
        self.stop()
        await interaction.response.edit_message(
            embed=ui.info_embed("Conversation memory for this channel has been cleared."),
            view=None)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction,
                     button: discord.ui.Button) -> None:
        self.stop()
        await interaction.response.edit_message(
            embed=ui.info_embed("Cancelled. Nothing was deleted."), view=None)
#--------------------------------------

#----Slash Command Helpers------
async def voice_gate(interaction: discord.Interaction) -> bool:
    """
    Apply the shared voice cooldown. Read-only music commands and admins skip it.
    Returns False (and refuses privately) when the caller is over the limit.
    """
    if is_admin_member(interaction.guild, interaction.user.id):
        return True
    allowed, reset_in = check_voice_cooldown(interaction.user.id)
    if allowed:
        return True
    await deny(interaction, f"Too many voice commands. Try again in {reset_in}s.")
    return False


async def require_guild(interaction: discord.Interaction) -> Optional[discord.Guild]:
    """
    Narrow interaction.guild for the type checker.

    @app_commands.guild_only() already stops these running in DMs, but the attribute stays
    Optional, so this makes the guarantee explicit instead of scattering asserts.
    """
    if interaction.guild is not None:
        return interaction.guild
    await deny(interaction, "That only works in a server.")
    return None


async def simple_music(
    interaction: discord.Interaction,
    handler: Callable[..., Awaitable[MusicResult]],
    *args: Any,
    cooldown: bool = True,
) -> None:
    """
    Run a music command that needs no network work, so it can answer immediately.
    Read-only commands pass cooldown=False: looking at the queue costs nothing.
    """
    guild = await require_guild(interaction)
    if guild is None:
        return
    if cooldown and not await voice_gate(interaction):
        return
    await send_reply(interaction, await handler(MusicContext.build(interaction, guild), *args))
#--------------------------------------

#----Slash Commands------
# Registration only. Each validates through Discord's own typing, then delegates to the
# handlers above. Commands doing network work defer first: Discord requires a response
# within 3 seconds and yt-dlp resolution takes several.

@tree.command(name="help", description="Show Amy's commands")
async def slash_help(interaction: discord.Interaction) -> None:
    is_adm = is_admin_member(interaction.guild, interaction.user.id)
    # Via send_reply so it splits if the command list outgrows 2000 characters
    await send_reply(interaction, HELP_EVERYONE + (HELP_ADMIN if is_adm else ""),
                     ephemeral=True)


@tree.command(name="dice", description="Roll dice")
@app_commands.describe(sides="Number of sides (default 6)",
                       amount="How many dice to roll (default 1)")
async def slash_dice(
    interaction: discord.Interaction,
    sides: app_commands.Range[int, 1, MAX_DICE_SIDES] = 6,
    amount: app_commands.Range[int, 1, MAX_DICE_AMOUNT] = 1,
) -> None:
    # Range makes Discord reject out-of-bounds input client-side, so the event-loop
    # freeze this once allowed is unreachable rather than merely guarded.
    rolls = [random.randint(1, sides) for _ in range(amount)]
    if amount == 1:
        await interaction.response.send_message(
            "\U0001F3B2 Rolled 1d%d: **%d**" % (sides, rolls[0]))
        return
    breakdown = " + ".join("**%d**" % r for r in rolls)
    await interaction.response.send_message(
        "\U0001F3B2 Rolled %dd%d: %s = **%d**" % (amount, sides, breakdown, sum(rolls)))


@tree.command(name="rng", description="Generate a random number between two values")
@app_commands.describe(minimum="Lowest possible value", maximum="Highest possible value")
async def slash_rng(interaction: discord.Interaction, minimum: int, maximum: int) -> None:
    if minimum > maximum:
        await deny(interaction, "Min must be less than or equal to Max.")
        return
    await interaction.response.send_message(
        "\U0001F3B2 Random number between %d and %d: **%d**"
        % (minimum, maximum, random.randint(minimum, maximum)))


@tree.command(name="websearch", description="Search the web and show the results")
@app_commands.describe(query="What to search for",
                       recency="Only show results from this recently")
@app_commands.choices(recency=[
    app_commands.Choice(name="Past day", value="day"),
    app_commands.Choice(name="Past week", value="week"),
    app_commands.Choice(name="Past month", value="month"),
    app_commands.Choice(name="Past year", value="year"),
])
async def slash_websearch(interaction: discord.Interaction, query: str,
                          recency: Optional[app_commands.Choice[str]] = None) -> None:
    if not WEB_SEARCH:
        await deny(interaction, "Web search is turned off on my host.")
        return
    # Amy usually searches on her own during conversation; this is the manual override
    # for when you want the raw sources, or when she decides not to search.
    if not is_admin_member(interaction.guild, interaction.user.id):
        allowed, reset_in = check_voice_cooldown(interaction.user.id)
        if not allowed:
            await deny(interaction, f"Too many searches. Try again in {reset_in}s.")
            return

    await interaction.response.defer()
    # This view is about the raw sources, so skip fetching page bodies - they cost several
    # seconds and never reach the embed. Amy's own searches during conversation do fetch them.
    # Blank recency means no filter, as the option's wording says - not "infer one".
    results = await websearch.search(query, recency=recency.value if recency else "any",
                                     with_content=False)
    await send_reply(interaction, Reply(embed=ui.search_web_embed(query, results)))


@tree.command(name="join", description="Bring Amy into your voice channel")
@app_commands.guild_only()
async def slash_join(interaction: discord.Interaction) -> None:
    if not await voice_gate(interaction):
        return
    guild = await require_guild(interaction)
    if guild is None:
        return
    await interaction.response.defer()
    await send_reply(interaction,
                     await execute_voice_command("join", ["join"], interaction, guild))


@tree.command(name="leave", description="Make Amy leave her voice channel")
@app_commands.guild_only()
async def slash_leave(interaction: discord.Interaction) -> None:
    if not await voice_gate(interaction):
        return
    guild = await require_guild(interaction)
    if guild is None:
        return
    await interaction.response.defer()
    await send_reply(interaction,
                     await execute_voice_command("leave", ["leave"], interaction, guild))


@tree.command(name="create", description="Create a voice channel and join it")
@app_commands.describe(name="Channel name (defaults to Amy's Room)")
@app_commands.guild_only()
@admin_only()
async def slash_create(interaction: discord.Interaction, name: str = "") -> None:
    guild = await require_guild(interaction)
    if guild is None:
        return
    await interaction.response.defer()
    parts = ["create"] + (name.split() if name else [])
    await send_reply(interaction,
                     await execute_voice_command("create", parts, interaction, guild))


@tree.command(name="play", description="Play a song, URL, or playlist")
@app_commands.describe(query="Song name, YouTube/audio URL, playlist URL, or local file")
@app_commands.guild_only()
async def slash_play(interaction: discord.Interaction, query: str) -> None:
    if not await voice_gate(interaction):
        return
    guild = await require_guild(interaction)
    if guild is None:
        return
    await interaction.response.defer()   # resolution takes seconds
    await send_reply(interaction, await music_play(MusicContext.build(interaction, guild), query))


@tree.command(name="search", description="Search and pick from the top results")
@app_commands.describe(query="What to search for")
@app_commands.guild_only()
async def slash_search(interaction: discord.Interaction, query: str) -> None:
    if not await voice_gate(interaction):
        return
    guild = await require_guild(interaction)
    if guild is None:
        return
    await interaction.response.defer()
    await send_reply(interaction,
                     await music_search(MusicContext.build(interaction, guild), query))


@tree.command(name="pause", description="Pause playback")
@app_commands.guild_only()
async def slash_pause(interaction: discord.Interaction) -> None:
    await simple_music(interaction, music_pause)


@tree.command(name="resume", description="Resume playback")
@app_commands.guild_only()
async def slash_resume(interaction: discord.Interaction) -> None:
    await simple_music(interaction, music_resume)


@tree.command(name="skip", description="Skip the current track")
@app_commands.guild_only()
async def slash_skip(interaction: discord.Interaction) -> None:
    await simple_music(interaction, music_skip)


@tree.command(name="previous", description="Go back to the previous track")
@app_commands.guild_only()
async def slash_previous(interaction: discord.Interaction) -> None:
    await simple_music(interaction, music_previous)


@tree.command(name="seek", description="Jump to a position in the current track")
@app_commands.describe(position="Where to jump to: 1:30, 1:02:03, or seconds")
@app_commands.guild_only()
async def slash_seek(interaction: discord.Interaction, position: str) -> None:
    if not await voice_gate(interaction):
        return
    guild = await require_guild(interaction)
    if guild is None:
        return
    # Seeking re-resolves the stream URL, which is slow enough to blow Discord's 3s window
    await interaction.response.defer()
    await send_reply(interaction,
                     await music_seek(MusicContext.build(interaction, guild), position))


@tree.command(name="replay", description="Restart the current track from the beginning")
@app_commands.guild_only()
async def slash_replay(interaction: discord.Interaction) -> None:
    if not await voice_gate(interaction):
        return
    guild = await require_guild(interaction)
    if guild is None:
        return
    await interaction.response.defer()
    await send_reply(interaction, await music_replay(MusicContext.build(interaction, guild)))


@tree.command(name="stop", description="Stop playback and clear the queue (Amy stays)")
@app_commands.guild_only()
async def slash_stop(interaction: discord.Interaction) -> None:
    await simple_music(interaction, music_stop)


@tree.command(name="queue", description="Show what's playing and what's queued")
@app_commands.describe(page="Page number (10 tracks per page)")
@app_commands.guild_only()
async def slash_queue(
    interaction: discord.Interaction,
    page: app_commands.Range[int, 1, 100] = 1,
) -> None:
    await simple_music(interaction, music_queue, page, cooldown=False)


@tree.command(name="nowplaying", description="Show the current track")
@app_commands.guild_only()
async def slash_nowplaying(interaction: discord.Interaction) -> None:
    await simple_music(interaction, music_nowplaying, cooldown=False)


@tree.command(name="remove", description="Remove a track you queued")
@app_commands.describe(position="Queue position, as shown by /queue")
@app_commands.guild_only()
async def slash_remove(
    interaction: discord.Interaction,
    position: app_commands.Range[int, 1, 100],
) -> None:
    await simple_music(interaction, music_remove, position)


@tree.command(name="skipto", description="Jump ahead to a queued track")
@app_commands.describe(position="Queue position, as shown by /queue")
@app_commands.guild_only()
async def slash_skipto(
    interaction: discord.Interaction,
    position: app_commands.Range[int, 1, 100],
) -> None:
    await simple_music(interaction, music_skipto, position)


@tree.command(name="shuffle", description="Shuffle the queued tracks")
@app_commands.guild_only()
async def slash_shuffle(interaction: discord.Interaction) -> None:
    await simple_music(interaction, music_shuffle)


@tree.command(name="loop", description="Set repeat mode")
@app_commands.describe(mode="What to repeat")
@app_commands.choices(mode=[
    app_commands.Choice(name="off", value="off"),
    app_commands.Choice(name="track", value="track"),
    app_commands.Choice(name="queue", value="queue"),
])
@app_commands.guild_only()
async def slash_loop(interaction: discord.Interaction,
                     mode: app_commands.Choice[str]) -> None:
    await simple_music(interaction, music_loop, LoopMode(mode.value))


@tree.command(name="clearqueue", description="Empty the queue (current track keeps playing)")
@app_commands.guild_only()
@admin_only()
async def slash_clearqueue(interaction: discord.Interaction) -> None:
    await simple_music(interaction, music_clearqueue)


@tree.command(name="volume", description="Show or set playback volume")
@app_commands.describe(level="Volume percent (omit to see the current value)")
@app_commands.guild_only()
@admin_only()
async def slash_volume(
    interaction: discord.Interaction,
    level: Optional[app_commands.Range[int, 0, 100]] = None,
) -> None:
    await simple_music(interaction, music_volume, level)


@tree.command(name="say", description="Make Amy say something in her voice channel")
@app_commands.describe(text="What she should say (English)")
@app_commands.guild_only()
@admin_only()
async def slash_say(interaction: discord.Interaction,
                    text: app_commands.Range[str, 1, 500]) -> None:
    guild = await require_guild(interaction)
    if guild is None:
        return
    started, reply = start_saying(guild, text)
    await send_reply(interaction, Reply(embed=ui.info_embed(reply) if started
                                        else ui.error_embed(reply)), ephemeral=True)


@tree.command(name="toggle", description="Enable or disable Amy's chat replies")
@admin_only()
async def slash_toggle(interaction: discord.Interaction) -> None:
    global bot_enabled
    bot_enabled = not bot_enabled
    db.set_bool_setting("bot_enabled", bot_enabled)   # survives a restart
    state = "enabled" if bot_enabled else "disabled"
    log.info("Bot is now " + state)
    await interaction.response.send_message("\U0001F916 Bot is now **%s**" % state)


@tree.command(name="status", description="Show bot, Ollama, voice and memory status")
@admin_only()
async def slash_status(interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=True)   # ollama.list() hits the network
    await send_reply(interaction, await build_status(interaction))


@tree.command(name="model", description="Show or switch the Ollama model")
@app_commands.describe(name="Model to switch to (omit to see the current one)")
@admin_only()
async def slash_model(interaction: discord.Interaction, name: str = "") -> None:
    await interaction.response.defer()
    await send_reply(interaction, await model_manager.switch(name))


@tree.command(name="forget", description="Wipe Amy's conversation memory for this channel")
@app_commands.guild_only()
@admin_only()
async def slash_forget(interaction: discord.Interaction) -> None:
    server_id = interaction.guild.id if interaction.guild else "DM"
    view = ForgetConfirm(interaction.user.id, server_id, interaction.channel_id or 0)
    await interaction.response.send_message(
        embed=ui.error_embed(
            "This will wipe all conversation memory for this channel.",
            heading="Are you sure?"),
        view=view,
    )
    view.message = await interaction.original_response()
#--------------------------------------

#----Slash Command Error Handling------
@tree.error
async def on_app_command_error(interaction: discord.Interaction,
                               error: app_commands.AppCommandError) -> None:
    """
    Catch anything a slash command didn't handle itself.

    None of the command callbacks wrap their own body, so without this an unexpected
    exception - yt-dlp changing, Ollama dying mid-reply, Discord refusing an edit - reaches
    discord.py's default handler, which logs to stderr and leaves the interaction unanswered.
    The user sees "The application did not respond", or nothing at all if the command had
    already deferred, with no way to connect that to anything in the console.

    A silent failure is the expensive kind: it costs debugging time precisely because
    nothing announces itself. So this says something to the user and logs the command,
    the caller and the full traceback.
    """
    # A failed check has already explained itself - admin_only() and voice_gate() reply
    # before returning False. Re-reporting it would double up on the user and fill the
    # console with tracebacks for ordinary permission denials.
    if isinstance(error, app_commands.CheckFailure):
        return

    # CommandInvokeError wraps the real exception; unwrap so the log names the actual cause.
    original = getattr(error, "original", error)
    command = interaction.command.name if interaction.command else "unknown"
    where = interaction.guild.name if interaction.guild else "a DM"
    log.error(f"/{command} raised for {interaction.user} in {where}: "
              f"{type(original).__name__}: {original}",
              exc_info=(type(original), original, original.__traceback__))

    try:
        await deny(interaction, "Something went wrong running that command. "
                                "It's been logged - try again, and tell an admin if it "
                                "keeps happening.")
    except discord.HTTPException:
        # The interaction may already be dead (past Discord's 15-minute token window, or
        # the 3-second one if the command never deferred). Nothing more to do; it is logged.
        pass
#--------------------------------------

#----Event Handlers for Discord Bot------
@bot.event
async def on_ready() -> None:
    log.info(f"{bot.user} is online!")
    activity = discord.Activity(type=discord.ActivityType.watching, name="conversations")
    await bot.change_presence(activity=activity, status=discord.Status.online)
    log.info("Bot status set to: Watching conversations")

    # Register commands with Discord. Guild-scoped so they appear immediately;
    # a global sync can take up to an hour to propagate.
    try:
        if GUILD_ID:
            scope = discord.Object(id=GUILD_ID)
            tree.copy_global_to(guild=scope)
            synced = await tree.sync(guild=scope)
            log.info(f"Synced {len(synced)} slash command(s) to guild {GUILD_ID}")

            # Discord merges the global and guild scopes, so anything left registered
            # globally (e.g. from a run before GUILD_ID was set) makes every command
            # appear twice. Clear the global scope whenever a guild scope is in use.
            stale = await tree.fetch_commands()
            if stale:
                tree.clear_commands(guild=None)
                await tree.sync()
                log.info(f"Removed {len(stale)} duplicate global command(s)")
        else:
            synced = await tree.sync()
            log.info(f"Synced {len(synced)} slash command(s) globally "
                     "(may take up to an hour to appear - set GUILD_ID for instant sync)")
    except Exception as e:
        log.error(f"Could not sync slash commands: {e}")

    if not prune_old_messages_task.is_running():
        prune_old_messages_task.start()
        log.info(f"Auto-prune task started (prunes messages older than {DB_PRUNE_DAYS} days, every 24h)")

    if not snapshot_queues_task.is_running():
        snapshot_queues_task.start()
        log.info(f"Queue snapshots every {SNAPSHOT_INTERVAL}s")

    # Register the persistent player buttons so they keep working across restarts
    bot.add_view(PlayerControls())
    log.info("Player controls registered")

    # Load the opus encoder up front so the first track doesn't stall while it loads
    if not discord.opus.is_loaded():
        try:
            discord.opus._load_default()
            log.info("Opus encoder loaded")
        except Exception as e:
            log.warning(f"Could not preload opus: {e}")

    # Warm the model in the background so the first real message doesn't pay the load cost.
    # Backgrounded rather than awaited: it takes a few seconds, and nothing else here needs
    # to wait for it.
    model_manager.start_startup_check()

    # Same for Amy's voice: load once, in the background, on its own thread. on_ready fires
    # again after a reconnect; start() does nothing if it's already loading or loaded.
    if speaker is not None and speaker.state == "off":
        music.spawn(speaker.start())

    # Clean up voice channels Amy created before a restart
    try:
        removed = await voice.sweep_orphan_channels(bot, db, voice_manager)
        if removed:
            log.info(f"Cleaned up {removed} orphaned voice channel(s) from a previous run")
    except Exception as e:
        log.warning(f"Orphan voice channel sweep failed: {e}")

    # Put back any queue that was playing when Amy last stopped. Done after the orphan
    # sweep so a channel she created and is about to delete isn't rejoined. Guarded to run
    # once per process: on_ready fires again after a failed RESUME.
    await restore_saved_queues()

@bot.event
async def on_connect() -> None:
    log.info("Bot connected to Discord")

@bot.event
async def on_disconnect() -> None:
    log.warning("Bot disconnected from Discord, attempting to reconnect...")

last_processed_id: Union[int, None] = None

@bot.event
async def on_message(msg: discord.Message) -> None:
    """Process messages from users - execute commands or send to chat."""
    global last_processed_id

    if msg.id == last_processed_id:
        return
    last_processed_id = msg.id

    log.debug(f"Message received from {msg.author}: {msg.content}")

    if msg.author == bot.user:
        log.debug("Ignoring bot's own message")
        return

    try:
        server_id = msg.guild.id if msg.guild else "DM"
        channel_id = msg.channel.id
        log.debug(f"Server ID: {server_id}, Channel ID: {channel_id}")

        # Commands are slash commands now and arrive as interactions, not messages.
        # Anything reaching here is conversation.
        if not bot_enabled:
            log.debug("Bot is disabled, ignoring chat message")
            return

        if not is_admin(msg):
            allowed, reset_in = check_rate_limit(msg.author.id)
            if not allowed:
                minutes = reset_in // 60
                seconds = reset_in % 60
                await msg.reply(
                    f"⏳ You've reached the limit of {RATE_LIMIT_MAX} messages per hour. "
                    f"Try again in {minutes}m {seconds}s."
                )
                return

        log.debug("Processing as streaming chat...")
        thinking_msg = await msg.reply("⏳ Thinking...")
        reply = await chat_streaming(msg.content, server_id, channel_id, thinking_msg)
        # Spoken too, but only to someone in the call with her (D4) - and in the
        # background: the paraphrase and synthesis take seconds and the text is already out.
        if reply and msg.guild is not None and in_call_with_amy(msg.guild, msg.author.id):
            music.spawn(speak_reply(msg.guild, reply))
        log.debug("Streaming response complete")

    except Exception as e:
        log.exception(f"Error processing message: {e}")
        try:
            await msg.reply("Sorry, I encountered an unexpected error. Please try again.")
        except Exception:
            log.error("Failed to send error message to Discord")

@bot.event
async def on_voice_state_update(
    member: discord.Member,
    before: discord.VoiceState,
    after: discord.VoiceState,
) -> None:
    """Leave (and tidy up) once the last human leaves Amy's voice channel."""
    guild = member.guild

    # Amy herself was moved or disconnected by someone else
    if bot.user and member.id == bot.user.id:
        if after.channel is None and before.channel is not None:
            log.info(f"Amy was disconnected from voice channel: {before.channel.name}")
            voice_manager.unmark_created(before.channel.id)
        return

    current = voice.active_channel(voice.get_voice_client(guild))
    if current is None or voice.humans_in(current) > 0:
        return

    # Grace period, so a quick reconnect doesn't kick Amy out
    channel_id = current.id
    await asyncio.sleep(voice.EMPTY_DISCONNECT_DELAY)

    current = voice.active_channel(voice.get_voice_client(guild))
    if current is None or current.id != channel_id or voice.humans_in(current) > 0:
        return

    async with voice_manager.lock_for(guild.id):
        left = await voice.leave_voice(guild, voice_manager, on_cleanup=cleanup_guild_music)
    if left:
        log.info(f"Left empty voice channel: {left}")

#--------------------------------------

if __name__ == "__main__":
    try:
        log.info("Starting Amy Chatbot...")
        # log_handler=None: discord.py's records already reach the handlers logs.py set up
        bot.run(discord_token, log_handler=None)
    except KeyboardInterrupt:
        log.info("Bot interrupted by user (Ctrl+C)")
    except Exception as e:
        log.exception(f"Fatal error: {e}")
        exit(1)
