"""
mixer.py: Amy's voice mixed over the music, against real Opus packets.

Covers the rules the mixer exists for - and the bugs the Phase 0 review found before it shipped:
  R1  the OpusHead/OpusTags packets at the start of every passed-through song can't be
      decoded; a song starting while Amy is mid-sentence must not end on them
  R2  read() never raises (discord.py would end the song)
  R3  music in 40/60 ms packets still mixes, in exact 20 ms frames
  R7  discord.py's real AudioPlayer loop, driven with the Mixer, ends the song exactly once
Needs libopus, which discord.py loads on Windows. Exits 77 (SKIP) if it can't.
"""
import collections
import io
import logging
import math
import os
import struct
import sys
import threading
import time

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJ)

import discord
import discord.opus

import mixer
from _check import check, finish

try:
    discord.opus._load_default()
except Exception:
    pass
if not discord.opus.is_loaded():
    print("SKIP: libopus isn't available here")
    sys.exit(77)

F = mixer.FRAME_BYTES
ENC = discord.opus.Encoder()


def tone(ms, freq=440, amp=0.5):
    """`ms` of a stereo sine wave as 16-bit PCM."""
    n = 48 * ms
    return b"".join(struct.pack("<hh", s, s) for s in
                    (int(amp * 32767 * math.sin(2 * math.pi * freq * i / 48000)) for i in range(n)))


def opus_packets(ms, frame_ms=20, headers=True):
    pcm = tone(ms)
    size = 48 * frame_ms * 4
    packets = [ENC.encode(pcm[i:i + size], 48 * frame_ms) for i in range(0, len(pcm) - size + 1, size)]
    if headers:     # what discord.py's Ogg reader hands over first on a passthrough stream
        packets = [b"OpusHead\x01\x02\x38\x01\x80\xbb\x00\x00\x00\x00\x00",
                   b"OpusTags\x08\x00\x00\x00Lavf60.3\x00\x00\x00\x00"] + packets
    return packets


class FakeMusic(discord.AudioSource):
    def __init__(self, packets, opus=True, sleep_on=None):
        self.packets = collections.deque(packets)
        self.opus = opus
        self.reads = 0
        self.sleep_on = sleep_on

    def read(self):
        self.reads += 1
        if self.sleep_on == self.reads:
            time.sleep(0.06)
        return self.packets.popleft() if self.packets else b""

    def is_opus(self):
        return self.opus


def voice(ms, amp=0.3):
    pcm = tone(ms, freq=220, amp=amp)
    return [pcm[i:i + F] for i in range(0, len(pcm), F)]


def drain(m, limit=10000, on_frame=None):
    out = []
    for i in range(limit):
        if on_frame:
            on_frame(i)
        data = m.read()
        out.append((data, m.is_opus()))
        if not data:
            break
    return out


def peak(frame):
    return mixer.audioop.max(frame, 2)


print("=== SpeechQueue ===")
q = mixer.SpeechQueue(max_seconds=1)               # 50 frames
check("empty is falsy", not q and q.pop_frame() is None)
q.add([b"a1", b"a2"])
q.add([b"b1"])
check("frames come out in order, across sentences",
      [q.pop_frame() for _ in range(3)] == [b"a1", b"a2", b"b1"])
check("drained is falsy again", not q)
q.add([b"x"] * 30)
q.add([b"y"] * 30)                                  # 60 > 50: the oldest sentence goes
check("a backlog drops whole sentences, oldest first",
      q.pop_frame() == b"y" and abs(q.seconds() - 29 / 50) < 1e-9, q.seconds())
q.add([b"z"] * 80)                                  # one sentence longer than the cap stays
check("a single long sentence is never cut", q.seconds() > 1)
q.clear()
check("clear empties it", not q)

print()
print("=== quiet: music passes through untouched ===")
packets = opus_packets(400)
m = mixer.Mixer(FakeMusic(packets), mixer.SpeechQueue())
out = drain(m)
check("every packet passes through as-is, still opus",
      [d for d, _ in out[:-1]] == packets and all(o for _, o in out[:-1]))
check("and the song ends with exactly one empty read", out[-1][0] == b"" and
      sum(1 for d, _ in out if not d) == 1)

