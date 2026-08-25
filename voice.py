# voice.py
"""Voice channel connection management for the Amy chatbot."""

import asyncio
from enum import Enum
from typing import Dict, Optional, Set, Union

import discord

DEFAULT_CHANNEL_NAME: str = "Amy's Room"
MAX_CHANNEL_NAME_LEN: int = 100
EMPTY_DISCONNECT_DELAY: int = 60  # Seconds to wait before leaving an empty channel


class JoinAction(Enum):
    """What /join should do, given where Amy currently is."""
    ALREADY_THERE = "already_there"
    CONNECT = "connect"
    MOVE = "move"
    BLOCKED_OCCUPIED = "blocked_occupied"


#----Pure Helpers (no Discord objects, unit testable)------
def sanitize_channel_name(raw: str) -> str:
    """Collapse whitespace, fall back to a default, and clamp to Discord's name limit."""
    name = " ".join(raw.split()).strip()
    if not name:
        return DEFAULT_CHANNEL_NAME
    return name[:MAX_CHANNEL_NAME_LEN]


def decide_join_action(
    current_channel_id: Optional[int],
    current_channel_has_humans: bool,
    target_channel_id: int,
    requester_is_admin: bool,
) -> JoinAction:
    """
    Decide what /join should do. Kept free of Discord objects so it can be tested directly.

    Amy is only blocked from moving when she is already sitting with other people and the
    requester is not an admin - that stops someone yanking her out of an active session.
    """
    if current_channel_id is None:
        return JoinAction.CONNECT
    if current_channel_id == target_channel_id:
        return JoinAction.ALREADY_THERE
    if current_channel_has_humans and not requester_is_admin:
        return JoinAction.BLOCKED_OCCUPIED
    return JoinAction.MOVE
#--------------------------------------


#----Discord-facing Helpers------
# discord.py types VoiceClient.channel as VoiceChannel | StageChannel; both are connectable
# and both expose .members, so accept either.
VocalChannel = Union[discord.VoiceChannel, discord.StageChannel]


def get_voice_client(guild: discord.Guild) -> Optional[discord.VoiceClient]:
    """
    Narrow guild.voice_client to a VoiceClient.

    discord.py declares the property as Optional[VoiceProtocol] (the abstract base, which
    has no is_connected/move_to), but connect() returns a VoiceClient. This makes that
    concrete so callers get real typing instead of the base class.
    """
    vc = guild.voice_client
    return vc if isinstance(vc, discord.VoiceClient) else None


def active_channel(vc: Optional[discord.VoiceClient]) -> Optional[VocalChannel]:
    """The channel a voice client is currently in, or None if not connected."""
    if vc is None or not vc.is_connected():
        return None
    channel = vc.channel
    return channel if isinstance(channel, (discord.VoiceChannel, discord.StageChannel)) else None


def humans_in(channel: VocalChannel) -> int:
    """Count non-bot members currently in a voice channel."""
    return sum(1 for m in channel.members if not m.bot)


async def connect_to(channel: VocalChannel) -> discord.VoiceClient:
    """Connect to a voice channel. Amy never needs to receive audio, so she self-deafens."""
    return await channel.connect(self_deaf=True)
#--------------------------------------


class VoiceManager:
    """Tracks Amy-created voice channels and serialises voice actions per guild."""

    def __init__(self, db=None) -> None:
        self.db = db
        self.bot_created: Set[int] = set()
        self._locks: Dict[int, asyncio.Lock] = {}

    def lock_for(self, guild_id: int) -> asyncio.Lock:
        """One lock per guild, so two concurrent /join calls can't race."""
        if guild_id not in self._locks:
            self._locks[guild_id] = asyncio.Lock()
        return self._locks[guild_id]

    def mark_created(self, channel_id: int, guild_id: int) -> None:
        self.bot_created.add(channel_id)
        if self.db:
            self.db.add_bot_voice_channel(channel_id, guild_id)

    def unmark_created(self, channel_id: int) -> None:
        self.bot_created.discard(channel_id)
        if self.db:
            self.db.remove_bot_voice_channel(channel_id)

    def is_bot_created(self, channel_id: int) -> bool:
        return channel_id in self.bot_created


async def leave_voice(guild: discord.Guild, manager: VoiceManager) -> Optional[str]:
    """
    Disconnect from the guild's voice channel, deleting it if Amy created it.
    Returns the channel name that was left, or None if not connected.
    """
    vc = get_voice_client(guild)
    channel = active_channel(vc)
    if vc is None or channel is None:
        return None

    name = channel.name
    await vc.disconnect(force=False)

    if manager.is_bot_created(channel.id):
        try:
            await channel.delete(reason="Amy created this channel and has now left")
        except discord.HTTPException:
            pass
        manager.unmark_created(channel.id)

    return name


async def sweep_orphan_channels(bot: discord.Client, db, manager: VoiceManager) -> int:
    """
    On startup, clean up voice channels Amy created before a restart.
    Empty ones are deleted; occupied ones are re-adopted so they get cleaned up later.
    Returns the number of channels deleted.
    """
    if db is None:
        return 0

    deleted = 0
    for channel_id, _guild_id in db.get_bot_voice_channels():
        channel = bot.get_channel(channel_id)
        if not isinstance(channel, (discord.VoiceChannel, discord.StageChannel)):
            # Gone, or somehow no longer a voice channel - drop the stale row
            db.remove_bot_voice_channel(channel_id)
            continue

        if humans_in(channel) == 0:
            try:
                await channel.delete(reason="Orphaned Amy voice channel cleaned up on startup")
                deleted += 1
            except discord.HTTPException:
                pass
            db.remove_bot_voice_channel(channel_id)
        else:
            manager.bot_created.add(channel_id)

    return deleted
