# music.py
"""Music playback: track resolution, per-guild queues, and the playback engine."""

import asyncio
import os
import random
import shutil
import urllib.parse
from collections import deque
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Coroutine, Deque, Dict, List, Optional, Tuple

import discord

# Set by the bot at startup so this module can log without importing it back
log: Callable[[str], None] = print

#----Configuration------
# -nostdin stops FFmpeg competing for the console; the reconnect flags keep a stream
# alive through transient network drops instead of ending the track. All verified
# supported by FFmpeg 9.x.
FFMPEG_BEFORE_OPTIONS: str = (
    "-nostdin "
    "-reconnect 1 -reconnect_streamed 1 -reconnect_on_network_error 1 "
    "-reconnect_delay_max 5"
)
FFMPEG_OPTIONS: str = "-vn"

IDLE_DISCONNECT_DELAY: int = 300  # Leave 5 minutes after the queue runs dry
MAX_QUEUE_SIZE: int = 100
MAX_PLAYLIST_TRACKS: int = 50  # Cap one playlist so it can't monopolise the queue
QUEUE_PAGE_SIZE: int = 10

# Emoji used in queue listings, as escapes so the source stays ASCII-safe
NOW_PLAYING_PREFIX: str = "🎵"  # musical note
LOOP_PREFIX: str = "🔁"        # repeat arrows

# Full volume by default so playback can take the cheap opus-passthrough path (see
# build_source). Listeners can still adjust Amy per-user in Discord's own volume slider.
DEFAULT_VOLUME: float = 1.0
# At/above this, volume is effectively unchanged, so no PCM transform is needed
OPUS_PASSTHROUGH_THRESHOLD: float = 0.99


def get_music_dir() -> Optional[str]:
    """The directory local playback is restricted to, or None if the feature is off."""
    raw = os.getenv("MUSIC_DIR")
    if not raw:
        return None
    path = os.path.realpath(raw)
    return path if os.path.isdir(path) else None


def resolve_local_path(query: str) -> Optional[str]:
    """
    Resolve `query` to a file inside MUSIC_DIR, or None if it isn't one.

    Local playback is opt-in and sandboxed on purpose. Treating any /play argument as a
    possible filesystem path would let any member of the server probe the host for files
    (including .env) and have FFmpeg open them. realpath() collapses ".." and symlinks
    before the containment check, so escaping the directory isn't possible, and an
    absolute path outside it is rejected too.
    """
    base = get_music_dir()
    if base is None:
        return None
    candidate = os.path.realpath(os.path.join(base, query))
    if candidate != base and not candidate.startswith(base + os.sep):
        return None
    return candidate if os.path.isfile(candidate) else None


def find_ffmpeg() -> Optional[str]:
    """
    Locate the FFmpeg binary.

    Prefers an explicit FFMPEG_PATH from .env, then falls back to PATH. The explicit
    option matters on Windows, where a fresh install updates PATH but already-running
    processes keep the stale copy until they restart.
    """
    explicit = os.getenv("FFMPEG_PATH")
    if explicit and os.path.isfile(explicit):
        return explicit
    return shutil.which("ffmpeg")
#--------------------------------------


class LoopMode(Enum):
    OFF = "off"
    TRACK = "track"
    QUEUE = "queue"


@dataclass
class Track:
    """
    A queued item. `query` is resolved to a playable stream URL at play time, not at
    enqueue time - YouTube stream URLs expire after a few hours, so resolving a long
    queue up front would leave later tracks pointing at dead links.
    """
    title: str
    query: str
    duration: Optional[int] = None
    requested_by: str = "unknown"
    is_local: bool = False


#----Pure Helpers (no Discord/network, unit testable)------
def format_duration(seconds: Optional[int]) -> str:
    """Render seconds as m:ss, or h:mm:ss past an hour. Unknown durations show as '?:??'."""
    if seconds is None or seconds < 0:
        return "?:??"
    seconds = int(seconds)
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def parse_volume(raw: str) -> Optional[int]:
    """Parse a 0-100 volume argument. Returns None if it isn't a valid percentage."""
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    if not 0 <= value <= 100:
        return None
    return value


