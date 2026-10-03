"""
Mixing Amy's voice into the call: a speech queue per server, and the audio source that lowers
the music ("ducking") while she talks.

discord.py plays exactly one AudioSource per voice connection. When TTS is on, every track's
source is wrapped in a Mixer, which passes the music straight through - still compressed, no
CPU cost - until there is speech queued. Then it decodes the music, fades it down to the duck
level and adds the voice on top, frame by frame, and fades back up afterwards.

The rules (ARCHITECTURE.md, "Speech"):
  - read() never raises. discord.py treats an exception as the end of the track and the
    queue advances, so one bad frame would skip the song.
  - read() returns b"" only when the music AND the speech are both finished - an empty read
    is how discord.py decides a track ended.
  - The speech queue belongs to the player, not the Mixer: /skip replaces the Mixer and the
    queued speech carries on over the next song.
"""
import collections
import logging
import threading
import time
import warnings
from typing import Callable, Deque, List, Optional

import discord

with warnings.catch_warnings():
    warnings.simplefilter("ignore", DeprecationWarning)   # see requirements-tts.txt
    import audioop

log = logging.getLogger("amy.mixer")

FRAME_BYTES = 3840               # 20 ms of 48 kHz 16-bit stereo; matches speech.FRAME_BYTES
SILENCE = b"\x00" * FRAME_BYTES
# Music volume while Amy speaks (DUCK_LEVEL). Was 0.3; in the first live test the music
# was barely audible under her voice, so it went up to 0.55.
DEFAULT_DUCK = 0.55
RAMP_FRAMES = 10                 # 200 ms fades, so the duck is never a hard cut
MAX_QUEUED_SECONDS = 30          # a backlog beyond this drops whole sentences, oldest first
SLOW_READ_MS = 40                # reads slower than two frames get logged (review item R9)
MAX_SLOW_LOGS = 5


class SpeechQueue:
    """
    Sentences waiting to be spoken, as lists of 20 ms frames. Filled from the event loop and
    drained by the audio thread, so every access holds a lock.

    Trimmed by whole sentences, oldest first: cutting frames would leave half a word.
    """

    def __init__(self, max_seconds: float = MAX_QUEUED_SECONDS) -> None:
        self._sentences: Deque[Deque[bytes]] = collections.deque()
        self._lock = threading.Lock()
        self._max_frames = int(max_seconds * 50)

    def add(self, frames: List[bytes]) -> int:
        """Queue one sentence. Returns how many older sentences were dropped to make room."""
        if not frames:
            return 0
        dropped = 0
        with self._lock:
            self._sentences.append(collections.deque(frames))
            while len(self._sentences) > 1 and self._frame_count() > self._max_frames:
                self._sentences.popleft()
                dropped += 1
        return dropped

    def pop_frame(self) -> Optional[bytes]:
        with self._lock:
            while self._sentences:
                sentence = self._sentences[0]
                if sentence:
                    return sentence.popleft()
                self._sentences.popleft()
            return None

    def clear(self) -> None:
        with self._lock:
            self._sentences.clear()

    def seconds(self) -> float:
        with self._lock:
            return self._frame_count() / 50

    def _frame_count(self) -> int:
        return sum(len(s) for s in self._sentences)

    def __bool__(self) -> bool:
        with self._lock:
            return any(self._sentences)