print()
print("=== R1: a song that starts while Amy is mid-sentence ===")
sq = mixer.SpeechQueue()
sq.add(voice(300))                                  # speech carried over a /skip
music = FakeMusic(opus_packets(1000))
m = mixer.Mixer(music, sq)
out = drain(m)
audio = [d for d, _ in out if d]
check("the header packets don't end the song (no early empty read)",
      sum(1 for d, _ in out if not d) == 1 and out[-1][0] == b"")
check("all the music still plays: 50 frames for 1 s", len(audio) == 50, len(audio))
check("mixed frames are exactly 20 ms", all(len(d) == F for d, o in out if d and not o))
check("no fallback was needed", not m._failed)

print()
print("=== R2: a failure inside the mixer never reaches discord.py ===")


class Boom:
    def decode(self, *a, **kw):
        raise RuntimeError("decoder exploded")


records = []


class Catch(logging.Handler):
    def emit(self, record):
        records.append(record.getMessage())


logging.getLogger("amy.mixer").addHandler(Catch())
logging.getLogger("amy.mixer").setLevel(logging.DEBUG)
sq = mixer.SpeechQueue()
sq.add(voice(200))
music_packets = opus_packets(400, headers=False)
m = mixer.Mixer(FakeMusic(music_packets), sq)
m._decoder = Boom()
try:
    out = drain(m)
    raised = None
except Exception as e:
    raised = e
check("read() did not raise", raised is None, raised)
got = [d for d, _ in out if d]
check("the rest of the song plays in order, untouched - at most the one packet in flight lost",
      got == music_packets[1:], (len(got), len(music_packets)))
check("...passed through as opus, no further mixing attempts", all(o for d, o in out if d))
check("the queued speech is kept for the next song's mixer", bool(sq))
check("and it was logged once", sum("mixing failed" in r for r in records) == 1, records)

print()
print("=== R3: music in 60 ms packets still mixes in 20 ms frames ===")
sq = mixer.SpeechQueue()
sq.add(voice(400))
m = mixer.Mixer(FakeMusic(opus_packets(600, frame_ms=60, headers=False)), sq)
out = drain(m)
check("every mixed frame is exactly 20 ms",
      all(len(d) == F for d, o in out if d and not o), {len(d) for d, _ in out})
check("600 ms of music -> 30 frames out, nothing lost or stretched",
      len([d for d, _ in out if d]) == 30, len(out) - 1)

print()
print("=== the reduced-volume (PCM) path ===")
pcm_music = [tone(20) for _ in range(25)]
sq = mixer.SpeechQueue()
m = mixer.Mixer(FakeMusic(pcm_music, opus=False), sq)
first = m.read()
check("quiet PCM passes through unchanged", first == pcm_music[0] and not m.is_opus())
sq.add(voice(100))
out = drain(m)
check("and mixes without error", not m._failed and all(len(d) == F for d, _ in out if d))

print()
print("=== ducking: fades down under the voice, back up after ===")
sq = mixer.SpeechQueue()
silent_voice = [b"\x00" * F] * 30                    # silence, so only the music is measured
music = FakeMusic([tone(20, amp=0.8) for _ in range(100)], opus=False)
m = mixer.Mixer(music, sq, duck=0.3)
before = peak(m.read())
sq.add(silent_voice)
levels = [peak(m.read()) for _ in range(30)]
check("the first ducked frame isn't a hard cut", levels[0] > before * 0.8, (before, levels[:3]))
check("after the 200 ms fade the music sits at the duck level",
      abs(levels[15] - before * 0.3) < before * 0.03, (levels[15], before))
after = [peak(m.read()) for _ in range(15)]
check("it fades back up rather than jumping", after[0] < before * 0.5, after[:3])
check("and returns to full level", abs(after[-1] - before) <= 2, (after[-1], before))

print()
print("=== passthrough resumes after speech ===")
sq = mixer.SpeechQueue()
music = FakeMusic(opus_packets(1000, headers=False))
m = mixer.Mixer(music, sq)
m.read()
sq.add(voice(100))
outs = [(m.read(), m.is_opus()) for _ in range(30)]
check("speaking switches to PCM", not outs[0][1])
check("and back to untouched opus once speech and fade are done", outs[-1][1], outs[-1][1])

