"""
Lightweight stand-ins for Discord objects.

The command handlers take a `discord.Interaction`, but constructing a real one needs a live
gateway connection. These fakes expose only the handful of attributes the handlers actually
touch (`guild`, `user`, `channel_id`, `followup`), which keeps the command tests offline.
"""
import asyncio
import importlib.util
import os
import sys
from collections import deque

PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_bot():
    """Import Amy_chatbot_V2 as a module, with duck-typed voice helpers for the fakes."""
    sys.path.insert(0, PROJ)
    os.chdir(PROJ)
    spec = importlib.util.spec_from_file_location("amy", os.path.join(PROJ, "Amy_chatbot_V2.py"))
    amy = importlib.util.module_from_spec(spec)
    sys.modules["amy"] = amy
    spec.loader.exec_module(amy)

    # The real helpers isinstance-check against discord.VoiceClient / VoiceChannel, which
    # these fakes are not. The narrowing itself is exercised against real types elsewhere;
    # what these tests target is the command decision flow.
    amy.voice.get_voice_client = lambda g: getattr(g, "voice_client", None)

    def _active_channel(vc):
        if vc is None or not vc.is_connected():
            return None
        return vc.channel

    amy.voice.active_channel = _active_channel
    return amy


class Perms:
    def __init__(self, connect=True, speak=True, manage_channels=True, administrator=False):
        self.connect = connect
        self.speak = speak
        self.manage_channels = manage_channels
        self.administrator = administrator


class VoiceChannel:
    def __init__(self, id_, name, members=(), perms=None):
        self.id = id_
        self.name = name
        self.members = list(members)
        self._perms = perms or Perms()

    def permissions_for(self, _who):
        return self._perms


class VoiceClient:
    def __init__(self, channel):
        self.channel = channel
        self.stopped = False
        self.disconnected = False
        self.moved_to = None
        self._paused = False

    def is_connected(self):
        return not self.disconnected

    def is_playing(self):
        return not self.stopped and not self._paused

    def is_paused(self):
        return self._paused

    def pause(self):
        self._paused = True

    def resume(self):
        self._paused = False

    def stop(self):
        self.stopped = True

    async def move_to(self, ch):
        self.moved_to = ch
        self.channel = ch

    async def disconnect(self, force=False):
        self.disconnected = True


class Member:
    def __init__(self, id_, voice_channel=None, is_bot=False, name="user"):
        self.id = id_
        self.bot = is_bot
        self.roles = []
        self.guild_permissions = Perms()
        self._name = name
        self.voice = type("VS", (), {"channel": voice_channel})() if voice_channel else None

    def __str__(self):
        return self._name


class Guild:
    def __init__(self, owner_id, member, voice_client=None, me_perms=None, guild_id=500):
        self.id = guild_id
        self.owner_id = owner_id
        self._member = member
        self.voice_client = voice_client
        self.me = type("Me", (), {"guild_permissions": me_perms or Perms()})()

    def get_member(self, uid):
        return self._member if self._member is not None and self._member.id == uid else None

    def get_channel(self, cid):
        """Channel lookup for restore paths. Returns None unless one was registered."""
        return getattr(self, "_channels", {}).get(cid)

    def add_channel(self, channel):
        self._channels = getattr(self, "_channels", {})
        self._channels[channel.id] = channel
        return channel


class SentMessage:
    """What followup.send returns - editable and deletable, like a WebhookMessage."""
    def __init__(self, content=None, embed=None, view=None):
        self.content = content
        self.embed = embed
        self.view = view
        self.deleted = False
        self.edits = []

    async def edit(self, content=None, embed=None, view=None):
        self.content = content
        self.embed = embed
        self.view = view
        self.edits.append((content, embed))

    async def delete(self):
        self.deleted = True

    async def add_reaction(self, emoji):
        pass


class Followup:
    def __init__(self, owner):
        self._owner = owner

    async def send(self, content=None, *, embed=None, view=None, ephemeral=False, wait=False):
        m = SentMessage(content, embed, view)
        self._owner.sent.append(m)
        return m if wait else None


class Response:
    def __init__(self, owner):
        self._owner = owner
        self._done = False

    def is_done(self):
        return self._done

    async def defer(self, ephemeral=False):
        self._done = True

    async def send_message(self, content=None, *, embed=None, view=None, ephemeral=False):
        self._done = True
        self._owner.sent.append(SentMessage(content, embed, view))

    async def edit_message(self, content=None, embed=None, view=None):
        self._done = True
        self._owner.sent.append(SentMessage(content, embed, view))


class Interaction:
    """Stands in for discord.Interaction across the command tests."""
    def __init__(self, user, guild, channel_id=77):
        self.user = user
        self.guild = guild
        self.channel_id = channel_id
        self.channel = type("C", (), {"id": channel_id, "category": None})()
        self.sent = []
        self.response = Response(self)
        self.followup = Followup(self)


def text_of(result, interaction=None):
    """
    Flatten a handler's return value into searchable text.

    Handlers return a str, a ui.Reply, or "" when they sent their own message - in the last
    case the text lives on the interaction, so pass it in to include what was sent.
    """
    parts = []

    def add_embed(e):
        if e is None:
            return
        parts.extend([e.title or "", e.description or ""])
        if e.author:
            parts.append(e.author.name or "")
        for f in e.fields:
            parts.extend([f.name or "", str(f.value or "")])

    if isinstance(result, str):
        parts.append(result)
    elif result is not None:
        parts.append(getattr(result, "content", "") or "")
        add_embed(getattr(result, "embed", None))

    if interaction is not None:
        for m in interaction.sent:
            parts.append(m.content or "")
            add_embed(m.embed)

    return chr(10).join(p for p in parts if p)


def run_music(amy, command, args=None, user=None, guild=None, interaction=None):
    """Drive a music command the way the slash layer does, returning flattened text."""
    interaction = interaction or Interaction(user, guild)
    parts = [command] + [str(a) for a in (args or [])]
    result = asyncio.run(
        amy.execute_music_command(command, parts, interaction, interaction.guild))
    return text_of(result, interaction)


def run_voice(amy, command, args=None, user=None, guild=None, interaction=None):
    """Drive a voice command the way the slash layer does, returning flattened text."""
    interaction = interaction or Interaction(user, guild)
    parts = [command] + [str(a) for a in (args or [])]
    result = asyncio.run(
        amy.execute_voice_command(command, parts, interaction, interaction.guild))
    return text_of(result, interaction)


def make_track(amy, title, duration=60, who="userA"):
    return amy.music.Track(title=title, query=title, duration=duration, requested_by=who)


def seed_queue(amy, guild_id, titles, owners=None, current="playing"):
    """Reset a guild's player and fill its queue. Returns the player."""
    owners = owners or {}
    amy.music_manager.cleanup(guild_id)
    p = amy.music_manager.player_for(guild_id)
    p.queue = deque([make_track(amy, t, who=owners.get(t, "userA")) for t in titles])
    if current:
        p.current = make_track(amy, current)
    return p
