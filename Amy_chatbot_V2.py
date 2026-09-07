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
from typing import Any, Dict, List, Optional, Tuple, Union

from dotenv import load_dotenv
import ollama
import discord
from discord.ext import tasks
from discord import app_commands

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
# Guild-scoped command sync is instant; global sync can take an hour to appear.
# Reasoning models (qwen3.x) can burn the entire generation budget "thinking" and emit
# no answer at all - measured 3,904 tokens of reasoning and 0 characters of content, with
# done_reason="length". Disabling it is both a correctness fix and ~7x faster.
OLLAMA_THINK: bool = os.getenv("OLLAMA_THINK", "false").strip().lower() in ("1", "true", "yes")

_raw_guild = os.getenv("GUILD_ID", "").strip()
GUILD_ID: Optional[int] = int(_raw_guild) if _raw_guild.isdigit() else None

if not discord_token:
    safe_print("[ERROR] DISCORD_TOKEN not found in .env file. Please add it and try again.")
    exit(1)

intents = discord.Intents.default()
intents.message_content = True
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


async def send_reply(interaction: discord.Interaction, result) -> None:
    """
    Deliver whatever a handler returned.

    Handlers return a str, a ui.Reply, or "" when they already sent their own message.
    Uses followup when the interaction was deferred, otherwise responds directly.
    """
    if not result:
        return

    kwargs = {}
    if isinstance(result, Reply):
        if result.content:
            kwargs["content"] = result.content
        if result.embed is not None:
            kwargs["embed"] = result.embed
        if result.view is not None:
            kwargs["view"] = result.view
    else:
        kwargs["content"] = result[:MAX_DISCORD_LEN]

    if interaction.response.is_done():
        await interaction.followup.send(**kwargs)
    else:
        await interaction.response.send_message(**kwargs)
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
        safe_print(f"[INFO] Pruned {deleted} message(s) older than {DB_PRUNE_DAYS} days")

    dropped = prune_rate_limit_stores()
    if dropped:
        safe_print(f"[INFO] Dropped {dropped} expired rate-limit entr(ies)")
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
    error_flag: Dict[str, Any] = {"occurred": False, "done_reason": None}

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
            stream = ollama.chat(model=model, messages=messages, stream=True,
                                 think=OLLAMA_THINK)
            for chunk in stream:
                content = chunk['message']['content']
                # Remember why generation stopped, so an empty answer can be explained
                reason = chunk.get('done_reason')
                if reason:
                    error_flag["done_reason"] = reason
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
        # Empty output almost always means the model hit its token ceiling rather than
        # having nothing to say, so don't tell the user to rephrase - it won't help.
        if error_flag.get("done_reason") == "length":
            safe_print("[WARNING] Model hit its token limit before producing an answer")
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
        safe_print(f"[ERROR] Failed to edit final message: {e}")
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
                    return Reply(embed=ui.voice_embed(f"Moved to **{target.name}**."))
                await voice.connect_to(target)
                return Reply(embed=ui.voice_embed(f"Joined **{target.name}**."))
            except RuntimeError as e:
                # discord.py raises this when a voice dependency is missing (PyNaCl or davey).
                # Report what it actually said rather than guessing which one.
                safe_print(f"[ERROR] Voice connect failed: {e}")
                return Reply(embed=ui.error_embed(
                    f"Voice support isn't fully installed on my host: {e}\n"
                    "Fix: `pip install \"discord.py[voice]\"`"
                ))
            except (discord.ClientException, asyncio.TimeoutError) as e:
                safe_print(f"[ERROR] Voice connect failed: {e}")
                return Reply(embed=ui.error_embed(f"I couldn't connect to **{target.name}**. Please try again."))

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
                safe_print(f"[ERROR] Channel creation failed: {e}")
                return Reply(embed=ui.error_embed("I couldn't create that channel. Please try again."))

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
                    return Reply(embed=ui.error_embed(
                        f"I created the channel but voice support isn't fully installed: {e}\n"
                        "Fix: `pip install \"discord.py[voice]\"` (channel removed again)"
                    ))
                return Reply(embed=ui.error_embed("I created the channel but couldn't join it, so I removed it again."))

            voice_manager.mark_created(channel.id, guild.id)
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
DENY: str = "🚫"       # prohibited
HOURGLASS: str = "⏳"      # hourglass
NEXT: str = "⏭"           # next track
#--------------------------------------

