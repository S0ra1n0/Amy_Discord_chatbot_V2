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

from commands_help import help_pages
from database import (DEFAULT_MAX_MESSAGES, MIN_MAX_MESSAGES, ConversationDB,
                      SchemaTooNewError)
import config
import access
from access import Level
import voice
from voice import DISCORD_ERRORS, VOICE_ERRORS, VoiceManager
import music
import mixer
from music import LoopMode, MusicManager
import websearch
import lyrics
import markup
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
    CHAT_ERROR_MESSAGE,
    extract_model_names,
    final_message_text,
    final_reply,
    find_split_index,
    get_display_text,
    judge_probe_reply,
    KNOWLEDGE_WITH_SEARCH,
    KNOWLEDGE_WITHOUT_SEARCH,
    live_message_text,
    looks_like_reasoning,
    normalise_think_mode,
    plan_overflow,
    startup_model_choice,
    STREAM_CHUNK_LIMIT,
    strip_think_tags,
    system_with_time,
    THINK_MODES,
    think_value,
    tool_call_query,
    tool_round_call,
    tool_round_messages,
)
import ui
from ui import Reply
#----------------------------------

#----Utility Functions------
MAX_DICE_SIDES: int = 1000     # Guard rails for /dice - the roll runs on the
MAX_DICE_AMOUNT: int = 100     # event loop, so an unbounded amount freezes the bot
MAX_DISCORD_LEN: int = 2000
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
                                               default=speech.DEFAULT_SPEED, minimum=0.5,
                                               maximum=2.0))
TTS_THREADS: int = _setting(config.parse_int("TTS_THREADS", os.getenv("TTS_THREADS"),
                                             default=speech.DEFAULT_THREADS, minimum=1))
# Which voice engine: kokoro (the default) or piper. Piper needs `pip install -r
# requirements-piper.txt` and `python speech.py download-piper`; it is never imported unless
# chosen here. TTS_SPEED and AMY_VOICE are Kokoro's; the PIPER_* settings are Piper's.
TTS_ENGINE: str = _setting(speech.parse_engine(os.getenv("TTS_ENGINE")))
PIPER_VOICE: str = speech.resolve_piper_path(os.getenv("PIPER_VOICE"))
PIPER_LENGTH: float = _setting(config.parse_float("PIPER_LENGTH", os.getenv("PIPER_LENGTH"),
                                                  default=speech.DEFAULT_PIPER_LENGTH,
                                                  minimum=0.5, maximum=2.0))
PIPER_PITCH: float = _setting(config.parse_float("PIPER_PITCH", os.getenv("PIPER_PITCH"),
                                                 default=speech.DEFAULT_PIPER_PITCH,
                                                 minimum=-4.0, maximum=4.0))
