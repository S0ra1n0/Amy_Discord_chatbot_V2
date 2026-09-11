# ui.py
"""Presentation layer: embed builders and the reply wrapper.

Kept free of bot state so every builder can be constructed and asserted offline,
the same property that makes music.render_queue and voice.decide_join_action testable.
"""

from dataclasses import dataclass
from typing import Optional

import discord

import music
from music import LoopMode, Track

#----Palette------
COLOUR_OK: int = 0x57F287       # green  - something succeeded
COLOUR_INFO: int = 0x5865F2     # blurple - neutral information
COLOUR_WARN: int = 0xFEE75C     # yellow - refused, but not an error
COLOUR_ERROR: int = 0xED4245    # red    - something failed
#--------------------------------------

# Discord's hard limits; exceeding either makes the API reject the whole message
MAX_EMBED_TITLE: int = 256
MAX_EMBED_DESC: int = 4096
MAX_FIELD_VALUE: int = 1024


def _clip(text: str, limit: int) -> str:
    """Trim text to a Discord field limit, leaving room for an ellipsis."""
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "\u2026"


@dataclass
class Reply:
    """
    What a command handler hands back.

    Handlers may still return a plain str; this exists for the ones that need an embed
    and/or interactive components alongside (or instead of) text.
    """
    content: Optional[str] = None
    embed: Optional[discord.Embed] = None
    view: Optional[discord.ui.View] = None


#----Embed Builders------
def now_playing_embed(
    track: Track,
    queue_len: int = 0,
    loop_mode: LoopMode = LoopMode.OFF,
    volume: float = 1.0,
    paused: bool = False,
    stopped: bool = False,
    elapsed: Optional[float] = None,
) -> discord.Embed:
    """
    The main player card. `stopped` renders the finished state after /stop or an empty queue.

    `elapsed` adds a progress bar. It is omitted on the finished card, where a position
    would be meaningless, and for tracks of unknown length it degrades to the elapsed time.
    """
    if stopped:
        heading, colour = "Playback finished", COLOUR_INFO
    elif paused:
        heading, colour = "Paused", COLOUR_WARN
    else:
        heading, colour = "Now playing", COLOUR_OK

    embed = discord.Embed(
        title=_clip(track.title, MAX_EMBED_TITLE),
        colour=colour,
        # Only a real http(s) link is clickable; local files have a filesystem path
        url=track.query if track.query.startswith("http") else None,
    )
    embed.set_author(name=heading)
    if elapsed is not None and not stopped:
        # Full width, so it reads as a scrubber rather than another stat
        embed.add_field(name="Position",
                        value=music.progress_bar(elapsed, track.duration),
                        inline=False)
    else:
        embed.add_field(name="Duration",
                        value=music.format_duration(track.duration), inline=True)
    embed.add_field(name="Requested by", value=_clip(track.requested_by, MAX_FIELD_VALUE), inline=True)
    embed.add_field(name="Volume", value=f"{int(volume * 100)}%", inline=True)

    if queue_len:
        embed.add_field(name="Up next", value=f"{queue_len} track(s)", inline=True)
    if loop_mode is not LoopMode.OFF:
        embed.add_field(name="Loop", value=loop_mode.value, inline=True)
    if track.thumbnail:
        embed.set_thumbnail(url=track.thumbnail)
    return embed


def queued_embed(track: Track, position: int) -> discord.Embed:
    """Confirmation that a single track joined the queue behind something already playing."""
    embed = discord.Embed(
        title=_clip(track.title, MAX_EMBED_TITLE),
        colour=COLOUR_OK,
        url=track.query if track.query.startswith("http") else None,
    )
    embed.set_author(name="Added to queue")
    embed.add_field(name="Duration", value=music.format_duration(track.duration), inline=True)
    embed.add_field(name="Position", value=str(position), inline=True)
    embed.add_field(name="Requested by", value=_clip(track.requested_by, MAX_FIELD_VALUE), inline=True)
    if track.thumbnail:
        embed.set_thumbnail(url=track.thumbnail)
    return embed


def playlist_embed(title: str, added: int, skipped: int = 0) -> discord.Embed:
    """Summary after a playlist URL is expanded into many tracks."""
    embed = discord.Embed(
        title=_clip(title, MAX_EMBED_TITLE),
        colour=COLOUR_OK,
    )
    embed.set_author(name="Playlist added")
    embed.add_field(name="Queued", value=f"{added} track(s)", inline=True)
    if skipped:
        embed.add_field(
            name="Skipped", value=f"{skipped} (unavailable or over the limit)", inline=True
        )
    return embed


def queue_embed(
    current: Optional[Track],
    queue,
    loop_mode: LoopMode,
    page: int = 1,
) -> discord.Embed:
    """The /queue listing. Body comes from music.render_queue so pagination stays in one place."""
    body = music.render_queue(current, queue, loop_mode, page=page)
    embed = discord.Embed(
        description=_clip(body, MAX_EMBED_DESC),
        colour=COLOUR_INFO,
    )
    embed.set_author(name="Queue")
    return embed


MAX_SELECT_LABEL: int = 100     # Discord's limit on a select option label


def search_results_embed(query: str, tracks) -> discord.Embed:
    """Numbered list of search hits, matching the order of the select menu below it."""
    lines = []
    for i, track in enumerate(tracks, start=1):
        lines.append(
            f"`{i}.` {track.title} `[{music.format_duration(track.duration)}]`"
        )
    embed = discord.Embed(
        description=_clip(chr(10).join(lines), MAX_EMBED_DESC),
        colour=COLOUR_INFO,
    )
    embed.set_author(name=f"Results for: {_clip(query, 200)}")
    embed.set_footer(text="Pick one from the menu below")
    return embed


def search_web_embed(query: str, results) -> discord.Embed:
    """Raw web results for /websearch, with clickable titles."""
    if not results:
        embed = discord.Embed(
            description="Nothing came back for that search.", colour=COLOUR_WARN)
        embed.set_author(name="Web search")
        return embed

    lines = []
    for i, r in enumerate(results, start=1):
        lines.append("`%d.` [%s](%s)" % (i, _clip(r.title, 120), r.url))
        if r.snippet:
            lines.append("_%s_" % _clip(r.snippet, 200))
    embed = discord.Embed(
        description=_clip(chr(10).join(lines), MAX_EMBED_DESC), colour=COLOUR_INFO)
    embed.set_author(name="Web search: %s" % _clip(query, 200))
    return embed


def voice_embed(description: str, colour: int = COLOUR_OK, heading: str = "Voice") -> discord.Embed:
    """Generic card for join/leave/create replies."""
    embed = discord.Embed(description=_clip(description, MAX_EMBED_DESC), colour=colour)
    embed.set_author(name=heading)
    return embed


def info_embed(description: str, heading: str = "Music") -> discord.Embed:
    embed = discord.Embed(description=_clip(description, MAX_EMBED_DESC), colour=COLOUR_INFO)
    embed.set_author(name=heading)
    return embed


def error_embed(description: str, heading: str = "Can't do that") -> discord.Embed:
    """Refusals and failures. Warn-yellow rather than red - most are permission refusals."""
    embed = discord.Embed(description=_clip(description, MAX_EMBED_DESC), colour=COLOUR_WARN)
    embed.set_author(name=heading)
    return embed
#--------------------------------------
