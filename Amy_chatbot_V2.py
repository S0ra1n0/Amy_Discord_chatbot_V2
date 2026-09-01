# Amy_chatbot_V2.py

#-------Import Libraries----------
import os
import re
import time
import asyncio
import random
import httpx
from collections import defaultdict
from datetime import datetime
from typing import Dict, List, Optional, Tuple, Union

from dotenv import load_dotenv
import ollama
import discord
from discord.ext import tasks

from commands_help import HELP_EVERYONE, HELP_ADMIN
from database import ConversationDB
import voice
from voice import VoiceManager
import music
from music import LoopMode, MusicManager
import ui
from ui import Reply
#----------------------------------

#----Utility Functions------
def safe_print(message: str) -> None:
    """
    Safe print function that handles Unicode encoding errors on Windows.
    Writes directly to stdout buffer to bypass cp1252 encoding.
    """
    try:
        print(message)
    except UnicodeEncodeError:
        import sys
        sys.stdout.buffer.write(message.encode('utf-8', errors='replace'))
        sys.stdout.buffer.write(b'\n')
        sys.stdout.buffer.flush()

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

async def send_long_reply(msg: discord.Message, text: str) -> None:
    """Reply to a message, splitting into multiple messages if text exceeds Discord's length limit."""
    if not text:
        return
    remaining = text
    first = True
    while remaining:
        idx = find_split_index(remaining, MAX_DISCORD_LEN)
        chunk = remaining[:idx]
        remaining = remaining[idx:]
        if first:
            await msg.reply(chunk)
            first = False
        else:
            await msg.channel.send(chunk)

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
#--------------------------------------

#----Setup Discord Bot and Ollama Model------
load_dotenv()
discord_token = os.getenv("DISCORD_TOKEN")
ADMIN_ROLE_NAME: str = os.getenv("ADMIN_ROLE_NAME", "Admin")
DB_PRUNE_DAYS: int = int(os.getenv("DB_PRUNE_DAYS", "30"))

if not discord_token:
    safe_print("[ERROR] DISCORD_TOKEN not found in .env file. Please add it and try again.")
    exit(1)

intents = discord.Intents.default()
intents.message_content = True
intents.reactions = True
# Privileged: must also be enabled in the Discord Developer Portal.
# Needed so VoiceChannel.members resolves reliably (it looks up guild.get_member),
# which is what empty-channel auto-disconnect depends on.
intents.members = True
bot = discord.Client(intents=intents)