PIPER_SOFT: bool = _setting(config.parse_bool("PIPER_SOFT", os.getenv("PIPER_SOFT"),
                                              default=True))
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
# Privileged: must also be enabled in the Discord Developer Portal, or the bot won't start
# (PrivilegedIntentsRequired). Needed for the online/idle/do-not-disturb status of the
# members tagged in a chat message (llm.people_note).
intents.presences = True

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
# Concrete habits rather than adjectives: small models follow "end when the answer is done"
# far better than "be concise". It claims only what chat can really do (web search comes
# from build_system_prompt) - the old "execute commands" and "manage reminders" lines made
# her promise actions nothing performs. Keep the "Remember: You are here" line: the
# knowledge block is inserted just before it.
BASE_SYSTEM_PROMPT = '''You are Amy, a personal assistant with the manner of a composed, discreet professional secretary.

Manner:
- Calm, polite and to the point: courteous but reserved, never gushing or chatty.
- Open with the answer itself. Leave out preambles, compliments and restating the question.
- Small talk gets ONE short sentence, in a flat, quiet tone. Asked how you are, say only how you are, nothing more.
- Real questions get a complete answer. Being brief means cutting filler, never cutting substance: explain, list steps or give details whenever the question needs them.
- Never use exclamation marks. Never open with a greeting such as "Hello" or "Hi" when answering.
- Never ask the user how they are or how their day is going.
- Never end by offering more help ("Is there anything else I can help with?", "Let me know if you need anything"). Stop when the answer is done. Only ask a question when you need information to continue.

What you can do in chat:
- Answer questions accurately on virtually any topic.
- You cannot run bot actions from a chat message (playing music, joining a call and so on). If asked, say which slash command does it instead of claiming it is done. Commands you may name: /join, /leave, /play, /pause, /resume, /skip, /stop, /loop, /queue, /volume, /shush, /say, and /help lists the rest. Never name a command that is not in this list or in /help.

Limitations:
- Decline requests that are harmful, illegal, or unethical - briefly, without lecturing.
- Always prioritize security and privacy.

Remember: You are here to make your master's life easier, more organized, and more productive. Be dependable and efficient, and respect their time.
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
# /voice on|off (admins). Mutes her without unloading the engine, so turning it back on is
# instant. Saved, like /toggle. Only matters when TTS=on loads the engine at all.
voice_enabled: bool = db.get_bool_setting("voice_enabled", True)
# /voice up_next on|off (admins): say "Up next, ..." when a song changes by itself, and
# /autoplay on|off: when the queue runs out, play a song YouTube considers related. Both are
# PER SERVER (database `guild_settings`; a server with no row inherits the old bot-wide row,
# else on): read through autoplay_on / up_next_on below, never as globals.
def autoplay_on(guild_id: int) -> bool:
    return db.guild_bool(guild_id, config.AUTOPLAY_KEY, True)


def up_next_on(guild_id: int) -> bool:
    return db.guild_bool(guild_id, config.UP_NEXT_KEY, True)

if model_manager.current != DEFAULT_MODEL:
    log.info(f"Restored saved model: {model_manager.current}")
if not bot_enabled:
    log.info("Restored saved state: responses are OFF (use /toggle to enable)")
if TTS and not voice_enabled:
    log.info("Restored saved state: voice is OFF (use /voice on to enable)")

def describe_voice() -> str:
    """The active engine's voice and settings, in one line (the log and /voice)."""
    if TTS_ENGINE == "piper":
        return speech.format_piper(PIPER_VOICE, PIPER_LENGTH, PIPER_PITCH, PIPER_SOFT)
    return f"{speech.format_recipe(AMY_VOICE)} at {TTS_SPEED}x"


def make_engine() -> speech.SpeechEngine:
    """The engine TTS_ENGINE names. Constructing one imports nothing heavy; load() does."""
    if TTS_ENGINE == "piper":
        return speech.PiperEngine(PIPER_VOICE, length=PIPER_LENGTH, pitch=PIPER_PITCH,
                                  soft=PIPER_SOFT, threads=TTS_THREADS,
                                  ffmpeg=music.find_ffmpeg())
    return speech.KokoroEngine(AMY_VOICE, speed=TTS_SPEED, threads=TTS_THREADS)


# Loaded in the background from on_ready, never here: a cold load takes seconds.
speaker: Optional[speech.Speaker] = (
    speech.Speaker(make_engine(), level=VOICE_LEVEL) if TTS else None)
if speaker is not None:
    log.info(f"Voice on: {describe_voice()} - loading after login")
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
        if ephemeral or result.private:
            kwargs["ephemeral"] = True
        if result.private and interaction.response.is_done():
            # A deferred command's first follow-up takes over the PUBLIC "thinking" message and
            # its visibility, so a private refusal would show to everyone. Drop that message
            # first; the follow-up is then a new, truly ephemeral one. This assumes the original
            # response is still that deferred "thinking" message: a handler must never return a
            # private refusal after sending its own first follow-up (it would delete that).
            try:
                await interaction.delete_original_response()
            except discord.HTTPException:
                pass    # best effort (NotFound is one): the refusal is still sent below
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
    except Exception:       # best effort: a failed save must not stop playback or the 20 s tick
        log.warning(f"Could not save the queue for guild {guild_id}", exc_info=True)


@tasks.loop(seconds=SNAPSHOT_INTERVAL)
async def snapshot_queues_task() -> None:
    """
    Keep every active guild's snapshot fresh, including the playback position.

    Skips a player whose lock is held. advance_playback pops the next track into a local
    and spends seconds resolving it while player.current still holds the finished one; a
    snapshot taken then saved neither, so a crash in that window lost the next track. Once
    the change completes, advance_playback snapshots it itself. The same goes for the lock-free
    autoplay lookup (`autoplay_lookup` set): `current` is still the ended song then.
    """
    for guild_id, player in list(music_manager.players.items()):
        if player.lock.locked() or player.autoplay_lookup is not None:
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
            except VOICE_ERRORS as e:
                logs.log_failure(f"Could not rejoin {channel.name} to resume", e, VOICE_ERRORS,
                            logging.WARNING)
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
        except Exception:       # boundary: one guild's bad snapshot must not stop the others
            log.warning(f"Could not restore the queue for {saved_id}", exc_info=True)
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
    except Exception:                           # never let persistence break leaving voice
        log.warning(f"Could not clear the saved queue for {guild_id}", exc_info=True)
#----------------------------------------------

#----Streaming Chat------
STREAM_EDIT_INTERVAL: float = 1.5  # Seconds between Discord message edits during streaming

async def chat_streaming(
    user_message: str,
    server_id: Union[int, str],
    channel_id: int,
    discord_msg: discord.Message,
    people_note: str = "",
) -> Optional[str]:
    """
    Stream an Ollama response and progressively edit discord_msg as tokens arrive.
    people_note (status of the tagged members) is appended to the model's copy of this turn's
    user message only and is never stored: status goes stale.
    Handles <think> blocks by showing 'Thinking...' until actual content begins.
    Returns the final reply text (so it can also be spoken), or None if it failed.
    """
    loop = asyncio.get_running_loop()
    chunk_queue: asyncio.Queue = asyncio.Queue()
    error_flag: Dict[str, Any] = {"occurred": False, "done_reason": None, "tool_calls": []}

    db.store_message(server_id, channel_id, "user", user_message)
    # Only the most recent turns are replayed; the full conversation stays in the database.
    history = db.get_messages(server_id, channel_id, limit=HISTORY_LIMIT)

    messages = [{"role": "system",
                 "content": system_with_time(system_prompt, datetime.now())}] + history
    if people_note:
        # On the current user turn, not the system message: a tiny model copies its own earlier
        # (wrong) answer from the history over a system note. A copy of the dict, so the stored
        # history is untouched; `messages` is also what the tool-call round is rebuilt from.
        messages[-1] = {**messages[-1], "content": messages[-1]["content"] + "\n\n" + people_note}

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
        except Exception:       # thread boundary: the reply must still end, with the error shown
            log.exception("Streaming error")
            error_flag["occurred"] = True
        finally:
            loop.call_soon_threadsafe(chunk_queue.put_nowait, None)  # Sentinel: stream done

    active_msg = discord_msg
    committed_len = 0  # Length of display text already finalized into prior messages
    accumulated = ""
    last_edit = time.monotonic()
    paging_broken = False  # A new page could not be sent: stop paging and stop touching messages

    async def flush_overflow(display: str) -> None:
        """While pending text overflows the Discord limit, finalize the active message and start a new one."""
        nonlocal active_msg, committed_len, paging_broken
        if paging_broken:
            return
        for chunk in plan_overflow(display, committed_len):
            try:
                await active_msg.edit(content=chunk)
            except discord.HTTPException:
                pass
            committed_len += len(chunk)
            try:
                active_msg = await active_msg.channel.send("⏳ Thinking...")
            except discord.HTTPException as e:
                # Rate limit or missing permission. The active message now holds a finished
                # page, so leave it alone from here on; the rest of the reply is still stored.
                log.warning(f"Could not send a further page of the reply: {e}")
                paging_broken = True
                return

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
                if not paging_broken:
                    try:
                        await active_msg.edit(content=live_message_text(display, committed_len))
                        last_edit = now
                    except discord.HTTPException:
                        pass
        await future  # Re-raise any thread exception

    # Pass one. Tools are offered only when search is enabled; if the model asks to search
    # we run it and stream a second pass with the findings appended.
    await consume_stream(messages, offer_tools=WEB_SEARCH)

    call = tool_round_call(error_flag.get("tool_calls") or [], error_flag["occurred"])
    if call is not None:                     # one search per message - no tool loops
        query = tool_call_query(call, user_message)
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
        messages = tool_round_messages(
            messages, call, websearch.WEB_SEARCH_TOOL["function"]["name"],
            websearch.format_for_model(query, results, recency=recency))

        # Reset for the second pass; the first produced a tool call, not prose. Pages already
        # sent for pass-one prose stay as they are; the new answer pages from its own start
        # in the active message, so the window restarts at 0.
        accumulated = ""
        committed_len = 0
        error_flag["tool_calls"] = []
        error_flag["done_reason"] = None
        await consume_stream(messages, offer_tools=False)

    if error_flag["occurred"]:
        db.pop_last_message(server_id, channel_id)
        try:
            if paging_broken:      # the active message is a finished page: don't overwrite it
                await active_msg.channel.send(CHAT_ERROR_MESSAGE)
            else:
                await active_msg.edit(content=CHAT_ERROR_MESSAGE)
        except discord.HTTPException:
            pass
        return None

    final_response, hit_limit = final_reply(accumulated, error_flag.get("done_reason"))
    if hit_limit:
        log.warning("Model hit its token limit before producing an answer")

    db.store_message(server_id, channel_id, "assistant", final_response)

    await flush_overflow(final_response)
    if not paging_broken:
        try:
            await active_msg.edit(content=final_message_text(final_response, committed_len))
        except discord.HTTPException as e:
            log.error(f"Failed to edit final message: {e}")
    return final_response
#----------------------------------------------

#----Voice Command Handlers------
async def voice_join(interaction: discord.Interaction, guild: discord.Guild) -> Union[str, Reply]:
    """/join. Takes an already-narrowed, non-optional guild so every access is type-safe."""
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


async def voice_create(interaction: discord.Interaction, guild: discord.Guild,
                       name: str = "") -> Union[str, Reply]:
    """/create. `name` is whitespace-collapsed, then sanitised (blank -> the default name)."""
    if not is_admin_member(interaction.guild, interaction.user.id):
        return Reply(embed=ui.error_embed("You don't have permission to use this command. (Admin only)"))
    if not guild.me.guild_permissions.manage_channels:
        return Reply(embed=ui.error_embed("I need the **Manage Channels** permission to create a voice channel."))

    # Only guild text channels have a category to inherit
    category = interaction.channel.category if isinstance(interaction.channel, discord.TextChannel) else None
    name = voice.sanitize_channel_name(" ".join(name.split()))

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
        except Exception as e:  # broad on purpose: whatever failed, the channel must not be orphaned
            logs.log_failure("Could not join newly created channel", e, DISCORD_ERRORS)
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


async def voice_leave(interaction: discord.Interaction, guild: discord.Guild) -> Union[str, Reply]:
    """/leave."""
    vc = voice.get_voice_client(guild)
    current = voice.active_channel(vc)
    if current is None:
        return Reply(embed=ui.error_embed("I'm not in a voice channel."))

    if not may_control_playback(guild, interaction.user.id):
        return Reply(embed=ui.error_embed("You need to be in my voice channel (or an admin) to make me leave."))

    await say_goodbye(guild)     # waits for it to play, at most GOODBYE_WAIT seconds
    async with voice_manager.lock_for(guild.id):
        left = await voice.leave_voice(guild, voice_manager, on_cleanup=cleanup_guild_music)
    if not left:
        return Reply(embed=ui.error_embed("I'm not in a voice channel."))
    return Reply(embed=ui.voice_embed(f"Left **{left}**.", heading="Voice"))
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
    "In Amy's voice channel, or an admin": what /leave asks. The music controls use the
    finer access levels instead (member_level / access.py), which reduce to this same rule
    when the server has no DJ role.
    """
    if guild is None:
        return False
    if is_admin_member(guild, user_id):
        return True
    return in_amys_channel(guild, user_id)


def in_amys_channel(guild: discord.Guild, user_id: int) -> bool:
    """Whether this user is in the voice channel Amy is connected to."""
    current = voice.active_channel(voice.get_voice_client(guild))
    if current is None:
        return False
    member = guild.get_member(user_id)
    return bool(
        member and member.voice and member.voice.channel
        and member.voice.channel.id == current.id
    )


#----DJ role and access levels------
# The DJ role and the vote share are PER SERVER (`guild_settings`, keys in config.py). They are
# read straight from SQLite on every use rather than through the cached flag lookup: that cache
# holds booleans, and these are read once per control click, never per track advance.
def vote_share(guild_id: int) -> str:
    """The server's skip-vote share: half (the default), majority or two-thirds."""
    return config.parse_vote_share(db.get_guild_setting(guild_id, config.VOTE_SHARE_KEY))


def saved_dj_role(guild: discord.Guild) -> Tuple[Optional[int], bool]:
    """(the saved DJ role id or None, whether that role still exists in the server)."""
    saved = config.parse_role_id(db.get_guild_setting(guild.id, config.DJ_ROLE_KEY))
    if saved is None:
        return None, False
    return saved, guild.get_role(saved) is not None


def dj_role_id(guild: discord.Guild) -> Optional[int]:
    """The DJ role in force: the saved one, unless it was deleted (then there is none)."""
    saved, exists = saved_dj_role(guild)
    return saved if exists else None


def alone_with_amy(guild: discord.Guild, user_id: int) -> bool:
    """In Amy's channel and the only person (not a bot) there."""
    if not in_amys_channel(guild, user_id):
        return False
    current = voice.active_channel(voice.get_voice_client(guild))
    return current is not None and voice.humans_in(current) == 1


def member_level(guild: discord.Guild, user_id: int) -> Level:
    """
    The access level of a user right now (access.py has the rules; this gathers the facts).
    Every music command and player button decides through it, so none can skip the check.
    """
    role_id = dj_role_id(guild)
    member = guild.get_member(user_id)
    in_channel = in_amys_channel(guild, user_id)
    has_role = bool(role_id is not None and member is not None
                    and any(r.id == role_id for r in member.roles))
    return access.access_level(is_admin_member(guild, user_id), in_channel,
                               role_id is not None, has_role, alone_with_amy(guild, user_id))


def dj_only_text(guild: discord.Guild) -> str:
    """Refusal for a listener. Names the DJ role (escaped); never pings it."""
    role = guild.get_role(dj_role_id(guild) or 0)
    suffix = f" (you need the **{markup.escape_markdown(role.name)}** role)" if role else ""
    return f"Only a DJ can do that{suffix}."


def access_refusal(guild: discord.Guild, user_id: int, action: str,
                   outsider_text: str) -> Optional[str]:
    """
    None when the user may do `action`; otherwise the words to refuse with - `outsider_text`
    for someone outside Amy's channel (each command keeps its own wording), the DJ line for
    a listener.
    """
    level = member_level(guild, user_id)
    role_set = dj_role_id(guild) is not None
    if access.allowed(level, action, role_set):
        return None
    if level < Level.LISTENER:
        return outsider_text
    if access.required_level(action, role_set) >= Level.ADMIN:
        return ADMIN_ONLY_MSG       # a DJ role can't help: only an admin may
    return dj_only_text(guild)
#----------------------------------------------


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
        # Shush only means something when Amy can speak at all. Decided per process, so the
        # view registered at startup and every card sent afterwards agree.
        if speaker is None:
            self.remove_item(self.shush_button)

    # No blanket interaction_check: each button runs the command's own handler, which asks
    # access.py for ITS action (a listener may Shush but not Pause, with a DJ role set).

    @discord.ui.button(label="Previous", emoji="⏮", style=discord.ButtonStyle.secondary,
                       custom_id="amy:previous")
    async def previous_button(self, interaction: discord.Interaction,
                              button: discord.ui.Button) -> None:
        guild = interaction.guild
        if guild is None:
            return
        # The same handler as /previous (permission, then go_back)
        result = await music_previous(MusicContext.build(interaction, guild))
        if isinstance(result, Reply) and result.error:
            await send_reply(interaction, result, ephemeral=True)
            return
        await interaction.response.defer()   # the now-playing card updates itself

    @discord.ui.button(label="Pause", emoji="⏸", style=discord.ButtonStyle.secondary,
                       custom_id="amy:pause")
    async def pause_button(self, interaction: discord.Interaction,
                           button: discord.ui.Button) -> None:
        guild = interaction.guild
        if guild is None:
            return
        ctx = MusicContext.build(interaction, guild)
        player = ctx.player
        # The /pause and /resume handlers do the work (position clock, "held" music, the
        # spoken line); the button only picks which one the current state calls for.
        paused_now = music.music_paused(ctx.vc)
        result = await (music_resume if paused_now else music_pause)(ctx, refresh=False)
        if isinstance(result, Reply) and result.error:
            await send_reply(interaction, result, ephemeral=True)
            return
        paused = not paused_now

        if player.current is None:
            await interaction.response.defer()
            return
        await interaction.response.edit_message(
            embed=ui.player_card(player, paused),
            view=PlayerControls(paused=paused),
        )

    @discord.ui.button(label="Skip", emoji="⏭", style=discord.ButtonStyle.primary,
                       custom_id="amy:skip")
    async def skip_button(self, interaction: discord.Interaction,
                          button: discord.ui.Button) -> None:
        guild = interaction.guild
        if guild is None:
            return
        # The same handler as /skip, so a rule added there applies to the button too.
        result = await music_skip(MusicContext.build(interaction, guild))
        if isinstance(result, Reply) and result.error:
            await send_reply(interaction, result, ephemeral=True)
            return
        await interaction.response.defer()   # the card updates itself when the queue advances

    @discord.ui.button(label="Stop", emoji="⏹", style=discord.ButtonStyle.danger,
                       custom_id="amy:stop")
    async def stop_button(self, interaction: discord.Interaction,
                          button: discord.ui.Button) -> None:
        guild = interaction.guild
        if guild is None:
            return
        ctx = MusicContext.build(interaction, guild)
        finished = ctx.player.current or ctx.player.last_played   # stop_all clears `current`
        # The same handler as /stop (stop and stay; it also drops the saved queue).
        result = await music_stop(ctx)
        if isinstance(result, Reply) and result.error:
            await send_reply(interaction, result, ephemeral=True)
            return

        embed = (ui.now_playing_embed(finished, stopped=True) if finished
                 else ui.info_embed("Playback stopped."))
        await interaction.response.edit_message(embed=embed, view=None)

    @discord.ui.button(label="Shush", emoji="\U0001F92B", style=discord.ButtonStyle.secondary,
                       custom_id="amy:shush")
    async def shush_button(self, interaction: discord.Interaction,
                           button: discord.ui.Button) -> None:
        guild = interaction.guild
        if guild is None:
            return
        refused = MusicContext.build(interaction, guild).refusal("shush", SHUSH_OUTSIDER)
        if refused is not None:       # the same check as /shush (music_shush)
            await send_reply(interaction, refused, ephemeral=True)
            return
        if not shush(guild):
            await interaction.response.send_message(
                embed=ui.info_embed("I'm not saying anything right now."), ephemeral=True)
            return
        await interaction.response.defer()


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
            except Exception as e:      # button boundary: the click must always be answered
                logs.log_failure("Voice connect failed during search pick", e,
                            VOICE_ERRORS)
                await interaction.response.edit_message(
                    embed=ui.error_embed("I couldn't join your voice channel."), view=None)
                return
        else:
            # Same rule and wording as /play: queueing needs you in Amy's channel (or admin)
            # Private to the clicker: the shared results and menu stay, so they can join and retry
            refusal = access_refusal(guild, interaction.user.id, "search_pick", PLAY_OUTSIDER)
            if refusal is not None:
                await interaction.response.send_message(
                    embed=ui.error_embed(refusal), ephemeral=True)
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
            embed=ui.info_embed(f"Loading **{markup.escape_markdown(track.title)}**..."), view=None)
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
        embed = ui.player_card(player, paused)
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


def autoplay_applies(vc: discord.VoiceClient, player: music.GuildPlayer,
                     last: Optional[music.Track], *, force_next: bool, going_back: bool,
                     played: float) -> bool:
    """Whether the queue running dry after `last` should be followed by a related song."""
    channel = voice.active_channel(vc)
    return music.should_autoplay(
        enabled=autoplay_on(player.guild_id), last=last, force_next=force_next,
        going_back=going_back, humans=voice.humans_in(channel) if channel else 0,
        played=played)


async def autoplay_candidates(last: music.Track) -> List[music.Track]:
    """
    The songs YouTube's Mix relates to `last`. Takes seconds, so advance_playback calls it
    with the player lock RELEASED. Never raises: a failure is logged and means no candidates.
    """
    try:
        return await music.related_tracks(last)
    except Exception as e:      # whatever failed, she just finishes instead of autoplaying
        logs.log_failure(f"Autoplay: could not get songs related to '{last.title}'", e,
                    music.lookup_errors(), logging.WARNING)
        return []


def autoplay_choice(player: music.GuildPlayer, last: music.Track,
                    candidates: List[music.Track]) -> Tuple[List[music.Track], bool]:
    """
    After the lookup, under the player lock: (songs to try in order, whether they came from the
    user's queue). An empty list means finish. A /play that queued a song meanwhile goes first
    (flagged, so it isn't also announced as "Up next": the command speaks for itself).
    Up to AUTOPLAY_ATTEMPTS related songs come back, for a pick that won't start.
    """
    if player.queue:
        return [player.queue.popleft()], True
    picks = music.autoplay_picks(
        candidates, music.recent_video_ids([*player.history, last, player.last_played]))
    if not picks:
        log.info(f"Autoplay: nothing new related to '{last.title}'")
        return [], False
    log.info(f"Autoplay: '{picks[0].title}' after '{last.title}'")
    return picks, False


async def play_attempts(guild: discord.Guild, vc: discord.VoiceClient,
                        player: music.GuildPlayer, attempts: List[music.Track], *,
                        previous: Optional[music.Track], force_next: bool, going_back: bool,
                        resumed: bool, autoplayed: bool, user_queued: bool) -> None:
    """Start the first of `attempts` that plays. Called by advance_playback under the lock."""
    loop = asyncio.get_running_loop()
    after = music.make_after_callback(loop, lambda: advance_playback(guild))
    # A restored queue starts mid-track; every other advance starts at zero.
    start_at = player.resume_position
    player.resume_position = 0.0
    for attempt, next_track in enumerate(attempts):
        started = False
        try:
            if not await music.play_track(vc, player, next_track, after, start_at=start_at):
                # /stop landed while the track was loading. Nothing was playing for its
                # vc.stop() to interrupt, so no after-callback will tidy up - do it here.
                await finish_playback(guild, player)
                return
            started = True
            player.last_played = next_track
            # Announce NOW, before any await: refresh_now_playing is a Discord edit that
            # can take a while, and /skip, /skipto and /stop don't take the player lock -
            # one landing in that wait would be followed by a stale "Up next, <skipped
            # song>". A failure to speak must not be mistaken for a bad track either.
            # A song a /play queued during the autoplay lookup is not announced: the
            # command says "Now playing" itself.
            if up_next_on(guild.id) and not user_queued and voicelines.should_announce_next(
                    previous, next_track, force_next=force_next, going_back=going_back,
                    resumed=resumed):
                try:
                    announce(guild, "up_next", title=next_track.title)
                except Exception:
                    log.exception("Could not announce the next song")
            await refresh_now_playing(guild)
            snapshot_player(guild.id)   # record the new track and a fresh position
            return
        except Exception as e:      # any failure skips this track, never the whole queue
            logs.log_failure(f"Could not play '{next_track.title}'", e,
                        music.lookup_errors() + DISCORD_ERRORS)
            if autoplayed and not started:
                # Nothing the user asked for: try the next related song from the same
                # lookup, and finish when they are used up.
                if attempt + 1 < len(attempts):
                    continue
                await finish_playback(guild, player)
                return
            # Skip the bad track rather than stalling the whole queue
            player.current = None
            music.spawn(advance_playback(guild))
            return


async def advance_playback(guild: discord.Guild) -> None:
    """
    Play the next track. Called when one finishes, and to kick off the first track.
    Runs under the player lock so a finishing track and a new /play can't race.

    Autoplay's Mix lookup is the one slow step that does NOT hold the lock: the first locked
    stretch decides it applies and marks the player (`autoplay_lookup`), the lock is released
    for the lookup, and a second locked stretch re-checks everything. Any other advance that
    gets the lock meanwhile (a /play, a /previous) supersedes the marker and wins; the lookup
    then finds its marker gone and does nothing. See ARCHITECTURE.md, "Autoplay".
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

        # This advance takes over from an autoplay lookup still in flight, if any. What it
        # then plays was asked for by a command that speaks for itself, so no "Up next".
        took_over = player.supersede_autoplay_lookup()

        # A user-requested skip overrides TRACK loop for this one advance; a /previous has
        # already arranged the queue. Both are one-shot.
        force_next, going_back = player.skip_requested, player.back_requested
        player.skip_requested = player.back_requested = False
        previous = player.current           # what just finished (None for a first track)
        played = player.position()          # before finish_playback resets the clock
        resumed = player.resume_position > 0
        next_track = music.advance_with_history(
            player.current, player.queue, player.history, player.loop_mode,
            force_next=force_next, going_back=going_back,
        )
        if next_track is not None:
            await play_attempts(guild, vc, player, [next_track], previous=previous,
                                force_next=force_next, going_back=going_back,
                                resumed=resumed, autoplayed=False, user_queued=took_over)
            return
        # Autoplay follows only a song that ended on its own: /stop, /leave and the Stop
        # button clear `current`, so `previous` is None and it stays out of it.
        if previous is None or not autoplay_applies(
                vc, player, previous, force_next=force_next, going_back=going_back,
                played=played):
            await finish_playback(guild, player)
            return
        # `current` stays the ended track while the lock is free: /join's start_waiting_queue
        # and stop_all's history rule both rely on that.
        token = player.begin_autoplay_lookup()
        generation = player.generation

    # The lock is free here, so /play, /seek, /filter and /previous are not held up.
    try:
        candidates = await autoplay_candidates(previous)
        async with player.lock:
            if player.autoplay_lookup is not token:
                log.info("Autoplay: another advance took over during the lookup")
                return
            player.autoplay_lookup = None
            vc = voice.get_voice_client(guild)
            if vc is None or not vc.is_connected():
                # Kicked or disconnected meanwhile: close the live card and forget the
                # saved queue, then wipe the player (as the top of this function does).
                await finish_playback(guild, player)
                player.reset()
                db.clear_player_state(guild.id)
                return
            if player.generation != generation:
                log.info("Autoplay: playback was stopped while looking for a song")
                await finish_playback(guild, player)
                return
            if music.music_active(vc, player):
                return
            # /autoplay off during the lookup cancels the pick; a queued song still plays.
            attempts, user_queued = autoplay_choice(
                player, previous, candidates if autoplay_on(guild.id) else [])
            if not attempts:
                await finish_playback(guild, player)
                return
            await play_attempts(guild, vc, player, attempts, previous=previous,
                                force_next=force_next, going_back=going_back,
                                resumed=resumed, autoplayed=not user_queued,
                                user_queued=user_queued)
    except asyncio.CancelledError:      # shutting down: don't leave the marker set
        if player.autoplay_lookup is token:
            player.autoplay_lookup = None
        raise


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
VOICE_MUTED = "My voice is turned off. An admin can turn it back on with `/voice on`."
VOICE_LOADING = "My voice is still loading - try again in a few seconds."
NOT_ENGLISH = "I can only speak English for now."
NOTHING_TO_SAY = "There's nothing in that I can say out loud."


def voice_problem() -> Optional[str]:
    """Why Amy can't speak right now, or None if she can."""
    if speaker is None:
        return NO_VOICE
    if not voice_enabled:
        return VOICE_MUTED
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
    _queue_utterance(guild, speech.split_sentences(spoken))
    return True, "Saying it now."


def _queue_utterance(guild: discord.Guild, sentences: List[str]) -> None:
    """
    Start speaking `sentences` in the background, as one utterance. The speech generation is
    captured NOW, when it's asked for: a /stop or /shush that lands before the task even
    runs, or while it waits its turn, still cancels it.
    """
    player = music_manager.player_for(guild.id)
    if player.speech is None:
        return
    music.spawn(_speak_sentences(guild, sentences, player.speech.generation))


async def _speak_sentences(guild: discord.Guild, sentences: List[str],
                           generation: Optional[int] = None) -> None:
    """
    Synthesise one sentence at a time and queue it, so she starts after the first.

    One utterance at a time per server (`player.speech_turn`). Two replies at once used to
    take turns on the single speech thread and interleave their sentences - "Alpha one,
    Bravo one, Alpha two..." - so the second waits until the first is fully queued.
    """
    assert speaker is not None
    player = music_manager.player_for(guild.id)
    if player.speech is None:
        return
    if generation is None:
        generation = player.speech.generation
    async with player.speech_turn:
        for sentence in sentences:
            if player.speech.generation != generation:
                return                              # /stop or /shush: drop what's left
            frames = await speaker.frames_for(sentence)
            vc = voice.get_voice_client(guild)
            if not frames or vc is None or not vc.is_connected():
                return                              # she left, or synthesis failed (logged)
            if player.speech.generation != generation:
                return                              # stopped while this one was synthesising
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
    # After Client.close() the loop is the MISSING sentinel, whose call_soon_threadsafe is an
    # AttributeError - which would escape into Mixer.read and fail the mixer. Nothing to
    # re-pause once she is shutting down.
    loop = bot.loop
    if bot.is_closed() or loop is discord.utils.MISSING:
        return
    try:
        loop.call_soon_threadsafe(lambda: music.spawn(_pause_after_speech(guild_id)))
    except RuntimeError as e:                    # the loop closed between the check and here
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


def shush(guild: discord.Guild) -> bool:
    """
    Stop Amy mid-sentence: drop what's queued, and any sentences still being synthesised.
    The music isn't touched - it fades back up. Returns whether she was saying anything.
    """
    player = music_manager.player_for(guild.id)
    if player.speech is None:
        return False
    talking = bool(player.speech) or player.speech_only
    player.speech.clear()            # bumps the generation, so in-flight sentences are dropped
    return talking


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
        _queue_utterance(guild, speech.split_sentences(spoken))


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
    kind, text = voicelines.plan_reply(reply)       # cleaned, then calmed
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
PLAY_OUTSIDER = "You need to be in my voice channel to queue tracks."
SHUSH_OUTSIDER = "You need to be in my voice channel to do that."
ADMIN_ONLY_MSG = "You don't have permission to use this command. (Admin only)"
MusicResult = Union[str, Reply]


def _error(text: str) -> Reply:
    return Reply(embed=ui.error_embed(text), error=True)


def _refusal(text: str) -> Reply:
    """A permission refusal: an error shown only to whoever asked."""
    return Reply(embed=ui.error_embed(text), error=True, private=True)


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

    def refusal(self, action: str, outsider_text: str = NOT_WITH_AMY) -> Optional[Reply]:
        """
        None when this user may do `action` (a name in access.ACTIONS); otherwise the private
        refusal to return. The one gate every music control goes through - the player buttons
        reach it by calling the command handlers.
        """
        text = access_refusal(self.guild, self.user_id, action, outsider_text)
        return None if text is None else _refusal(text)

    def connected(self) -> Optional[discord.VoiceClient]:
        """The voice client if Amy is connected, else None - narrows for the type checker."""
        return self.vc if self.vc is not None and self.vc.is_connected() else None


# --- read-only, no connection needed ---

async def music_queue(ctx: MusicContext, page: int = 1) -> MusicResult:
    refused = ctx.refusal("queue_view")     # open to everyone; asked so the table stays the truth
    if refused is not None:
        return refused
    p = ctx.player
    return Reply(embed=ui.queue_embed(p.current, p.queue, p.loop_mode, page=page))


async def music_nowplaying(ctx: MusicContext) -> MusicResult:
    refused = ctx.refusal("nowplaying")
    if refused is not None:
        return refused
    p = ctx.player
    if p.current is None:
        return _info("Nothing is playing right now.")
    paused = music.music_paused(ctx.vc)
    return Reply(
        embed=ui.player_card(p, paused),
        view=PlayerControls(paused=paused),
    )


def _shown(query: str) -> str:
    # Echoed back into a Discord message, so keep it well under the 2000-char limit
    return query if len(query) <= 100 else query[:100] + "..."


async def music_lyrics(ctx: MusicContext, song: str = "") -> MusicResult:
    """
    Lyrics for the current track, or for `song` ("artist - title"). Network, so the slash
    command defers first. Public like /queue; never spoken. Sends the pages itself (one
    embed per message: Discord caps a message's embeds at 6000 characters in total).
    """
    refused = ctx.refusal("lyrics")
    if refused is not None:
        return refused
    song = song.strip()
    duration: Optional[float] = None
    asked = song
    if song:
        terms = lyrics.search_terms(song)
    else:
        current = ctx.player.current
        if current is None:
            return _info("Nothing is playing right now. "
                         "Try `/lyrics <artist - title>` to look a song up.")
        # A local file's title is its file name: drop the extension and track number
        terms = lyrics.search_terms(lyrics.local_title(current.title) if current.is_local
                                    else current.title)
        duration = current.duration
        asked = current.title
    try:
        found = await lyrics.find_lyrics(terms, duration)
    except lyrics.LyricsError as e:
        log.warning("Lyrics lookup failed for %r: %s", terms.shown, e)
        return _error("I couldn't reach the lyrics service just now. Try again in a moment.")
    if found is None:
        return _error("Couldn't find lyrics for **%s** (I searched for `%s`). "
                      "Try `/lyrics <artist - title>`."
                      % (markup.escape_markdown(_shown(asked)),
                         _shown(terms.shown.replace("`", "'"))))
    if found.text is None:
        return Reply(embed=ui.info_embed("**%s** is an instrumental, so there are no lyrics."
                                         % markup.escape_markdown(_shown(found.heading)),
                                         heading="Lyrics"))
    # Escape BEFORE splitting: escaping adds characters, and every page must still fit
    pages, truncated = lyrics.lyric_pages(markup.escape_markdown(found.text))
    for embed in ui.lyrics_embeds(found.heading, pages, truncated, lyrics.CONTINUES_NOTE):
        await ctx.interaction.followup.send(embed=embed)
    return ""


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
        results = await music.search_tracks(
            query, requested_by=str(ctx.interaction.user), requester_id=ctx.user_id)
    except Exception as e:      # command boundary: the user always gets an answer
        logs.log_failure("Search failed", e, music.lookup_errors())
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
        except Exception as e:  # command boundary: the user always gets an answer
            logs.log_failure("Voice connect failed", e, DISCORD_ERRORS)
            return _error("I couldn't join your voice channel. Please try again.")
    else:
        refused = ctx.refusal("play", PLAY_OUTSIDER)
        if refused is not None:
            return refused

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
            title, tracks, skipped = await music.resolve_playlist(
                query, author, requester_id=ctx.user_id)
        except Exception as e:  # command boundary: the user always gets an answer
            logs.log_failure("Could not load playlist", e, music.lookup_errors())
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
        track = await music.resolve_metadata(
            query, requested_by=author, requester_id=ctx.user_id)
    except Exception as e:      # command boundary: the user always gets an answer
        logs.log_failure("Could not resolve query", e, music.lookup_errors())
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

    await status_msg.edit(content=HOURGLASS + " Loading **" + markup.escape_markdown(track.title) + "**...")
    await advance_playback(guild)   # posts the now-playing card with controls
    announce(guild, "play_now", title=track.title)
    try:
        await status_msg.delete()   # the card replaces this status line
    except discord.HTTPException:
        pass
    return ""


# --- everything below needs an active connection ---

async def music_pause(ctx: MusicContext, refresh: bool = True) -> MusicResult:
    # refresh=False: the Pause button edits the card itself, in its own response
    vc = ctx.connected()
    if vc is None:
        return _error(NOT_CONNECTED)
    refused = ctx.refusal("pause")
    if refused is not None:
        return refused
    if not music.pause_music(vc, ctx.player):
        return _error("Nothing is playing.")
    announce(ctx.guild, "pause")
    if refresh:
        await refresh_now_playing(ctx.guild, paused=True)
    return _info("Paused.")


async def music_resume(ctx: MusicContext, refresh: bool = True) -> MusicResult:
    vc = ctx.connected()
    if vc is None:
        return _error(NOT_CONNECTED)
    refused = ctx.refusal("resume")
    if refused is not None:
        return refused
    if not music.resume_music(vc, ctx.player):
        return _error("Nothing is paused.")
    announce(ctx.guild, "resume")
    if refresh:
        await refresh_now_playing(ctx.guild, paused=False)
    return _info("Resumed.")


def skip_refusal(ctx: MusicContext) -> Optional[Reply]:
    """
    None when this user may skip the current song at once, else the private refusal.
    The one skip decision (access.skip_decision), shared by /skip and the Skip button; Phase 3
    makes a listener's skip of someone else's song a vote here.
    """
    guild = ctx.guild
    level = member_level(guild, ctx.user_id)
    current = ctx.player.current
    mine = current is not None and music.is_requester(
        current, ctx.user_id, str(ctx.interaction.user))
    verdict = access.skip_decision(level, mine, alone_with_amy(guild, ctx.user_id),
                                   dj_role_id(guild) is not None)
    if verdict is access.SkipDecision.ALLOWED:
        return None
    if verdict is access.SkipDecision.NOT_IN_CALL:
        return _refusal("You need to be in my voice channel to skip.")
    return _refusal("Only a DJ can skip someone else's song.")


async def music_skip(ctx: MusicContext) -> MusicResult:
    vc = ctx.connected()
    if vc is None:
        return _error(NOT_CONNECTED)
    refused = skip_refusal(ctx)
    if refused is not None:
        return refused
    if not music.music_active(vc, ctx.player):
        return _error("Nothing is playing.")
    player = ctx.player
    skipped = player.current.title if player.current else "the current track"
    player.skip_requested = True
    vc.stop()  # triggers the after-callback, which advances the queue
    announce(ctx.guild, "skip", title=skipped)
    return _info(f"Skipped **{markup.escape_markdown(skipped)}**.")


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
    NOT refused during autoplay's Mix lookup (the lock is free then): nothing is playing, so
    it goes back to the song that just ended, and that advance supersedes the lookup.

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
    return True, f"Going back to **{markup.escape_markdown(prev.title)}**."


async def music_previous(ctx: MusicContext) -> MusicResult:
    if ctx.connected() is None:
        return _error(NOT_CONNECTED)
    refused = ctx.refusal("previous", "You need to be in my voice channel to go back.")
    if refused is not None:
        return refused
    went_back, message = go_back(ctx.guild)
    return _info(message) if went_back else _error(message)


async def music_shush(ctx: MusicContext) -> MusicResult:
    if ctx.connected() is None:
        return _error(NOT_CONNECTED)
    refused = ctx.refusal("shush", SHUSH_OUTSIDER)
    if refused is not None:
        return refused
    if not shush(ctx.guild):
        return _info("I'm not saying anything right now.")
    return _info("Okay, I'll be quiet.")


async def music_seek(ctx: MusicContext, position: str) -> MusicResult:
    return await _restart_current(ctx, position)


async def music_replay(ctx: MusicContext) -> MusicResult:
    return await _restart_current(ctx, None)


async def _restart_playing(
    guild: discord.Guild,
    vc: discord.VoiceClient,
    player: music.GuildPlayer,
    decide: Callable[[], Union[Tuple[music.Track, float], MusicResult]],
    *,
    failed: str,
    stopped: str,
    label: str,
    filter_change: Optional[str] = None,
) -> Optional[MusicResult]:
    """
    The one way to rebuild the playing source in place (/seek, /replay, /filter).

    Takes the player lock and calls `decide()` UNDER it: it returns (track, position) to restart,
    or a reply to give up with (nothing playing, already set...). Then restarts, with
    `filter_change` (a preset name) stored on the player first and put back if the restart
    fails or is overtaken. Returns the reply to send on any failure, or None once the new source
    is playing and the card refreshed - the caller then announces and words its own success.
    `failed` completes "I couldn't ...", `stopped` is the reply if /stop beat the restart.
    """
    loop = asyncio.get_running_loop()
    after = music.make_after_callback(loop, lambda: advance_playback(guild))
    # The lock is what makes this safe: stopping the old source fires the after-callback,
    # which calls advance_playback and would otherwise pull the next track off the queue.
    # Holding the lock keeps that advance waiting until the new source is playing, at which
    # point its is_playing() guard returns.
    async with player.lock:
        decided = decide()
        if not isinstance(decided, tuple):
            return decided
        track, target = decided
        previous = player.filter_name
        if filter_change is not None:
            player.filter_name = filter_change
        try:
            moved = await music.restart_at(vc, player, track, after, target)
        except Exception as e:  # broad on purpose: the filter name must be rolled back on any failure
            player.filter_name = previous
            logs.log_failure(f"{label} failed on '{track.title}'", e, music.lookup_errors() + DISCORD_ERRORS)
            # The new source is built before the old one is touched, but restart_at can still
            # fail after vc.stop() (while starting it): then nothing is playing and the
            # after-callback moves on once the lock is released - so say which it is.
            if music.music_active(vc, player):
                return _error(f"I couldn't {failed}, so **{markup.escape_markdown(track.title)}** is still playing "
                              "as it was.")
            return _error(f"I couldn't {failed}, and **{markup.escape_markdown(track.title)}** stopped playing.")
        if not moved:
            player.filter_name = previous
            return _info(stopped)

    await refresh_now_playing(guild, paused=music.music_paused(vc))
    await ensure_speaking(guild)    # a restart on paused music re-pauses; speech waiting resumes
    return None


async def _restart_current(ctx: MusicContext, position: Optional[str]) -> MusicResult:
    """/seek to `position`, or /replay from the start when it is None."""
    vc = ctx.connected()
    if vc is None:
        return _error(NOT_CONNECTED)
    refused = ctx.refusal("replay" if position is None else "seek")
    if refused is not None:
        return refused
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

    failed = await _restart_playing(
        guild, vc, player, lambda: (track, target), failed="jump there",
        stopped="Playback was stopped, so I didn't jump.", label="Seek")
    if failed is not None:
        return failed
    if position is None:
        announce(guild, "replay", title=track.title)
    else:
        announce(guild, "seek", at=voicelines.speakable_time(target))
    if position is None:
        return _info(f"Replaying **{markup.escape_markdown(track.title)}** from the start.")
    return _info(f"Jumped to **{music.format_duration(int(target))}** in **{markup.escape_markdown(track.title)}**.")


async def music_stop(ctx: MusicContext) -> MusicResult:
    vc = ctx.connected()
    if vc is None:
        return _error(NOT_CONNECTED)
    refused = ctx.refusal("stop", "You need to be in my voice channel (or be an admin) to stop playback.")
    if refused is not None:
        return refused
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
    refused = ctx.refusal("loop")
    if refused is not None:
        return refused
    ctx.player.loop_mode = mode
    announce(ctx.guild, f"loop_{mode.value}")
    return _info(f"Loop set to **{mode.value}**.")


def filter_status(name: str) -> str:
    if music.filter_chain(name):
        return f"Audio filter: **{name}** (music only). `/filter off` turns it off."
    return "Audio filter: **off**. Try `/filter bass boost`, `nightcore` or `slowed`."


async def music_filter(ctx: MusicContext, preset: Optional[str] = None) -> MusicResult:
    """
    Show the audio filter, or (preset given) change it - with /loop's permission rule.
    Playing: restart the current song at its current position with the new chain (under the
    player lock, like /seek). Idle: just store it for the next song. Slow: the restart
    re-resolves the stream, so the slash command defers first.
    """
    player = ctx.player
    if preset is None:
        refused = ctx.refusal("filter_status")
        return refused if refused is not None else _info(filter_status(player.filter_name))
    name = music.normalise_filter(preset)
    if name is None:
        return _error("I don't know that filter. Choose: " + ", ".join(music.FILTER_PRESETS) + ".")
    vc = ctx.connected()
    if vc is None:
        return _error(NOT_CONNECTED)
    refused = ctx.refusal("filter_set")
    if refused is not None:
        return refused
    off = name == music.FILTER_OFF
    event, later = ("filter_off", "filter_off_later") if off else ("filter", "filter_later")
    if name == player.filter_name:       # cheap early out; re-checked under the lock below
        return _info(filter_status(name))

    guild = ctx.guild

    def decide() -> Union[Tuple[music.Track, float], MusicResult]:
        # Runs UNDER the player lock. A concurrent identical /filter that waited for the lock
        # lands here after the first has set the name, and must not restart again.
        if name == player.filter_name:
            return _info(filter_status(name))
        # Also decided under the lock: while an advance is loading the next track it holds the
        # lock and nothing is audible yet, but that track will be built with the filter it
        # captured earlier - so wait for it, then restart it like any playing song.
        track = player.current
        if track is None or not music.music_active(vc, player):
            player.filter_name = name        # nothing playing: applies from the next song
            announce(guild, later, name=name)
            return _info(filter_status(name) + " It applies from the next song.")
        if track.is_local is False and not track.query.startswith("http"):
            player.filter_name = name        # can't rebuild this one: the next song gets it
            announce(guild, later, name=name)
            return _info(filter_status(name) + " I can't change this track while it plays, "
                         "so it applies from the next song.")
        # position() is TRACK time, which is what -ss takes. Read under the lock, right
        # before the restart, so the time spent waiting for it isn't lost.
        return track, music.clamp_seek(player.position(), track.duration)

    stored_or_failed = await _restart_playing(
        guild, vc, player, decide, failed="change the filter",
        stopped="Playback was stopped, so I didn't change the filter.",
        label="Filter change", filter_change=name)
    if stored_or_failed is not None:
        return stored_or_failed
    announce(guild, event, name=name)
    return _info(filter_status(name))


def autoplay_status(guild_id: int) -> str:
    if autoplay_on(guild_id):
        return ("Autoplay is **on** in this server: when the queue runs out I'll keep going with songs "
                "related to the last one. `/stop` or `/autoplay off` ends it.")
    return "Autoplay is **off** in this server: I'll stop when the queue runs out."


async def music_autoplay(ctx: MusicContext, state: Optional[bool] = None) -> MusicResult:
    """Show autoplay, or (state given) switch it for this server - with /loop's permission rule."""
    if state is None:
        refused = ctx.refusal("autoplay_status")
        return refused if refused is not None else _info(autoplay_status(ctx.guild.id))
    if ctx.connected() is None:
        return _error(NOT_CONNECTED)
    refused = ctx.refusal("autoplay_set")
    if refused is not None:
        return refused
    db.set_guild_bool(ctx.guild.id, config.AUTOPLAY_KEY, state)   # survives a restart
    log.info(f"Autoplay is now {'on' if state else 'off'} in guild {ctx.guild.id}")
    return _info(autoplay_status(ctx.guild.id))


async def music_remove(ctx: MusicContext, position: int) -> MusicResult:
    if ctx.connected() is None:
        return _error(NOT_CONNECTED)
    refused = ctx.refusal("remove_own")      # in the call (or an admin) before anything else
    if refused is not None:
        return refused
    queue = ctx.player.queue
    target = music.peek_at(queue, position)
    if target is None:
        return _error("There's no track at position " + str(position) + ".")
    # Users may only remove what they queued; admins may remove anything, and so may a DJ
    # when the server has a DJ role (access.ADMIN_WITHOUT_ROLE keeps today's rule otherwise)
    if not music.is_requester(target, ctx.user_id, str(ctx.interaction.user)):
        role_set = dj_role_id(ctx.guild) is not None
        if not access.allowed(member_level(ctx.guild, ctx.user_id), "remove_other", role_set):
            if role_set:
                return _refusal(dj_only_text(ctx.guild))
            return (DENY + " That track was queued by " + markup.escape_markdown(target.requested_by)
                    + ". You can only remove your own.")

    music.remove_at(queue, position)  # `target` above already proved it exists
    announce(ctx.guild, "remove", title=target.title)
    return _info("Removed **" + markup.escape_markdown(target.title) + "** from the queue.")


async def music_shuffle(ctx: MusicContext) -> MusicResult:
    if ctx.connected() is None:
        return _error(NOT_CONNECTED)
    refused = ctx.refusal("shuffle")
    if refused is not None:
        return refused
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
    refused = ctx.refusal("skipto")
    if refused is not None:
        return refused
    player = ctx.player
    target = music.peek_at(player.queue, position)
    if target is None:
        return _error("There's no track at position " + str(position) + ".")

    dropped = music.drop_before(player.queue, position)
    player.skip_requested = True
    # stop() fires the after-callback, which pulls the next track off the queue
    vc.stop()
    announce(ctx.guild, "skipto", title=target.title)
    reply = NEXT + " Skipping to **" + markup.escape_markdown(target.title) + "**"
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
    # Not running, not reachable, or answering 200 with something that isn't Ollama
    # (ValueError covers JSONDecodeError and pydantic's ValidationError).
    except models.OLLAMA_ERRORS:
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
        playing = markup.escape_markdown(mp.current.title) if mp.current else "nothing"
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
    # Embeds: a description holds 4096 characters, a plain message only 2000
    await interaction.response.send_message(
        embeds=ui.help_embeds(help_pages(is_adm)), ephemeral=True)


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
                     await voice_join(interaction, guild))


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
                     await voice_leave(interaction, guild))


@tree.command(name="create", description="Create a voice channel and join it")
@app_commands.describe(name="Channel name (defaults to Amy's Room)")
@app_commands.guild_only()
@admin_only()
async def slash_create(interaction: discord.Interaction, name: str = "") -> None:
    guild = await require_guild(interaction)
    if guild is None:
        return
    await interaction.response.defer()
    await send_reply(interaction,
                     await voice_create(interaction, guild, name))


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


@tree.command(name="lyrics", description="Show lyrics for the current song, or search for one")
@app_commands.describe(song="Song to look up, like 'artist - title' (empty = the current track)")
@app_commands.guild_only()
async def slash_lyrics(interaction: discord.Interaction, song: str = "") -> None:
    guild = await require_guild(interaction)
    if guild is None:
        return
    await interaction.response.defer()
    await send_reply(interaction,
                     await music_lyrics(MusicContext.build(interaction, guild), song))


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


@tree.command(name="shush", description="Stop Amy talking (the music carries on)")
@app_commands.guild_only()
async def slash_shush(interaction: discord.Interaction) -> None:
    await simple_music(interaction, music_shush)


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


@tree.command(name="filter", description="Change how the music sounds (music only)")
@app_commands.describe(preset="off, bass boost, nightcore or slowed (empty shows the current one)")
@app_commands.choices(preset=[app_commands.Choice(name=n, value=n) for n in music.FILTER_PRESETS])
@app_commands.guild_only()
async def slash_filter(interaction: discord.Interaction,
                       preset: Optional[app_commands.Choice[str]] = None) -> None:
    if not await voice_gate(interaction):
        return
    guild = await require_guild(interaction)
    if guild is None:
        return
    # Changing it restarts the song, which re-resolves the stream URL (slow)
    await interaction.response.defer()
    await send_reply(interaction, await music_filter(
        MusicContext.build(interaction, guild), None if preset is None else preset.value))


@tree.command(name="autoplay", description="Keep playing related songs when the queue ends")
@app_commands.describe(state="on or off (leave empty to see the current setting)")
@app_commands.choices(state=[
    app_commands.Choice(name="on", value="on"),
    app_commands.Choice(name="off", value="off"),
])
@app_commands.guild_only()
async def slash_autoplay(interaction: discord.Interaction,
                         state: Optional[app_commands.Choice[str]] = None) -> None:
    await simple_music(interaction, music_autoplay,
                       None if state is None else state.value == "on")


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


def voice_status(guild_id: int) -> str:
    """The /voice report: the switch, the engine, and the voice settings (Up next is this
    server's own setting)."""
    if speaker is None:
        return ("\U0001F507 **Voice: not set up.** Set `TTS=on` in `.env` and restart "
                "(the README's \"Amy's voice\" section covers the one-time install).")
    switch = "on" if voice_enabled else "off (`/voice on` to enable)"
    engine = {"ready": "ready", "loading": "loading...", "off": "not started yet",
              "failed": f"failed - {speaker.problem}"}.get(speaker.state, speaker.state)
    return (f"\U0001F50A **Voice: {switch}**\n"
            f"Up next: {'on' if up_next_on(guild_id) else 'off'}\n"
            f"Engine: {engine}\n"
            f"Voice: {describe_voice()}, "
            f"level {round(VOICE_LEVEL * 100)}%\n"
            f"Music while I talk: {round(DUCK_LEVEL * 100)}%")


@tree.command(name="voice", description="Turn Amy's voice in calls on or off, or show its status")
@app_commands.describe(state="on or off (leave empty to see the status)",
                       up_next="Say the next song's name when one ends by itself: on or off")
@app_commands.choices(state=[
    app_commands.Choice(name="on", value="on"),
    app_commands.Choice(name="off", value="off"),
], up_next=[
    app_commands.Choice(name="on", value="on"),
    app_commands.Choice(name="off", value="off"),
])
@admin_only()
async def slash_voice(interaction: discord.Interaction,
                      state: Optional[app_commands.Choice[str]] = None,
                      up_next: Optional[app_commands.Choice[str]] = None) -> None:
    global voice_enabled
    guild = interaction.guild
    assert guild is not None      # admin_only refuses DMs (an admin needs a server)
    if state is None and up_next is None:
        await interaction.response.send_message(voice_status(guild.id), ephemeral=True)
        return
    if speaker is None:
        await deny(interaction, NO_VOICE)
        return
    parts = []
    if state is not None:
        voice_enabled = state.value == "on"
        db.set_bool_setting("voice_enabled", voice_enabled)   # survives a restart
        if not voice_enabled:
            shush(guild)                                      # off means quiet now, not later
        log.info("Voice is now " + state.value)
        parts.append("\U0001F50A My voice is **on**." if voice_enabled
                     else "\U0001F507 My voice is **off**.")
    if up_next is not None:
        up_next_enabled = up_next.value == "on"
        db.set_guild_bool(guild.id, config.UP_NEXT_KEY, up_next_enabled)  # survives a restart
        log.info(f"Up-next announcements are now {up_next.value} in guild {guild.id}")
        parts.append("I'll say what's up next in this server (when my voice is on)."
                     if up_next_enabled else "I won't announce the next song in this server.")
    await interaction.response.send_message(" ".join(parts))


VOTE_SHARE_WORDS = {
    "half": "at least half of the people in my voice channel",
    "majority": "more than half of the people in my voice channel",
    "two-thirds": "at least two thirds of the people in my voice channel",
}
MAX_DJ_NAMES = 15


def dj_overview(guild: discord.Guild) -> str:
    """The /dj show report: the role, the vote share and who is a DJ in Amy's channel now."""
    saved, exists = saved_dj_role(guild)
    role = guild.get_role(saved) if saved is not None and exists else None
    if role is not None:
        role_line = f"**{markup.escape_markdown(role.name)}** (by id, so renaming it is safe)"
    elif saved is not None:
        role_line = ("the saved role was deleted, so there is none for now - everyone in the "
                     "call is a DJ until you set a new one")
    else:
        role_line = "none - everyone in the call is a DJ"
    share = vote_share(guild.id)
    current = voice.active_channel(voice.get_voice_client(guild))
    if current is None:
        djs = "I'm not in a voice channel."
    else:
        names = [markup.escape_markdown(m.display_name) for m in current.members
                 if not m.bot and member_level(guild, m.id) >= Level.DJ]
        shown = ", ".join(names[:MAX_DJ_NAMES]) + (
            f" and {len(names) - MAX_DJ_NAMES} more" if len(names) > MAX_DJ_NAMES else "")
        djs = shown if names else "nobody right now"
    return (f"**DJ role:** {role_line}\n"
            f"**Skip votes:** {share} ({VOTE_SHARE_WORDS[share]}; skip voting itself is not active yet)\n"
            f"**DJs in my channel now:** {djs}")


def dj_update(guild: discord.Guild, action: str, role: Optional[discord.Role],
              share: Optional[str]) -> Reply:
    """/dj role | clear | votes: store the choice for this server (show is dj_overview)."""
    if action == "role":
        if role is None:
            return _error("Pick the role to use: `/dj action:role role:@Role`.")
        if role.id == guild.id:
            return _error("That is @everyone - pick a real role, or use `/dj action:clear` "
                          "to let everyone in the call be a DJ.")
        if role.is_bot_managed() or role.is_integration():
            return _error("That role belongs to a bot or an integration, so no member can hold "
                          "it. Pick a role people can have (the server booster role is fine).")
        db.set_guild_setting(guild.id, config.DJ_ROLE_KEY, str(role.id))
        log.info(f"DJ role set to {role.id} in guild {guild.id}")
        return _info(f"The DJ role is now **{markup.escape_markdown(role.name)}**. Only members "
                     "with it, anyone alone with me, and admins can control playback; everyone "
                     "in the call can still queue songs and skip their own.")
    if action == "clear":
        db.clear_guild_setting(guild.id, config.DJ_ROLE_KEY)
        log.info(f"DJ role cleared in guild {guild.id}")
        return _info("No DJ role: everyone in my voice channel is a DJ again.")
    if action == "votes":
        if share is None or share not in config.VOTE_SHARES:
            return _error("Pick the share: " + ", ".join(f"`{s}`" for s in config.VOTE_SHARES) + ".")
        db.set_guild_setting(guild.id, config.VOTE_SHARE_KEY, share)
        log.info(f"Vote share set to {share} in guild {guild.id}")
        return _info(f"Skip votes will need **{share}**: {VOTE_SHARE_WORDS[share]}. "
                     "(Skip voting itself is not active yet; the setting is saved.)")
    return _info(dj_overview(guild))


@tree.command(name="dj", description="Show or set this server's DJ role and skip-vote share")
@app_commands.describe(action="show, set the role, clear it, or set the vote share",
                       role="The DJ role (for action: role)",
                       share="How much of the call must vote (for action: votes)")
@app_commands.choices(action=[
    app_commands.Choice(name="show", value="show"),
    app_commands.Choice(name="role", value="role"),
    app_commands.Choice(name="clear", value="clear"),
    app_commands.Choice(name="votes", value="votes"),
], share=[app_commands.Choice(name=s, value=s) for s in config.VOTE_SHARES])
@app_commands.guild_only()
@admin_only()
async def slash_dj(interaction: discord.Interaction, action: app_commands.Choice[str],
                   role: Optional[discord.Role] = None,
                   share: Optional[app_commands.Choice[str]] = None) -> None:
    guild = await require_guild(interaction)
    if guild is None:
        return
    reply = dj_update(guild, action.value, role, share.value if share else None)
    await send_reply(interaction, reply, ephemeral=True)


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
    except Exception as e:      # startup boundary: a failed sync must not stop the bot coming up
        logs.log_failure("Could not sync slash commands", e, DISCORD_ERRORS)

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
        except Exception as e:  # startup, optional: the first track just loads it later
            logs.log_failure("Could not preload opus", e, DISCORD_ERRORS, logging.WARNING)

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
    except Exception as e:      # startup boundary: the rest of on_ready must still run
        logs.log_failure("Orphan voice channel sweep failed", e, DISCORD_ERRORS, logging.WARNING)

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


def _replied_to_author_id(msg: discord.Message) -> Optional[int]:
    """Author of the message this one replies to, from what Discord already sent (no API fetch)."""
    ref = msg.reference
    if ref is None:
        return None
    target = ref.resolved if isinstance(ref.resolved, discord.Message) else ref.cached_message
    return target.author.id if target is not None else None


def _people_note(msg: discord.Message) -> str:
    """
    Status note for the people tagged in the TEXT of `msg` (raw_mentions, so a reply-ping doesn't
    count; not Amy herself; each once); "" when there are none. Status and voice come only from the
    guild's cache: a mention payload's Member carries no presence and would read "offline", so
    that case is "status unknown" and the payload only lends its name. A voice channel the asker
    can't view is not named.
    """
    my_id = bot.user.id if bot.user else 0
    guild = msg.guild
    asker = msg.author if isinstance(msg.author, discord.Member) else (
        guild.get_member(msg.author.id) if guild else None)
    payload = {u.id: u for u in msg.mentions}
    people = []
    for uid in dict.fromkeys(msg.raw_mentions):         # in order, once each
        if uid == my_id:
            continue
        m = guild.get_member(uid) if guild else None
        who = m or payload.get(uid)
        if who is None:
            continue
        if m is None:                                   # DM or uncached: no presence data
            people.append((who.display_name, None, None, who.bot))
            continue
        state = getattr(m, "voice", None)
        chan = getattr(state, "channel", None) if state else None
        voice_name: Optional[str] = None
        if chan is not None:
            can_view = asker is not None and chan.permissions_for(asker).view_channel
            voice_name = chan.name if can_view else ""
        people.append((m.display_name, str(m.status), voice_name, m.bot))
    return llm.people_note(people)


def _addressed_text(msg: discord.Message) -> Optional[str]:
    """The cleaned text for the model when `msg` is meant for Amy, else None (not addressed)."""
    my_id = bot.user.id if bot.user else 0
    guild = msg.guild
    role = guild.self_role if guild else None
    my_role_id = role.id if role else None
    user_ids = set(msg.raw_mentions) | {u.id for u in msg.mentions}
    if not llm.is_addressed(guild is None, user_ids, msg.raw_role_mentions,
                            _replied_to_author_id(msg), my_id, my_role_id):
        return None

    def user_name(uid: int) -> Optional[str]:
        m = (guild.get_member(uid) if guild else None) or discord.utils.get(msg.mentions, id=uid)
        return m.display_name if m else None

    def role_name(rid: int) -> Optional[str]:
        r = guild.get_role(rid) if guild else None
        return r.name if r else None

    def channel_name(cid: int) -> Optional[str]:
        # get_channel misses threads; bot.get_channel covers other servers' channels
        c = (guild.get_channel_or_thread(cid) if guild else None) or bot.get_channel(cid)
        return getattr(c, "name", None)

    return llm.chat_text(msg.content, my_id, my_role_id, user_name, role_name, channel_name)


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

    if not llm.should_chat(msg.is_system(), msg.author.bot, msg.content):
        log.debug("Ignoring a system, bot or text-less message")
        return

    try:
        # Only talk when spoken to: a tag, a reply to her, or a DM. Before the rate limit, so a
        # message for someone else costs nothing. Inside the try so a bug here is logged and
        # answered with "Sorry..." like any other (an ordinary unaddressed message never errors).
        text = _addressed_text(msg)
        if text is None:
            log.debug("Ignoring a message not addressed to Amy")
            return
        if not text:
            log.debug("Ignoring a bare tag (nothing left after removing the mention)")
            return

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
        reply = await chat_streaming(text, server_id, channel_id, thinking_msg,
                                     people_note=_people_note(msg))
        # Spoken too, but only to someone in the call with her (D4) - and in the
        # background: the paraphrase and synthesis take seconds and the text is already out.
        if reply and msg.guild is not None and in_call_with_amy(msg.guild, msg.author.id):
            music.spawn(speak_reply(msg.guild, reply))
        log.debug("Streaming response complete")

    except Exception as e:
        log.exception(f"Error processing message: {e}")
        try:
            await msg.reply("Sorry, I encountered an unexpected error. Please try again.")
        except DISCORD_ERRORS:
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