def advance_queue(
    current: Optional[Track],
    queue: Deque[Track],
    mode: LoopMode,
    force_next: bool = False,
) -> Optional[Track]:
    """
    Pick the next track, applying the loop mode. Mutates `queue`.

    OFF   - drop the finished track, take the next one
    TRACK - replay the finished track
    QUEUE - send the finished track to the back, take the next one

    `force_next` is set when the user explicitly asked to skip. Without it, TRACK loop
    would replay the same song and /skip would appear to do nothing. QUEUE rotation is
    still honoured on a skip, so a skipped track returns later in the cycle.
    """
    if mode is LoopMode.TRACK and current is not None and not force_next:
        return current
    if mode is LoopMode.QUEUE and current is not None:
        queue.append(current)
    return queue.popleft() if queue else None


def total_pages(queue_len: int, page_size: int = QUEUE_PAGE_SIZE) -> int:
    """How many pages a queue of this length spans. Always at least 1."""
    if queue_len <= 0:
        return 1
    return (queue_len + page_size - 1) // page_size


def clamp_page(page: int, queue_len: int, page_size: int = QUEUE_PAGE_SIZE) -> int:
    """Clamp a requested page number into the valid range."""
    if page < 1:
        return 1
    return min(page, total_pages(queue_len, page_size))


def remove_at(queue: Deque[Track], index: int) -> Optional[Track]:
    """
    Remove the track at 1-based position `index` and return it.
    Returns None if the position doesn't exist, so callers can report a clean error.
    """
    if index < 1 or index > len(queue):
        return None
    items = list(queue)
    track = items.pop(index - 1)
    queue.clear()
    queue.extend(items)
    return track


def peek_at(queue: Deque[Track], index: int) -> Optional[Track]:
    """Look at the track at 1-based position `index` without removing it."""
    if index < 1 or index > len(queue):
        return None
    return list(queue)[index - 1]


def shuffle_queue(queue: Deque[Track]) -> None:
    """Randomise the pending queue in place. The currently playing track is untouched."""
    items = list(queue)
    random.shuffle(items)
    queue.clear()
    queue.extend(items)


def drop_before(queue: Deque[Track], index: int) -> int:
    """
    Drop everything before 1-based position `index` (used by /skipto).
    Returns how many tracks were discarded; 0 if the position is invalid.
    """
    if index < 1 or index > len(queue):
        return 0
    dropped = index - 1
    for _ in range(dropped):
        queue.popleft()
    return dropped


def render_queue(
    current: Optional[Track],
    queue: Deque[Track],
    loop_mode: LoopMode,
    page: int = 1,
    page_size: int = QUEUE_PAGE_SIZE,
) -> str:
    """
    Human-readable queue listing for /queue, one page at a time.

    Positions shown are absolute (page 2 starts at 11) so the numbers line up with
    what /remove and /skipto expect.
    """
    lines: List[str] = []
    if current is not None:
        lines.append(NOW_PLAYING_PREFIX + " **Now playing:** " + current.title
                     + " `[" + format_duration(current.duration) + "]`")
        lines.append("   requested by " + current.requested_by)
    else:
        lines.append(NOW_PLAYING_PREFIX + " **Nothing is playing.**")

    if queue:
        pages = total_pages(len(queue), page_size)
        page = clamp_page(page, len(queue), page_size)
        start_i = (page - 1) * page_size

        lines.append("")
        total = total_duration(queue)
        header = "**Up next (" + str(len(queue)) + " track(s)"
        if total is not None:
            header += ", " + format_duration(total) + " total"
        header += ") — page " + str(page) + "/" + str(pages) + ":**"
        lines.append(header)

        for offset, track in enumerate(list(queue)[start_i:start_i + page_size]):
            position = start_i + offset + 1
            lines.append("`" + str(position) + ".` " + track.title
                         + " `[" + format_duration(track.duration) + "]` — "
                         + track.requested_by)
        if pages > 1:
            lines.append("_Use `/queue <page>` to see more (1-" + str(pages) + ")._")

    if loop_mode is not LoopMode.OFF:
        lines.append("")
        lines.append(LOOP_PREFIX + " Loop: **" + loop_mode.value + "**")

    return "\n".join(lines)


def total_duration(queue: Deque[Track]) -> Optional[int]:
    """Sum queue duration, or None if any track's length is unknown."""
    total = 0
    for track in queue:
        if track.duration is None:
            return None
        total += track.duration
    return total
#--------------------------------------


#----Track Resolution------
# yt-dlp types its params as a TypedDict; this is an open options bag, so keep it untyped
YTDL_OPTIONS: Any = {
    # Prefer an opus stream so build_source can copy it through untouched
    "format": "bestaudio[acodec=opus]/bestaudio/best",
    "noplaylist": True,
    "quiet": True,
    "no_warnings": True,
    "default_search": "ytsearch",
    "ignoreerrors": True,
    "skip_download": True,
}