model = "qwen3.5:2b"
system_prompt = '''You are Amy, a sophisticated and helpful personal assistant with the demeanor of a professional secretary.

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
#----------------------------------------------

#----Database------
db = ConversationDB()
#----------------------------------------------

#----Voice & Music------
voice_manager = VoiceManager(db)
music_manager = MusicManager()
music.log = safe_print  # let music.py log through the Windows-safe printer
#----------------------------------------------

#----Bot State------
bot_enabled: bool = True
#----------------------------------------------

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
        safe_print(f"[INFO] Pruned {deleted} message(s) older than {DB_PRUNE_DAYS} days")

    dropped = prune_rate_limit_stores()
    if dropped:
        safe_print(f"[INFO] Dropped {dropped} expired rate-limit entr(ies)")
#----------------------------------------------

#----Pending /clear Confirmations------
# Maps confirmation_message_id → (requesting_user_id, channel_id, server_id)
pending_clear: Dict[int, Tuple[int, int, Union[int, str]]] = {}
#----------------------------------------------

#----Streaming Chat------
STREAM_EDIT_INTERVAL: float = 1.5  # Seconds between Discord message edits during streaming

async def chat_streaming(
    user_message: str,
    server_id: Union[int, str],
    channel_id: int,
    discord_msg: discord.Message,
) -> None:
    """
    Stream an Ollama response and progressively edit discord_msg as tokens arrive.
    Handles <think> blocks by showing 'Thinking...' until actual content begins.
    """
    loop = asyncio.get_running_loop()
    chunk_queue: asyncio.Queue = asyncio.Queue()
    error_flag: Dict[str, bool] = {"occurred": False}

    db.store_message(server_id, channel_id, "user", user_message)
    history = db.get_messages(server_id, channel_id)

    current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    current_day = datetime.now().strftime("%A")
    system_with_time = (
        f"{system_prompt}\n\n[IMPORTANT] Current date and time: {current_time} ({current_day}). "
        "Use this information when answering questions about time."
    )
    messages = [{"role": "system", "content": system_with_time}] + history

    def stream_worker() -> None:
        try:
            stream = ollama.chat(model=model, messages=messages, stream=True)
            for chunk in stream:
                content = chunk['message']['content']
                loop.call_soon_threadsafe(chunk_queue.put_nowait, content)
        except httpx.ConnectError as e:
            safe_print(f"[WARNING] Ollama unavailable during stream: {e}")
            error_flag["occurred"] = True
        except Exception as e:
            safe_print(f"[ERROR] Streaming error: {e}")
            error_flag["occurred"] = True
        finally:
            loop.call_soon_threadsafe(chunk_queue.put_nowait, None)  # Sentinel: stream done

    future = loop.run_in_executor(None, stream_worker)

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

    if error_flag["occurred"]:
        db.pop_last_message(server_id, channel_id)
        try:
            await active_msg.edit(
                content="I apologize, but I'm currently having trouble connecting to my knowledge system. Please try again in a moment."
            )
        except discord.HTTPException:
            pass
        return

    final_response = strip_think_tags(accumulated)
    if not final_response:
        final_response = "I apologize, but I'm having difficulty formulating a response at the moment. Could you please rephrase your question?"

    db.store_message(server_id, channel_id, "assistant", final_response)

    await flush_overflow(final_response)
    pending = final_response[committed_len:]
    try:
        await active_msg.edit(content=pending if pending else "✅ Done.")
    except discord.HTTPException as e:
        safe_print(f"[ERROR] Failed to edit final message: {e}")
#----------------------------------------------

#----Voice Command Handlers------
async def execute_voice_command(
    command: str,
    parts: List[str],
    msg: discord.Message,
    guild: discord.Guild,
) -> Union[str, Reply]:
    """
    Handle /join, /create and /leave.

    Takes an already-narrowed, non-optional guild so every access below is type-safe
    rather than relying on a guard in a different branch.
    """
    if command == "join":
        member = guild.get_member(msg.author.id)
        state = member.voice if member else None
        if state is None or state.channel is None:
            return "🚫 You're not in a voice channel. Join one first, then use `/join`."

        target = state.channel
        perms = target.permissions_for(guild.me)
        if not perms.connect:
            return f"🚫 I don't have permission to connect to **{target.name}**."
        if not perms.speak:
            return f"🚫 I can join **{target.name}**, but I'm not allowed to speak there."

        async with voice_manager.lock_for(guild.id):
            vc = voice.get_voice_client(guild)
            current = voice.active_channel(vc)
            current_id = current.id if current else None
            has_humans = voice.humans_in(current) > 0 if current else False

            action = voice.decide_join_action(current_id, has_humans, target.id, is_admin(msg))

            if action is voice.JoinAction.ALREADY_THERE:
                return f"✅ I'm already in **{target.name}**."
            if action is voice.JoinAction.BLOCKED_OCCUPIED:
                where = current.name if current else "another channel"
                return (
                    f"🚫 I'm currently in **{where}** with other people. "
                    "Join that channel, or ask an admin to move me."
                )
            try:
                if action is voice.JoinAction.MOVE and vc is not None:
                    await vc.move_to(target)
                    return Reply(embed=ui.voice_embed(f"Moved to **{target.name}**."))
                await voice.connect_to(target)
                return Reply(embed=ui.voice_embed(f"Joined **{target.name}**."))
            except RuntimeError as e:
                # discord.py raises this when a voice dependency is missing (PyNaCl or davey).
                # Report what it actually said rather than guessing which one.
                safe_print(f"[ERROR] Voice connect failed: {e}")
                return (
                    f"🚫 Voice support isn't fully installed on my host: {e}\n"
                    "Fix: `pip install \"discord.py[voice]\"`"
                )
            except (discord.ClientException, asyncio.TimeoutError) as e:
                safe_print(f"[ERROR] Voice connect failed: {e}")
                return f"🚫 I couldn't connect to **{target.name}**. Please try again."

    if command == "create":
        if not is_admin(msg):
            return "🚫 You don't have permission to use this command. (Admin only)"
        if not guild.me.guild_permissions.manage_channels:
            return "🚫 I need the **Manage Channels** permission to create a voice channel."

        # Only guild text channels have a category to inherit
        category = msg.channel.category if isinstance(msg.channel, discord.TextChannel) else None
        name = voice.sanitize_channel_name(" ".join(parts[1:]))

        async with voice_manager.lock_for(guild.id):
            try:
                channel = await guild.create_voice_channel(
                    name,
                    category=category,
                    reason=f"/create requested by {msg.author}",
                )
            except discord.Forbidden:
                return "🚫 Discord refused that. Check my **Manage Channels** permission."
            except discord.HTTPException as e:
                safe_print(f"[ERROR] Channel creation failed: {e}")
                return "🚫 I couldn't create that channel. Please try again."

            try:
                vc = voice.get_voice_client(guild)
                if vc is not None and vc.is_connected():
                    await vc.move_to(channel)
                else:
                    await voice.connect_to(channel)
            except Exception as e:
                safe_print(f"[ERROR] Could not join newly created channel: {e}")
                # Channel exists but Amy couldn't join - don't orphan it
                try:
                    await channel.delete(reason="Amy could not join the channel she created")
                except discord.HTTPException:
                    pass
                if isinstance(e, RuntimeError):
                    # Missing voice dependency - say which, so it's actionable
                    return (
                        f"🚫 I created the channel but voice support isn't fully installed: {e}\n"
                        "Fix: `pip install \"discord.py[voice]\"` (channel removed again)"
                    )
                return "🚫 I created the channel but couldn't join it, so I removed it again."

            voice_manager.mark_created(channel.id, guild.id)
            return Reply(embed=ui.voice_embed(
                f"Created {channel.mention} and joined — hop in!", heading="Voice channel created"))

    if command == "leave":
        vc = voice.get_voice_client(guild)
        current = voice.active_channel(vc)
        if current is None:
            return "🚫 I'm not in a voice channel."

        member = guild.get_member(msg.author.id)
        in_same_channel = bool(
            member and member.voice and member.voice.channel
            and member.voice.channel.id == current.id
        )
        if not (is_admin(msg) or in_same_channel):
            return "🚫 You need to be in my voice channel (or an admin) to make me leave."

        async with voice_manager.lock_for(guild.id):
            left = await voice.leave_voice(guild, voice_manager, on_cleanup=music_manager.cleanup)
        if not left:
            return Reply(embed=ui.error_embed("I'm not in a voice channel."))
        return Reply(embed=ui.voice_embed(f"Left **{left}**.", heading="Voice"))

    return f"Unknown voice command: `{command}`."
#--------------------------------------

#----Reply Emoji------
# Written as escapes so the source stays ASCII-safe on Windows consoles
SEARCH: str = "🔍"     # magnifying glass
NOTE: str = "🎵"       # musical note
PLUS: str = "➕"           # heavy plus
DENY: str = "🚫"       # prohibited
HOURGLASS: str = "⏳"      # hourglass
SHUFFLE: str = "🔀"    # shuffle arrows
TRASH: str = "🗑"      # wastebasket
NEXT: str = "⏭"           # next track
NEWLINE: str = "\n"
#--------------------------------------

#----Command Groups------
VOICE_COMMANDS = ("join", "create", "leave")
MUSIC_COMMANDS = (
    "play", "pause", "resume", "skip", "stop", "queue", "nowplaying", "np", "volume", "loop",
    "remove", "shuffle", "clearqueue", "skipto",
)
# Harmless reads - exempt from the voice cooldown so checking the queue never costs a slot
MUSIC_READONLY_COMMANDS = ("queue", "nowplaying", "np")
#--------------------------------------

#----Player Buttons------
def may_control_playback(guild: Optional[discord.Guild], user_id: int) -> bool:
    """
    Same rule the text commands use: you must be in Amy's voice channel, or be an admin.
    Shared so a button can never become a way around a command's permission check.
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
        if vc.is_paused():
            vc.resume()
            paused = False
        elif vc.is_playing():
            vc.pause()
            paused = True
        else:
            await interaction.response.send_message(
                embed=ui.error_embed("Nothing is playing."), ephemeral=True)
            return

        if player.current is None:
            await interaction.response.defer()
            return
        await interaction.response.edit_message(
            embed=ui.now_playing_embed(player.current, len(player.queue),
                                       player.loop_mode, player.volume, paused=paused),
            view=PlayerControls(paused=paused),
        )

    @discord.ui.button(label="Skip", emoji="⏭", style=discord.ButtonStyle.primary,
                       custom_id="amy:skip")
    async def skip_button(self, interaction: discord.Interaction,
                          button: discord.ui.Button) -> None:
        guild = interaction.guild
        vc = voice.get_voice_client(guild) if guild else None
        if guild is None or vc is None or not (vc.is_playing() or vc.is_paused()):
            await interaction.response.send_message(
                embed=ui.error_embed("Nothing is playing."), ephemeral=True)
            return

        player = music_manager.player_for(guild.id)
        player.skip_requested = True  # so TRACK loop can't swallow an explicit skip
        await interaction.response.defer()
        vc.stop()  # after-callback advances the queue and refreshes the message

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
        player.queue.clear()
        player.loop_mode = LoopMode.OFF
        player.current = None
        vc.stop()

        embed = (ui.now_playing_embed(finished, stopped=True) if finished
                 else ui.info_embed("Playback stopped."))
        await interaction.response.edit_message(embed=embed, view=None)

        async with voice_manager.lock_for(guild.id):
            await voice.leave_voice(guild, voice_manager, on_cleanup=music_manager.cleanup)


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
                                     player.loop_mode, player.volume, paused=paused)
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
        safe_print(f"[WARNING] Could not post now-playing message: {e}")
#--------------------------------------

#----Music Playback Engine------
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
        if vc.is_playing() or vc.is_paused():
            return

        # A user-requested skip overrides TRACK loop for this one advance
        force_next = player.skip_requested
        player.skip_requested = False
        next_track = music.advance_queue(
            player.current, player.queue, player.loop_mode, force_next=force_next
        )
        if next_track is None:
            if player.current is not None:
                player.last_played = player.current
            player.current = None
            await refresh_now_playing(guild, stopped=True)
            safe_print("[INFO] Queue empty - starting idle timer")
            player.cancel_idle()  # never stack timers; a stale one could disconnect later
            player.idle_task = asyncio.create_task(idle_disconnect(guild))
            return

        loop = asyncio.get_running_loop()
        after = music.make_after_callback(loop, lambda: advance_playback(guild))
        try:
            await music.play_track(vc, player, next_track, after)
            player.last_played = next_track
            await refresh_now_playing(guild)
        except Exception as e:
            safe_print(f"[ERROR] Could not play '{next_track.title}': {e}")
            # Skip the bad track rather than stalling the whole queue
            player.current = None
            asyncio.create_task(advance_playback(guild))


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
        left = await voice.leave_voice(guild, voice_manager, on_cleanup=music_manager.cleanup)
    if left:
        safe_print(f"[INFO] Left {left} after being idle")
#--------------------------------------

#----Music Command Handlers------
async def execute_music_command(
    command: str,
    parts: List[str],
    msg: discord.Message,
    guild: discord.Guild,
) -> Union[str, Reply]:
    """Handle the music commands. `guild` is already narrowed to non-optional."""
    player = music_manager.player_for(guild.id)
    vc = voice.get_voice_client(guild)
    member = guild.get_member(msg.author.id)

    def in_voice_with_amy() -> bool:
        current = voice.active_channel(vc)
        return bool(
            current and member and member.voice and member.voice.channel
            and member.voice.channel.id == current.id
        )

    # --- read-only commands, no connection required ---
    if command == "queue":
        page = 1
        if len(parts) > 1:
            try:
                page = int(parts[1])
            except ValueError:
                return DENY + " Page must be a number. Usage: `/queue` or `/queue 2`"
        return Reply(embed=ui.queue_embed(player.current, player.queue,
                                          player.loop_mode, page=page))

    if command in ("nowplaying", "np"):
        if player.current is None:
            return Reply(embed=ui.info_embed("Nothing is playing right now."))
        paused = bool(vc and vc.is_paused())
        return Reply(
            embed=ui.now_playing_embed(player.current, len(player.queue),
                                       player.loop_mode, player.volume, paused=paused),
            view=PlayerControls(paused=paused),
        )

    # --- /play: joins if needed, then queues ---
    if command == "play":
        if len(parts) < 2:
            return "🚫 What should I play? Usage: `/play <song name, URL, or file path>`"

        if music.find_ffmpeg() is None:
            return (
                "🚫 FFmpeg isn't available on my host, so I can't play audio.\n"
                "Install it, or set `FFMPEG_PATH` in `.env` to the full path of `ffmpeg.exe`."
            )

        # Checked before joining, so Amy doesn't connect only to refuse the track
        if len(player.queue) >= music.MAX_QUEUE_SIZE:
            return f"🚫 The queue is full ({music.MAX_QUEUE_SIZE} tracks). Try again once it drains."

        # Join the requester's channel if not already connected
        if voice.active_channel(vc) is None:
            state = member.voice if member else None
            if state is None or state.channel is None:
                return "🚫 You're not in a voice channel. Join one first, then use `/play`."
            perms = state.channel.permissions_for(guild.me)
            if not perms.connect or not perms.speak:
                return f"🚫 I need Connect and Speak permissions in **{state.channel.name}**."
            try:
                async with voice_manager.lock_for(guild.id):
                    await voice.connect_to(state.channel)
                vc = voice.get_voice_client(guild)
            except RuntimeError as e:
                safe_print(f"[ERROR] Voice connect failed: {e}")
                return (
                    f"🚫 Voice support isn't fully installed on my host: {e}\n"
                    "Fix: `pip install \"discord.py[voice]\"`"
                )
            except Exception as e:
                safe_print(f"[ERROR] Voice connect failed: {e}")
                return "🚫 I couldn't join your voice channel. Please try again."
        elif not in_voice_with_amy() and not is_admin(msg):
            return "🚫 You need to be in my voice channel to queue tracks."

        # advance_playback has no message context, so remember where to post the card
        player.text_channel_id = msg.channel.id

        query = " ".join(parts[1:])
        # Echoed back into a Discord message, so keep it well under the 2000-char limit
        shown = query if len(query) <= 100 else query[:100] + "..."
        author = str(msg.author)

        # Resolving hits the network and can take a few seconds, so acknowledge
        # immediately and edit this message once we know the result.
        status_msg = await msg.reply(SEARCH + " Searching for **" + shown + "**...")

        # --- playlist URL: queue many tracks at once ---
        if music.is_playlist_url(query):
            await status_msg.edit(content=SEARCH + " Loading playlist...")
            try:
                title, tracks, skipped = await music.resolve_playlist(query, author)
            except Exception as e:
                safe_print("[ERROR] Could not load playlist: " + str(e))
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
            if vc is None or not (vc.is_playing() or vc.is_paused()):
                await advance_playback(guild)   # posts its own now-playing card
            return ""

        # --- single track ---
        try:
            track = await music.resolve_metadata(query, requested_by=author)
        except Exception as e:
            safe_print("[ERROR] Could not resolve query: " + str(e))
            await status_msg.edit(content=DENY + " I couldn't find anything for `" + shown + "`.")
            return ""

        player.queue.append(track)
        player.cancel_idle()
        duration = music.format_duration(track.duration)

        if vc is not None and (vc.is_playing() or vc.is_paused()):
            await status_msg.edit(content=None,
                                  embed=ui.queued_embed(track, len(player.queue)))
            return ""

        await status_msg.edit(content=HOURGLASS + " Loading **" + track.title + "**...")
        await advance_playback(guild)   # posts the now-playing card with controls
        try:
            await status_msg.delete()   # the card replaces this status line
        except discord.HTTPException:
            pass
        return ""

    # --- everything below needs an active connection ---
    if vc is None or not vc.is_connected():
        return "🚫 I'm not in a voice channel."

    if command == "pause":
        if not in_voice_with_amy() and not is_admin(msg):
            return "🚫 You need to be in my voice channel to do that."
        if not vc.is_playing():
            return "🚫 Nothing is playing."
        vc.pause()
        await refresh_now_playing(guild, paused=True)
        return Reply(embed=ui.info_embed("Paused."))

    if command == "resume":
        if not in_voice_with_amy() and not is_admin(msg):
            return "🚫 You need to be in my voice channel to do that."
        if not vc.is_paused():
            return Reply(embed=ui.error_embed("Nothing is paused."))
        vc.resume()
        await refresh_now_playing(guild, paused=False)
        return Reply(embed=ui.info_embed("Resumed."))

    if command == "skip":
        if not in_voice_with_amy() and not is_admin(msg):
            return "🚫 You need to be in my voice channel to skip."
        if not (vc.is_playing() or vc.is_paused()):
            return "🚫 Nothing is playing."
        skipped = player.current.title if player.current else "the current track"
        player.skip_requested = True
        vc.stop()  # triggers the after-callback, which advances the queue
        return Reply(embed=ui.info_embed(f"Skipped **{skipped}**."))

    if command == "stop":
        if not in_voice_with_amy() and not is_admin(msg):
            return "🚫 You need to be in my voice channel (or be an admin) to stop playback."
        player.queue.clear()
        player.loop_mode = LoopMode.OFF
        player.current = None
        vc.stop()
        async with voice_manager.lock_for(guild.id):
            left = await voice.leave_voice(guild, voice_manager, on_cleanup=music_manager.cleanup)
        return Reply(embed=ui.info_embed(
            f"Stopped and left **{left}**." if left else "Stopped."))

    if command == "volume":
        if not is_admin(msg):
            return "🚫 You don't have permission to use this command. (Admin only)"
        if len(parts) < 2:
            return Reply(embed=ui.info_embed(
                f"Current volume: **{int(player.volume * 100)}%**\nUsage: `/volume 0-100`"))
        parsed = music.parse_volume(parts[1])
        if parsed is None:
            return Reply(embed=ui.error_embed("Volume must be a whole number between 0 and 100."))
        player.volume = parsed / 100
        # Applies instantly only on the PCM path; an opus-passthrough track has no
        # volume stage, so the change lands when the next track starts.
        if isinstance(vc.source, discord.PCMVolumeTransformer):
            vc.source.volume = player.volume
            return Reply(embed=ui.info_embed(f"Volume set to **{parsed}%**."))

        if parsed >= 100:
            return Reply(embed=ui.info_embed("Volume set to **100%** (full quality, lowest CPU)."))
        return (
            f"🔊 Volume set to **{parsed}%** — applies from the next track.\n"
            "_Note: below 100% Amy has to decode audio rather than pass it through, "
            "which costs more CPU and can stutter on a busy machine._"
        )

    if command == "loop":
        if not in_voice_with_amy() and not is_admin(msg):
            return "🚫 You need to be in my voice channel to do that."
        if len(parts) < 2:
            return (
                f"🔁 Loop is **{player.loop_mode.value}**.\n"
                "Usage: `/loop off`, `/loop track`, or `/loop queue`"
            )
        try:
            player.loop_mode = LoopMode(parts[1].lower())
        except ValueError:
            return "🚫 Loop mode must be `off`, `track`, or `queue`."
        return Reply(embed=ui.info_embed(f"Loop set to **{player.loop_mode.value}**."))

    if command == "remove":
        if not in_voice_with_amy() and not is_admin(msg):
            return DENY + " You need to be in my voice channel to do that."
        if len(parts) < 2:
            return DENY + " Which one? Usage: `/remove <position>` (see `/queue`)"
        try:
            index = int(parts[1])
        except ValueError:
            return DENY + " Position must be a number. Usage: `/remove <position>`"

        target = music.peek_at(player.queue, index)
        if target is None:
            return DENY + " There's no track at position " + str(index) + "."
        # Users may only remove what they queued; admins may remove anything
        if target.requested_by != str(msg.author) and not is_admin(msg):
            return (DENY + " That track was queued by " + target.requested_by
                    + ". You can only remove your own.")

        music.remove_at(player.queue, index)  # `target` above already proved it exists
        return Reply(embed=ui.info_embed("Removed **" + target.title + "** from the queue."))

    if command == "shuffle":
        if not in_voice_with_amy() and not is_admin(msg):
            return DENY + " You need to be in my voice channel to do that."
        if len(player.queue) < 2:
            return DENY + " Not enough tracks queued to shuffle."
        music.shuffle_queue(player.queue)
        return Reply(embed=ui.info_embed("Shuffled **" + str(len(player.queue)) + "** queued track(s)."))

    if command == "clearqueue":
        if not is_admin(msg):
            return DENY + " You don't have permission to use this command. (Admin only)"
        count = len(player.queue)
        if count == 0:
            return DENY + " The queue is already empty."
        player.queue.clear()
        return Reply(embed=ui.info_embed(
            "Cleared **" + str(count) + "** queued track(s). Current track keeps playing."))

    if command == "skipto":
        if not in_voice_with_amy() and not is_admin(msg):
            return DENY + " You need to be in my voice channel to do that."
        if len(parts) < 2:
            return DENY + " Skip to where? Usage: `/skipto <position>` (see `/queue`)"
        try:
            index = int(parts[1])
        except ValueError:
            return DENY + " Position must be a number. Usage: `/skipto <position>`"

        target = music.peek_at(player.queue, index)
        if target is None:
            return DENY + " There's no track at position " + str(index) + "."

        dropped = music.drop_before(player.queue, index)
        player.skip_requested = True
        # stop() fires the after-callback, which pulls the next track off the queue
        vc.stop()
        reply = NEXT + " Skipping to **" + target.title + "**"
        if dropped:
            reply += " (" + str(dropped) + " track(s) skipped)"
        return reply + "."

    return f"Unknown music command: `{command}`."
#--------------------------------------

#----Command Functions------
async def execute_command(command_text: str, msg: discord.Message) -> Union[str, Reply]:
    """Execute a command based on the command text."""
    global bot_enabled, model
    parts = command_text.strip().split()

    if not parts:
        return "Invalid command. Use /help for available commands."

    command = parts[0].lower()

    if command == "help":
        return HELP_EVERYONE + (HELP_ADMIN if is_admin(msg) else "")

    if command == "toggle":
        if not is_admin(msg):
            return "🚫 You don't have permission to use this command. (Admin only)"
        bot_enabled = not bot_enabled
        state = "enabled" if bot_enabled else "disabled"
        safe_print(f"[INFO] Bot is now {state}")
        return f"🤖 Bot is now **{state}**"

    if command == "status":
        if not is_admin(msg):
            return "🚫 You don't have permission to use this command. (Admin only)"

        bot_state = "Enabled ✅" if bot_enabled else "Disabled ❌"

        try:
            await asyncio.get_running_loop().run_in_executor(None, ollama.list)
            ollama_state = "Connected ✅"
        except Exception:
            ollama_state = "Unavailable ❌"

        stats = db.get_stats()
        throttled = get_rate_limited_count()

        current = voice.active_channel(voice.get_voice_client(msg.guild)) if msg.guild else None
        if current is not None:
            voice_state = f"In **{current.name}** ({voice.humans_in(current)} listener(s))"
        else:
            voice_state = "Not connected"

        ffmpeg_state = "Found ✅" if music.find_ffmpeg() else "Missing ❌"
        if msg.guild:
            mp = music_manager.player_for(msg.guild.id)
            playing = mp.current.title if mp.current else "nothing"
            music_state = f"{playing} | {len(mp.queue)} queued | loop {mp.loop_mode.value}"
        else:
            music_state = "n/a"

        return (
            f"📊 **Amy Status**\n"
            f"Bot: {bot_state}\n"
            f"Ollama: {ollama_state} | Model: `{model}`\n"
            f"Voice: {voice_state}\n"
            f"Music: {music_state} | FFmpeg: {ffmpeg_state}\n"
            f"Memory: {stats['total_messages']} messages across {stats['active_channels']} channel(s)\n"
            f"Rate limits: {throttled} user(s) currently throttled"
        )

    if command == "model":
        if not is_admin(msg):
            return "🚫 You don't have permission to use this command. (Admin only)"

        if len(parts) < 2:
            return f"🧠 Current model: `{model}`\nUsage: `/model <model_name>` to switch."

        new_model = parts[1]
        loop = asyncio.get_running_loop()
        try:
            available = await loop.run_in_executor(None, ollama.list)
        except Exception as e:
            return f"🚫 Could not reach Ollama to verify the model: {e}"

        model_names = extract_model_names(available)
        if new_model not in model_names:
            names_list = ", ".join(f"`{n}`" for n in model_names) or "none installed"
            return f"🚫 Model `{new_model}` not found. Installed models: {names_list}"

        model = new_model
        safe_print(f"[INFO] Model switched to {model}")
        return f"🧠 Model switched to `{model}`"

    #----Voice & Music Commands------
    # Handled in one block so the guild guard below narrows for every one of them
    if command in VOICE_COMMANDS or command in MUSIC_COMMANDS:
        guild = msg.guild
        if guild is None:
            return "🚫 Voice and music commands only work in a server, not in DMs."
        # Light cooldown so these can't be spammed (admins and read-only commands exempt)
        if not is_admin(msg) and command not in MUSIC_READONLY_COMMANDS:
            allowed, reset_in = check_voice_cooldown(msg.author.id)
            if not allowed:
                return f"⏳ Too many voice commands. Try again in {reset_in}s."
        if command in VOICE_COMMANDS:
            return await execute_voice_command(command, parts, msg, guild)
        return await execute_music_command(command, parts, msg, guild)
    #--------------------------------------

    if command == "clear":
        if not is_admin(msg):
            return "🚫 You don't have permission to use this command. (Admin only)"
        server_id = msg.guild.id if msg.guild else "DM"
        channel_id = msg.channel.id
        confirm_msg = await msg.reply(
            "⚠️ This will wipe all conversation memory for this channel. React with ✅ to confirm, or ❌ to cancel."
        )
        await confirm_msg.add_reaction("✅")
        await confirm_msg.add_reaction("❌")
        pending_clear[confirm_msg.id] = (msg.author.id, channel_id, server_id)

        async def timeout_clear(confirm_id: int) -> None:
            await asyncio.sleep(60)
            if confirm_id in pending_clear:
                del pending_clear[confirm_id]
                try:
                    await confirm_msg.edit(content="⚠️ Clear request timed out. Cancelled.")
                    await confirm_msg.clear_reactions()
                except Exception:
                    pass
        asyncio.create_task(timeout_clear(confirm_msg.id))
        return ""  # Reply already sent above

    if command == "dice1":
        roll = random.randint(1, 6)
        return f"🎲 Rolled 1d6: **{roll}**"

    if command == "dice2":
        roll1 = random.randint(1, 6)
        roll2 = random.randint(1, 6)
        total = roll1 + roll2
        return f"🎲 Rolled 2d6: **{roll1}** + **{roll2}** = **{total}**"

    if command == "dice":
        try:
            amount = 1
            sides = 6
            if len(parts) > 1:
                sides = int(parts[1])
                if sides < 1:
                    return "Invalid dice command. Sides must be at least 1."
            if len(parts) > 2:
                amount = int(parts[2])
                if amount < 1:
                    return "Invalid dice command. Amount must be at least 1."
            total = sum(random.randint(1, sides) for _ in range(amount))
            return f"🎲 Rolled {amount} {sides}-sided dice(s): **{total}**"
        except ValueError:
            return "Invalid dice command. Usage: /dice [sides] or /dice [sides] [amount]\nExample: /dice 20 or /dice 6 3"

    if command == "rng":
        try:
            if len(parts) < 3:
                return "Invalid rng command. Usage: `/rng min max`\nExample: `/rng 0 999`"
            n = int(parts[1])
            m = int(parts[2])
            if n > m:
                return "Invalid rng command. Min must be less than or equal to Max."
            result = random.randint(n, m)
            return f"🎲 Random number between {n} and {m}: **{result}**"
        except ValueError:
            return "Invalid rng command. Both arguments must be integers.\nUsage: `/rng min max`\nExample: `/rng 0 999`"

    return f"Unknown command: `{command}`. Use `/help` for available commands."
#--------------------------------------

#----Event Handlers for Discord Bot------
@bot.event
async def on_ready() -> None:
    safe_print(f'{bot.user} is online!')
    activity = discord.Activity(type=discord.ActivityType.watching, name="conversations")
    await bot.change_presence(activity=activity, status=discord.Status.online)
    safe_print("[INFO] Bot status set to: Watching conversations")

    if not prune_old_messages_task.is_running():
        prune_old_messages_task.start()
        safe_print(f"[INFO] Auto-prune task started (prunes messages older than {DB_PRUNE_DAYS} days, every 24h)")

    # Register the persistent player buttons so they keep working across restarts
    bot.add_view(PlayerControls())
    safe_print("[INFO] Player controls registered")

    # Load the opus encoder up front so the first track doesn't stall while it loads
    if not discord.opus.is_loaded():
        try:
            discord.opus._load_default()
            safe_print("[INFO] Opus encoder loaded")
        except Exception as e:
            safe_print(f"[WARNING] Could not preload opus: {e}")

    # Clean up voice channels Amy created before a restart
    try:
        removed = await voice.sweep_orphan_channels(bot, db, voice_manager)
        if removed:
            safe_print(f"[INFO] Cleaned up {removed} orphaned voice channel(s) from a previous run")
    except Exception as e:
        safe_print(f"[WARNING] Orphan voice channel sweep failed: {e}")

@bot.event
async def on_connect() -> None:
    safe_print("[INFO] Bot connected to Discord")

@bot.event
async def on_disconnect() -> None:
    safe_print("[WARNING] Bot disconnected from Discord, attempting to reconnect...")

last_processed_id: Union[int, None] = None

@bot.event
async def on_message(msg: discord.Message) -> None:
    """Process messages from users - execute commands or send to chat."""
    global last_processed_id

    if msg.id == last_processed_id:
        return
    last_processed_id = msg.id

    safe_print(f"[DEBUG] Message received from {msg.author}: {msg.content}")

    if msg.author == bot.user:
        safe_print("[DEBUG] Ignoring bot's own message")
        return

    try:
        server_id = msg.guild.id if msg.guild else "DM"
        channel_id = msg.channel.id
        safe_print(f"[DEBUG] Server ID: {server_id}, Channel ID: {channel_id}")

        if msg.content.startswith("/"):
            safe_print("[DEBUG] Processing as command")
            response = await execute_command(msg.content[1:], msg)
            if isinstance(response, Reply):
                # discord.py wants MISSING, not None, for "don't set this field"
                await msg.reply(
                    content=response.content or discord.utils.MISSING,
                    embed=response.embed or discord.utils.MISSING,
                    view=response.view or discord.utils.MISSING,
                )
            elif response:
                await send_long_reply(msg, response)
            safe_print("[DEBUG] Command handled")
            return

        if not bot_enabled:
            safe_print("[DEBUG] Bot is disabled, ignoring chat message")
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

        safe_print("[DEBUG] Processing as streaming chat...")
        thinking_msg = await msg.reply("⏳ Thinking...")
        await chat_streaming(msg.content, server_id, channel_id, thinking_msg)
        safe_print("[DEBUG] Streaming response complete")

    except Exception as e:
        import traceback
        safe_print(f"[ERROR] Error processing message: {e}")
        safe_print(traceback.format_exc())
        try:
            await msg.reply("Sorry, I encountered an unexpected error. Please try again.")
        except Exception:
            safe_print("[ERROR] Failed to send error message to Discord")

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
            safe_print(f"[INFO] Amy was disconnected from voice channel: {before.channel.name}")
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
        left = await voice.leave_voice(guild, voice_manager, on_cleanup=music_manager.cleanup)
    if left:
        safe_print(f"[INFO] Left empty voice channel: {left}")

@bot.event
async def on_reaction_add(reaction: discord.Reaction, user: discord.User) -> None:
    """Handle reactions for /clear confirmation."""
    if user.bot:
        return

    confirm_id = reaction.message.id
    if confirm_id not in pending_clear:
        return

    requesting_user_id, channel_id, server_id = pending_clear[confirm_id]

    if user.id != requesting_user_id:
        return

    del pending_clear[confirm_id]

    if str(reaction.emoji) == "✅":
        db.clear_channel(server_id, channel_id)
        await reaction.message.edit(content="🗑️ Conversation memory for this channel has been cleared.")
    else:
        await reaction.message.edit(content="Cancelled.")

    try:
        await reaction.message.clear_reactions()
    except Exception:
        pass
#--------------------------------------

if __name__ == "__main__":
    try:
        safe_print("[INFO] Starting Amy Chatbot...")
        bot.run(discord_token)
    except KeyboardInterrupt:
        safe_print("[INFO] Bot interrupted by user (Ctrl+C)")
    except Exception as e:
        safe_print(f"[ERROR] Fatal error: {e}")
        import traceback
        safe_print(traceback.format_exc())
        exit(1)