#----Command Groups------
# Harmless reads - exempt from the voice cooldown so checking the queue never costs a slot
MUSIC_READONLY_COMMANDS = ("queue", "nowplaying")
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
        player.skip_requested = False
        player.current = None
        # Matches /stop: stop and stay. The idle timer still disconnects her later.
        vc.stop()

        embed = (ui.now_playing_embed(finished, stopped=True) if finished
                 else ui.info_embed("Playback stopped."))
        await interaction.response.edit_message(embed=embed, view=None)


class SearchResults(discord.ui.View):
    """
    Dropdown of /search hits.

    Unlike PlayerControls this holds per-message state (the specific results), so it
    cannot be a persistent view. It expires after SEARCH_TIMEOUT and disables itself,
    which is fine: a stale search is not worth acting on.
    """

    SEARCH_TIMEOUT: float = 60.0

    def __init__(self, tracks, requester_id: int, guild: discord.Guild) -> None:
        super().__init__(timeout=self.SEARCH_TIMEOUT)
        self.tracks = tracks
        self.requester_id = requester_id
        self.guild = guild
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
        if self.message is not None:
            try:
                await self.message.edit(view=self)
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
                safe_print(f"[ERROR] Voice connect failed during search pick: {e}")
                await interaction.response.edit_message(
                    embed=ui.error_embed("I couldn't join your voice channel."), view=None)
                return

        player.text_channel_id = interaction.channel_id
        player.queue.append(track)
        player.cancel_idle()
        self.stop()

        vc = voice.get_voice_client(guild)
        if vc is not None and (vc.is_playing() or vc.is_paused()):
            await interaction.response.edit_message(
                embed=ui.queued_embed(track, len(player.queue)), view=None)
            return

        await interaction.response.edit_message(
            embed=ui.info_embed(f"Loading **{track.title}**..."), view=None)
        await advance_playback(guild)   # posts the now-playing card


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
    interaction: discord.Interaction,
    guild: discord.Guild,
) -> Union[str, Reply]:
    """Handle the music commands. `guild` is already narrowed to non-optional."""
    player = music_manager.player_for(guild.id)
    vc = voice.get_voice_client(guild)
    member = guild.get_member(interaction.user.id)

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
                return Reply(embed=ui.error_embed("Page must be a number. Usage: `/queue` or `/queue 2`"))
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

    if command == "search":
        if len(parts) < 2:
            return Reply(embed=ui.error_embed(
                "What should I search for? Usage: `/search <song name>`"))
        if music.find_ffmpeg() is None:
            return Reply(embed=ui.error_embed(
                "FFmpeg isn't available on my host, so I can't play audio."))

        query = " ".join(parts[1:])
        shown = query if len(query) <= 100 else query[:100] + "..."
        status_msg = await interaction.followup.send(SEARCH + " Searching for **" + shown + "**...", wait=True)
        try:
            results = await music.search_tracks(query, requested_by=str(interaction.user))
        except Exception as e:
            safe_print("[ERROR] Search failed: " + str(e))
            await status_msg.edit(content=None,
                                  embed=ui.error_embed("That search went wrong. Try again."))
            return ""

        if not results:
            await status_msg.edit(content=None, embed=ui.error_embed(
                "Nothing found for `" + shown + "`."))
            return ""

        view = SearchResults(results, interaction.user.id, guild)
        await status_msg.edit(content=None,
                              embed=ui.search_results_embed(query, results), view=view)
        view.message = status_msg   # so on_timeout can grey out the menu
        return ""

    # --- /play: joins if needed, then queues ---
    if command == "play":
        if len(parts) < 2:
            return Reply(embed=ui.error_embed("What should I play? Usage: `/play <song name, URL, or file path>`"))

        if music.find_ffmpeg() is None:
            return Reply(embed=ui.error_embed(
                "FFmpeg isn't available on my host, so I can't play audio.\n"
                "Install it, or set `FFMPEG_PATH` in `.env` to the full path of `ffmpeg.exe`."
            ))

        # Checked before joining, so Amy doesn't connect only to refuse the track
        if len(player.queue) >= music.MAX_QUEUE_SIZE:
            return Reply(embed=ui.error_embed(f"The queue is full ({music.MAX_QUEUE_SIZE} tracks). Try again once it drains."))

        # Join the requester's channel if not already connected
        if voice.active_channel(vc) is None:
            state = member.voice if member else None
            if state is None or state.channel is None:
                return Reply(embed=ui.error_embed("You're not in a voice channel. Join one first, then use `/play`."))
            perms = state.channel.permissions_for(guild.me)
            if not perms.connect or not perms.speak:
                return Reply(embed=ui.error_embed(f"I need Connect and Speak permissions in **{state.channel.name}**."))
            try:
                async with voice_manager.lock_for(guild.id):
                    await voice.connect_to(state.channel)
                vc = voice.get_voice_client(guild)
            except RuntimeError as e:
                safe_print(f"[ERROR] Voice connect failed: {e}")
                return Reply(embed=ui.error_embed(
                    f"Voice support isn't fully installed on my host: {e}\n"
                    "Fix: `pip install \"discord.py[voice]\"`"
                ))
            except Exception as e:
                safe_print(f"[ERROR] Voice connect failed: {e}")
                return Reply(embed=ui.error_embed("I couldn't join your voice channel. Please try again."))
        elif not in_voice_with_amy() and not is_admin_member(interaction.guild, interaction.user.id):
            return Reply(embed=ui.error_embed("You need to be in my voice channel to queue tracks."))

        # advance_playback has no message context, so remember where to post the card
        player.text_channel_id = interaction.channel_id

        query = " ".join(parts[1:])
        # Echoed back into a Discord message, so keep it well under the 2000-char limit
        shown = query if len(query) <= 100 else query[:100] + "..."
        author = str(interaction.user)

        # Resolving hits the network and can take a few seconds, so acknowledge
        # immediately and edit this message once we know the result.
        status_msg = await interaction.followup.send(SEARCH + " Searching for **" + shown + "**...", wait=True)

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
        return Reply(embed=ui.error_embed("I'm not in a voice channel."))

    if command == "pause":
        if not in_voice_with_amy() and not is_admin_member(interaction.guild, interaction.user.id):
            return Reply(embed=ui.error_embed("You need to be in my voice channel to do that."))
        if not vc.is_playing():
            return Reply(embed=ui.error_embed("Nothing is playing."))
        vc.pause()
        await refresh_now_playing(guild, paused=True)
        return Reply(embed=ui.info_embed("Paused."))

    if command == "resume":
        if not in_voice_with_amy() and not is_admin_member(interaction.guild, interaction.user.id):
            return Reply(embed=ui.error_embed("You need to be in my voice channel to do that."))
        if not vc.is_paused():
            return Reply(embed=ui.error_embed("Nothing is paused."))
        vc.resume()
        await refresh_now_playing(guild, paused=False)
        return Reply(embed=ui.info_embed("Resumed."))

    if command == "skip":
        if not in_voice_with_amy() and not is_admin_member(interaction.guild, interaction.user.id):
            return Reply(embed=ui.error_embed("You need to be in my voice channel to skip."))
        if not (vc.is_playing() or vc.is_paused()):
            return Reply(embed=ui.error_embed("Nothing is playing."))
        skipped = player.current.title if player.current else "the current track"
        player.skip_requested = True
        vc.stop()  # triggers the after-callback, which advances the queue
        return Reply(embed=ui.info_embed(f"Skipped **{skipped}**."))

    if command == "stop":
        if not in_voice_with_amy() and not is_admin_member(interaction.guild, interaction.user.id):
            return Reply(embed=ui.error_embed("You need to be in my voice channel (or be an admin) to stop playback."))
        player.queue.clear()
        player.loop_mode = LoopMode.OFF
        player.skip_requested = False
        player.current = None
        # Stop only. vc.stop() fires the after-callback, which finds an empty queue,
        # shows the finished card and starts the idle timer - so Amy still leaves on her
        # own after 5 minutes. /leave is the command for disconnecting straight away.
        vc.stop()
        return Reply(embed=ui.info_embed(
            "Stopped and cleared the queue. I'll stay here — use `/leave` to send me away."))

    if command == "volume":
        if not is_admin_member(interaction.guild, interaction.user.id):
            return Reply(embed=ui.error_embed("You don't have permission to use this command. (Admin only)"))
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
        return Reply(embed=ui.info_embed(
            f"Volume set to **{parsed}%** — applies from the next track.\n"
            "_Note: below 100% Amy has to decode audio rather than pass it through, "
            "which costs more CPU and can stutter on a busy machine._"
        ))

    if command == "loop":
        if not in_voice_with_amy() and not is_admin_member(interaction.guild, interaction.user.id):
            return Reply(embed=ui.error_embed("You need to be in my voice channel to do that."))
        if len(parts) < 2:
            return Reply(embed=ui.info_embed(
                f"Loop is **{player.loop_mode.value}**.\n"
                "Usage: `/loop off`, `/loop track`, or `/loop queue`"
            ))
        try:
            player.loop_mode = LoopMode(parts[1].lower())
        except ValueError:
            return Reply(embed=ui.error_embed("Loop mode must be `off`, `track`, or `queue`."))
        return Reply(embed=ui.info_embed(f"Loop set to **{player.loop_mode.value}**."))

    if command == "remove":
        if not in_voice_with_amy() and not is_admin_member(interaction.guild, interaction.user.id):
            return Reply(embed=ui.error_embed("You need to be in my voice channel to do that."))
        if len(parts) < 2:
            return Reply(embed=ui.error_embed("Which one? Usage: `/remove <position>` (see `/queue`)"))
        try:
            index = int(parts[1])
        except ValueError:
            return Reply(embed=ui.error_embed("Position must be a number. Usage: `/remove <position>`"))

        target = music.peek_at(player.queue, index)
        if target is None:
            return Reply(embed=ui.error_embed("There's no track at position " + str(index) + "."))
        # Users may only remove what they queued; admins may remove anything
        if target.requested_by != str(interaction.user) and not is_admin_member(interaction.guild, interaction.user.id):
            return (DENY + " That track was queued by " + target.requested_by
                    + ". You can only remove your own.")

        music.remove_at(player.queue, index)  # `target` above already proved it exists
        return Reply(embed=ui.info_embed("Removed **" + target.title + "** from the queue."))

    if command == "shuffle":
        if not in_voice_with_amy() and not is_admin_member(interaction.guild, interaction.user.id):
            return Reply(embed=ui.error_embed("You need to be in my voice channel to do that."))
        if len(player.queue) < 2:
            return Reply(embed=ui.error_embed("Not enough tracks queued to shuffle."))
        music.shuffle_queue(player.queue)
        return Reply(embed=ui.info_embed("Shuffled **" + str(len(player.queue)) + "** queued track(s)."))

    if command == "clearqueue":
        if not is_admin_member(interaction.guild, interaction.user.id):
            return Reply(embed=ui.error_embed("You don't have permission to use this command. (Admin only)"))
        count = len(player.queue)
        if count == 0:
            return Reply(embed=ui.error_embed("The queue is already empty."))
        player.queue.clear()
        return Reply(embed=ui.info_embed(
            "Cleared **" + str(count) + "** queued track(s). Current track keeps playing."))

    if command == "skipto":
        if not in_voice_with_amy() and not is_admin_member(interaction.guild, interaction.user.id):
            return Reply(embed=ui.error_embed("You need to be in my voice channel to do that."))
        if len(parts) < 2:
            return Reply(embed=ui.error_embed("Skip to where? Usage: `/skipto <position>` (see `/queue`)"))
        try:
            index = int(parts[1])
        except ValueError:
            return Reply(embed=ui.error_embed("Position must be a number. Usage: `/skipto <position>`"))

        target = music.peek_at(player.queue, index)
        if target is None:
            return Reply(embed=ui.error_embed("There's no track at position " + str(index) + "."))

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