# Playlist lookups run "flat": metadata only, no per-video resolution, so a 50-track
# playlist costs one request instead of fifty. Stream URLs are still resolved lazily at
# play time by resolve_stream_url, which is what keeps expiring links from piling up.
YTDL_FLAT_OPTIONS: Any = dict(
    YTDL_OPTIONS,
    noplaylist=False,
    extract_flat=True,
    ignoreerrors=True,
)


def is_playlist_url(query: str) -> bool:
    """
    True only for a genuine playlist URL.

    A share link like watch?v=VIDEO&list=PLAYLIST carries a list id but is a request for
    that one video, so it deliberately returns False - otherwise pasting an ordinary
    YouTube link could dump hundreds of tracks into the queue.
    """
    try:
        parsed = urllib.parse.urlparse(query.strip())
    except ValueError:
        return False
    if parsed.scheme not in ("http", "https"):
        return False

    params = urllib.parse.parse_qs(parsed.query)
    if "list" not in params:
        return False
    if "v" in params:
        return False        # watch?v=VIDEO&list=... -> that one video
    # youtu.be/VIDEO?list=... carries the video id in the path, not a "v" param
    if parsed.netloc.lower().endswith("youtu.be") and parsed.path.strip("/"):
        return False
    return True


def _ytdl_extract_flat(url: str) -> Dict[str, Any]:
    """Blocking flat playlist lookup. Always called through an executor."""
    import yt_dlp

    with yt_dlp.YoutubeDL(YTDL_FLAT_OPTIONS) as ydl:
        raw = ydl.extract_info(url, download=False)
    if raw is None:
        raise ValueError("no result")
    return dict(raw)


def _ytdl_extract(query: str) -> Dict[str, Any]:
    """Blocking yt-dlp lookup. Always called through an executor."""
    import yt_dlp

    with yt_dlp.YoutubeDL(YTDL_OPTIONS) as ydl:
        raw = ydl.extract_info(query, download=False)
    if raw is None:
        raise ValueError("no result")
    info: Dict[str, Any] = dict(raw)
    # A search returns a playlist wrapper; take the first entry
    if "entries" in info:
        entries = [e for e in info["entries"] if e]
        if not entries:
            raise ValueError("no result")
        info = dict(entries[0])
    return info


async def resolve_metadata(query: str, requested_by: str) -> Track:
    """
    Build a Track from a query, URL, or local file path.
    Only fetches title/duration - the stream URL is resolved later, at play time.
    """
    local = resolve_local_path(query)
    if local is not None:
        return Track(
            title=os.path.basename(local),
            query=local,
            duration=None,
            requested_by=requested_by,
            is_local=True,
        )

    loop = asyncio.get_running_loop()
    info = await loop.run_in_executor(None, _ytdl_extract, query)
    return Track(
        title=info.get("title") or "Unknown title",
        # Prefer the canonical page URL so re-resolution at play time stays valid
        query=info.get("webpage_url") or query,
        duration=info.get("duration"),
        requested_by=requested_by,
    )


async def resolve_playlist(
    url: str, requested_by: str, limit: int = MAX_PLAYLIST_TRACKS
) -> Tuple[str, List[Track], int]:
    """
    Resolve a playlist URL into Tracks.

    Returns (playlist_title, tracks, skipped). `skipped` counts both entries yt-dlp
    could not read (private, deleted, age-gated) and those cut by `limit`, so the caller
    can tell the user exactly what did not load.
    """
    loop = asyncio.get_running_loop()
    info = await loop.run_in_executor(None, _ytdl_extract_flat, url)

    raw_entries = info.get("entries") or []
    usable = []
    unavailable = 0
    for entry in raw_entries:
        if not entry:
            unavailable += 1          # ignoreerrors turns failed entries into None
            continue
        page = entry.get("url") or entry.get("webpage_url") or entry.get("id")
        title = entry.get("title")
        if not page or not title:
            unavailable += 1
            continue
        usable.append((title, page, entry.get("duration")))

    over_limit = max(0, len(usable) - limit)
    tracks = [
        Track(title=title, query=page, duration=duration, requested_by=requested_by)
        for title, page, duration in usable[:limit]
    ]
    playlist_title = info.get("title") or "playlist"
    return playlist_title, tracks, unavailable + over_limit