print()
print("=== music ends while Amy is still talking ===")
sq = mixer.SpeechQueue()
music = FakeMusic(opus_packets(200, headers=False))     # 10 frames of music
m = mixer.Mixer(music, sq)
sq.add(voice(600))                                      # 30 frames of speech
out = drain(m)
nonempty = [d for d, _ in out if d]
check("her sentence finishes after the music ends", len(nonempty) == 30, len(nonempty))
check("and only then does the source end", out[-1][0] == b"" and
      sum(1 for d, _ in out if not d) == 1)

print()
print("=== speech with no music ===")
sq = mixer.SpeechQueue()
sq.add(voice(100))
m = mixer.Mixer(None, sq)
out = drain(m)
check("plays the speech, then ends", len(out) == 6 and out[-1][0] == b"" and not out[0][1],
      len(out))

print()
print("=== hold_music: speaking over paused music ===")
calls = []
sq = mixer.SpeechQueue()
music = FakeMusic(opus_packets(400, headers=False))
m = mixer.Mixer(music, sq, on_hold_done=lambda: calls.append("done"))
m.hold_music = True
sq.add(voice(60))                                       # 3 frames
reads_before = music.reads
held = [m.read() for _ in range(6)]
check("the music isn't read while held", music.reads == reads_before)
check("the voice plays", held[:3] == voice(60))
check("then silence - never an empty read, which would end the song",
      all(h == mixer.SILENCE for h in held[3:]))
check("and the bot is told once that it can pause again", calls == ["done"], calls)
m.hold_music = False
next_packet = music.packets[0]
check("released, the music carries on from exactly where it was held",
      m.read() == next_packet and music.reads == reads_before + 1, music.reads - reads_before)

print()
print("=== helpers ===")


class VC:
    def __init__(self, source, paused=False):
        self.source = source
        self._paused = paused

    def is_paused(self):
        return self._paused


plain = FakeMusic([])
m = mixer.Mixer(plain, mixer.SpeechQueue())
check("mixer_of finds a Mixer", mixer.mixer_of(VC(m)) is m)
check("mixer_of ignores other sources", mixer.mixer_of(VC(plain)) is None)
check("music_source looks through the Mixer", mixer.music_source(VC(m)) is plain)
check("music_source without a Mixer is the source itself", mixer.music_source(VC(plain)) is plain)
check("music_paused: plain pause", mixer.music_paused(VC(m, paused=True)))
m.hold_music = True
check("music_paused: held while she speaks", mixer.music_paused(VC(m, paused=False)))
check("music_paused: no voice client", not mixer.music_paused(None))

print()
print("=== R9: slow reads are logged with their position ===")
records.clear()
m = mixer.Mixer(FakeMusic(opus_packets(200, headers=False), sleep_on=3), mixer.SpeechQueue())
drain(m)
check("a 60 ms read is logged, naming the frame", any("Slow audio read" in r and "frame 3" in r
                                                      for r in records), records)

print()
print("=== R7: discord.py's real player loop, driven with the Mixer ===")


class FakeWS:
    async def speak(self, state):
        return None


class FakeClient:
    def __init__(self):
        self.sent = []
        self.ws = FakeWS()
        self.client = type("C", (), {"loop": None})()

    def send_audio_packet(self, data, *, encode=True):
        self.sent.append((len(data), encode))

    def is_connected(self):
        return True


client = FakeClient()
sq = mixer.SpeechQueue()
sq.add(voice(300))                                      # mid-sentence as the song starts
src = mixer.Mixer(FakeMusic(opus_packets(1000)), sq)
ended = threading.Event()
afters = []


def after(error):
    afters.append(error)
    ended.set()

player = discord.player.AudioPlayer(src, client, after=after)
player.DELAY = 0.001                                    # don't wait 20 ms per frame
player.start()
ended.wait(10)
check("the song ends exactly once, with no error", afters == [None], afters)
audio_sent = [sz for sz, _ in client.sent if sz != len(discord.player.OPUS_SILENCE)]
check("every frame of music was sent, nothing dropped (end-of-track silence aside)",
      len(audio_sent) == 50, len(audio_sent))
check("mixed frames are sent for encoding, passthrough frames as-is",
      all(enc == (size == F) for size, enc in client.sent), client.sent[:5])

finish("ALL MIXER TESTS PASSED")