#----Status and Model------
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
        f"Ollama: {ollama_state} | Model: `{model}`\n"
        f"Voice: {voice_state}\n"
        f"Music: {music_state} | FFmpeg: {ffmpeg_state}\n"
        f"Memory: {stats['total_messages']} messages across {stats['active_channels']} channel(s)\n"
        f"Rate limits: {throttled} user(s) currently throttled"
    )


async def switch_model(name: str) -> str:
    """Show the active Ollama model, or switch to another installed one."""
    global model
    if not name:
        return f"\U0001F9E0 Current model: `{model}`\nPass a name to switch."

    loop = asyncio.get_running_loop()
    try:
        available = await loop.run_in_executor(None, ollama.list)
    except Exception as e:
        return f"\U0001F6AB Could not reach Ollama to verify the model: {e}"

    model_names = extract_model_names(available)
    if name not in model_names:
        names_list = ", ".join(f"`{n}`" for n in model_names) or "none installed"
        return f"\U0001F6AB Model `{name}` not found. Installed models: {names_list}"

    model = name
    safe_print(f"[INFO] Model switched to {model}")
    return f"\U0001F9E0 Model switched to `{model}`"
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
    interaction: discord.Interaction, command: str, args: Optional[List[str]] = None
) -> None:
    """Run a music command that needs no network work, so it can answer immediately."""
    guild = await require_guild(interaction)
    if guild is None:
        return
    if command not in MUSIC_READONLY_COMMANDS and not await voice_gate(interaction):
        return
    parts = [command] + (args or [])
    await send_reply(
        interaction, await execute_music_command(command, parts, interaction, guild)
    )