async def resolve_stream_url(track: Track) -> str:
    """Resolve the actual playable URL. Called immediately before playback."""
    if track.is_local:
        return track.query

    loop = asyncio.get_running_loop()
    info = await loop.run_in_executor(None, _ytdl_extract, track.query)
    url = info.get("url")
    if not url:
        raise ValueError(f"could not resolve a stream for {track.title}")
    return url
#--------------------------------------


#----Per-guild State------
class GuildPlayer:
    def __init__(self, guild_id: int) -> None:
        self.guild_id = guild_id
        self.queue: Deque[Track] = deque()
        self.current: Optional[Track] = None
        self.loop_mode: LoopMode = LoopMode.OFF
        self.volume: float = DEFAULT_VOLUME
        self.idle_task: Optional[asyncio.Task] = None
        self.lock: asyncio.Lock = asyncio.Lock()
        # Set by /skip and /skipto, consumed by the next advance so an explicit skip
        # isn't swallowed by TRACK loop mode
        self.skip_requested: bool = False

    def cancel_idle(self) -> None:
        if self.idle_task is not None and not self.idle_task.done():
            self.idle_task.cancel()
        self.idle_task = None

    def reset(self) -> None:
        self.cancel_idle()
        self.queue.clear()
        self.current = None
        self.loop_mode = LoopMode.OFF
        self.skip_requested = False


class MusicManager:
    def __init__(self) -> None:
        self.players: Dict[int, GuildPlayer] = {}

    def player_for(self, guild_id: int) -> GuildPlayer:
        if guild_id not in self.players:
            self.players[guild_id] = GuildPlayer(guild_id)
        return self.players[guild_id]

    def cleanup(self, guild_id: int) -> None:
        """Drop all playback state for a guild. Safe to call when nothing is playing."""
        player = self.players.pop(guild_id, None)
        if player is not None:
            player.reset()
#--------------------------------------


#----Playback Engine------
async def build_source(
    stream_url: str, volume: float, ffmpeg: Optional[str]
) -> discord.AudioSource:
    """
    Build an audio source, picking the cheapest path that still honours `volume`.

    At full volume it uses FFmpegOpusAudio.from_probe, which copies an already-opus
    stream straight through - no decode to PCM and no re-encode. YouTube serves opus,
    so this skips nearly all transcoding and markedly reduces CPU, which is what causes
    stuttering on a loaded machine.

    Below full volume that isn't possible: PCMVolumeTransformer needs PCM samples to
    scale, so it falls back to decoding. Quieter playback therefore costs more CPU.
    """
    executable = ffmpeg or "ffmpeg"

    if volume >= OPUS_PASSTHROUGH_THRESHOLD:
        try:
            return await discord.FFmpegOpusAudio.from_probe(
                stream_url,
                method="fallback",
                executable=executable,
                before_options=FFMPEG_BEFORE_OPTIONS,
                options=FFMPEG_OPTIONS,
            )
        except Exception as e:
            # Probing can fail on odd sources; PCM always works
            log(f"[WARNING] Opus probe failed, using PCM: {e}")

    source = discord.FFmpegPCMAudio(
        stream_url,
        executable=executable,
        before_options=FFMPEG_BEFORE_OPTIONS,
        options=FFMPEG_OPTIONS,
    )
    return discord.PCMVolumeTransformer(source, volume=volume)


async def play_track(
    voice_client: discord.VoiceClient,
    player: GuildPlayer,
    track: Track,
    on_finished: Callable[[Optional[Exception]], None],
) -> None:
    """Resolve `track` and start playing it on `voice_client`."""
    stream_url = await resolve_stream_url(track)
    source = await build_source(stream_url, player.volume, find_ffmpeg())

    player.current = track
    player.cancel_idle()
    voice_client.play(source, after=on_finished)
    log(f"[INFO] Now playing: {track.title}")


def make_after_callback(
    loop: asyncio.AbstractEventLoop,
    advance: Callable[[], Coroutine[Any, Any, None]],
) -> Callable[[Optional[Exception]], None]:
    """
    Build the `after=` callback for VoiceClient.play.

    discord.py runs this on a separate thread, so it must hand control back to the event
    loop rather than touching async state directly - doing otherwise is the classic music
    bot bug where the queue silently stops advancing.
    """
    def _after(error: Optional[Exception]) -> None:
        if error:
            log(f"[ERROR] Playback error: {error}")
        try:
            asyncio.run_coroutine_threadsafe(advance(), loop)
        except Exception as e:  # loop already closed during shutdown
            log(f"[WARNING] Could not schedule next track: {e}")

    return _after