class Mixer(discord.AudioSource):
    """
    Music (or nothing) with Amy's voice mixed on top. `music` None means speech only.

    `hold_music` plays speech while the music is paused: the bot resumes the voice client with
    the music held - not read, so it doesn't advance - and when the speech runs out the Mixer
    calls `on_hold_done` (from the audio thread) so the bot can pause again. Until then it
    sends silence rather than an empty read, which would end the track.
    """

    def __init__(self, music: Optional[discord.AudioSource], speech: SpeechQueue,
                 duck: float = DEFAULT_DUCK,
                 on_hold_done: Optional[Callable[[], None]] = None) -> None:
        self.music = music
        self.speech = speech
        self.duck = duck
        self.on_hold_done = on_hold_done
        self.hold_music = False
        self._hold_done_sent = False
        self._music_done = music is None
        self._decoder: Optional[discord.opus.Decoder] = None
        self._pcm = bytearray()          # decoded music not yet sent, in 20 ms slices (R3)
        self._gain = 1.0
        self._opus_out = music is not None and music.is_opus()
        self._frame = 0
        self._slow_logged = 0
        self._failed = False

    # ---- discord.AudioSource ----
    def is_opus(self) -> bool:
        return self._opus_out            # discord.py asks right after every read()

    def read(self) -> bytes:
        started = time.perf_counter()
        try:
            if self._failed:
                return self._plain_music()
            return self._read()
        except Exception as e:
            # Never let an exception reach discord.py: it would end the track (R2). Fall
            # back to the plain music - for the REST OF THIS SONG, not just this frame: a
            # fault that repeats (a broken decoder) would otherwise lose the packet read in
            # each failed attempt and play every other one. At most one 20 ms packet is
            # lost; queued speech waits for the next song's Mixer.
            self._failed = True
            log.warning(f"Speech mixing failed, playing this song unmixed: {e!r}")
            try:
                return self._plain_music()
            except Exception:
                return b""      # the music source itself is broken: let the track end
        finally:
            self._frame += 1
            ms = (time.perf_counter() - started) * 1000
            if ms > SLOW_READ_MS and self._slow_logged < MAX_SLOW_LOGS:
                self._slow_logged += 1
                log.info(f"Slow audio read: {ms:.0f} ms at frame {self._frame}")

    def cleanup(self) -> None:
        if self.music is not None:
            self.music.cleanup()

    # ---- internals ----
    def _read(self) -> bytes:
        speaking = bool(self.speech)

        if self.hold_music:
            voice = self.speech.pop_frame()
            self._opus_out = False
            if voice is not None:
                self._hold_done_sent = False
                return voice
            if not self._hold_done_sent and self.on_hold_done is not None:
                self._hold_done_sent = True
                self.on_hold_done()
            return SILENCE

        self._step_gain(speaking)
        if not speaking and self._gain >= 1.0 and not self._pcm:
            return self._plain_music()

        # Speaking, fading, or draining decoded music: work in PCM
        self._opus_out = False
        music_pcm = self._music_frame()
        voice = self.speech.pop_frame() if speaking else None
        if music_pcm and self._gain < 1.0:
            music_pcm = audioop.mul(music_pcm, 2, self._gain)
        if music_pcm and voice:
            return audioop.add(music_pcm, voice, 2)    # saturates at full scale, never wraps
        return music_pcm or voice or b""

    def _plain_music(self) -> bytes:
        if self._music_done or self.music is None:
            self._opus_out = False
            return b""
        data = self.music.read()
        if not data:
            self._music_done = True
        self._opus_out = self.music.is_opus()
        return data

    def _music_frame(self) -> bytes:
        """Exactly one 20 ms frame of decoded music, or b"" once the music has ended."""
        while len(self._pcm) < FRAME_BYTES and not self._music_done and self.music is not None:
            packet = self.music.read()
            if not packet:
                self._music_done = True
                break
            if self.music.is_opus():
                if self._decoder is None:
                    self._decoder = discord.opus.Decoder()
                try:
                    self._pcm += self._decoder.decode(packet, fec=False)
                except discord.opus.OpusError:
                    # Not audio: the OpusHead/OpusTags headers at the start of every
                    # passed-through stream (R1). Nothing to mix; skip it.
                    continue
            else:
                self._pcm += packet
        if not self._pcm:
            return b""
        if len(self._pcm) < FRAME_BYTES:                  # the music's last, partial frame
            self._pcm += b"\x00" * (FRAME_BYTES - len(self._pcm))
        frame = bytes(self._pcm[:FRAME_BYTES])
        del self._pcm[:FRAME_BYTES]
        return frame

    def _step_gain(self, speaking: bool) -> None:
        target = self.duck if speaking else 1.0
        step = (1.0 - self.duck) / RAMP_FRAMES if self.duck < 1.0 else 1.0
        if self._gain > target:
            self._gain = max(target, self._gain - step)
        elif self._gain < target:
            self._gain = min(target, self._gain + step)


def mixer_of(voice_client: Optional[discord.VoiceClient]) -> Optional[Mixer]:
    """The Mixer the voice client is playing, if it's playing one."""
    source = getattr(voice_client, "source", None) if voice_client is not None else None
    return source if isinstance(source, Mixer) else None


def music_source(voice_client: Optional[discord.VoiceClient]) -> Optional[discord.AudioSource]:
    """The music source itself, looking through a Mixer - e.g. to reach its volume control."""
    mixer = mixer_of(voice_client)
    if mixer is not None:
        return mixer.music
    return getattr(voice_client, "source", None) if voice_client is not None else None


def music_paused(voice_client: Optional[discord.VoiceClient]) -> bool:
    """
    Whether the MUSIC is paused. While Amy speaks over paused music the voice client is
    playing (her voice), but the music is held - from a listener's point of view still paused.
    """
    if voice_client is None:
        return False
    mixer = mixer_of(voice_client)
    return voice_client.is_paused() or (mixer is not None and mixer.hold_music)