#--------------------------------------

#----Slash Commands------
# Registration only. Each validates through Discord's own typing, then delegates to the
# handlers above. Commands doing network work defer first: Discord requires a response
# within 3 seconds and yt-dlp resolution takes several.

@tree.command(name="help", description="Show Amy's commands")
async def slash_help(interaction: discord.Interaction) -> None:
    is_adm = is_admin_member(interaction.guild, interaction.user.id)
    await interaction.response.send_message(
        HELP_EVERYONE + (HELP_ADMIN if is_adm else ""), ephemeral=True)


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
    await send_reply(interaction,
                     await execute_music_command("play", ["play"] + query.split(),
                                                 interaction, guild))


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
                     await execute_music_command("search", ["search"] + query.split(),
                                                 interaction, guild))


@tree.command(name="pause", description="Pause playback")
@app_commands.guild_only()
async def slash_pause(interaction: discord.Interaction) -> None:
    await simple_music(interaction, "pause")


@tree.command(name="resume", description="Resume playback")
@app_commands.guild_only()
async def slash_resume(interaction: discord.Interaction) -> None:
    await simple_music(interaction, "resume")


@tree.command(name="skip", description="Skip the current track")
@app_commands.guild_only()
async def slash_skip(interaction: discord.Interaction) -> None:
    await simple_music(interaction, "skip")


@tree.command(name="stop", description="Stop playback and clear the queue (Amy stays)")
@app_commands.guild_only()
async def slash_stop(interaction: discord.Interaction) -> None:
    await simple_music(interaction, "stop")


@tree.command(name="queue", description="Show what's playing and what's queued")
@app_commands.describe(page="Page number (10 tracks per page)")
@app_commands.guild_only()
async def slash_queue(
    interaction: discord.Interaction,
    page: app_commands.Range[int, 1, 100] = 1,
) -> None:
    await simple_music(interaction, "queue", [str(page)])


@tree.command(name="nowplaying", description="Show the current track")
@app_commands.guild_only()
async def slash_nowplaying(interaction: discord.Interaction) -> None:
    await simple_music(interaction, "nowplaying")


@tree.command(name="remove", description="Remove a track you queued")
@app_commands.describe(position="Queue position, as shown by /queue")
@app_commands.guild_only()
async def slash_remove(
    interaction: discord.Interaction,
    position: app_commands.Range[int, 1, 100],
) -> None:
    await simple_music(interaction, "remove", [str(position)])


@tree.command(name="skipto", description="Jump ahead to a queued track")
@app_commands.describe(position="Queue position, as shown by /queue")
@app_commands.guild_only()
async def slash_skipto(
    interaction: discord.Interaction,
    position: app_commands.Range[int, 1, 100],
) -> None:
    await simple_music(interaction, "skipto", [str(position)])


@tree.command(name="shuffle", description="Shuffle the queued tracks")
@app_commands.guild_only()
async def slash_shuffle(interaction: discord.Interaction) -> None:
    await simple_music(interaction, "shuffle")


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
    await simple_music(interaction, "loop", [mode.value])


@tree.command(name="clearqueue", description="Empty the queue (current track keeps playing)")
@app_commands.guild_only()
@admin_only()
async def slash_clearqueue(interaction: discord.Interaction) -> None:
    await simple_music(interaction, "clearqueue")


@tree.command(name="volume", description="Show or set playback volume")
@app_commands.describe(level="Volume percent (omit to see the current value)")
@app_commands.guild_only()
@admin_only()
async def slash_volume(
    interaction: discord.Interaction,
    level: Optional[app_commands.Range[int, 0, 100]] = None,
) -> None:
    args = [str(level)] if level is not None else []
    await simple_music(interaction, "volume", args)


@tree.command(name="toggle", description="Enable or disable Amy's chat replies")
@admin_only()
async def slash_toggle(interaction: discord.Interaction) -> None:
    global bot_enabled
    bot_enabled = not bot_enabled
    state = "enabled" if bot_enabled else "disabled"
    safe_print("[INFO] Bot is now " + state)
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
    await send_reply(interaction, await switch_model(name))


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

#----Event Handlers for Discord Bot------
@bot.event
async def on_ready() -> None:
    safe_print(f'{bot.user} is online!')
    activity = discord.Activity(type=discord.ActivityType.watching, name="conversations")
    await bot.change_presence(activity=activity, status=discord.Status.online)
    safe_print("[INFO] Bot status set to: Watching conversations")

    # Register commands with Discord. Guild-scoped so they appear immediately;
    # a global sync can take up to an hour to propagate.
    try:
        if GUILD_ID:
            scope = discord.Object(id=GUILD_ID)
            tree.copy_global_to(guild=scope)
            synced = await tree.sync(guild=scope)
            safe_print(f"[INFO] Synced {len(synced)} slash command(s) to guild {GUILD_ID}")
        else:
            synced = await tree.sync()
            safe_print(f"[INFO] Synced {len(synced)} slash command(s) globally "
                       "(may take up to an hour to appear - set GUILD_ID for instant sync)")
    except Exception as e:
        safe_print(f"[ERROR] Could not sync slash commands: {e}")

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

        # Commands are slash commands now and arrive as interactions, not messages.
        # Anything reaching here is conversation.
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
