import io
import os
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
# Resolve the project root from this file, so the suite runs from any checkout
PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _check import record
from _fakes import (Guild, Interaction, Member, Perms, VoiceChannel, VoiceClient,
                    load_bot, run_voice)

amy = load_bot()

OWNER, USER = 42, 99
results = []


def check(label, got, expect):
    ok = expect.lower() in got.lower()
    results.append(record(label, ok))
    print(("PASS" if ok else "FAIL"), "|", label)
    if not ok:
        print("      expected:", expect)
        print("      got     :", got[:160])


def scene(author_id, in_channel=None, amy_in=None, me_perms=None):
    amy.voice_cmd_store.clear()
    member = Member(author_id, in_channel)
    guild = Guild(OWNER, member, VoiceClient(amy_in) if amy_in else None, me_perms)
    return Interaction(member, guild)


# 1. Not in any voice channel
check("not in a VC", run_voice(amy, "join", interaction=scene(USER)), "not in a voice channel")

# 2. Bot lacks Connect
target = VoiceChannel(10, "General", perms=Perms(connect=False))
check("no Connect perm", run_voice(amy, "join", interaction=scene(USER, target)),
      "permission to connect")

# 3. Bot lacks Speak
target = VoiceChannel(10, "General", perms=Perms(connect=True, speak=False))
check("no Speak perm", run_voice(amy, "join", interaction=scene(USER, target)),
      "not allowed to speak")

# 4. Already in the target channel
target = VoiceChannel(10, "General")
check("already there", run_voice(amy, "join", interaction=scene(USER, target, target)),
      "already in")

# 5. Elsewhere + occupied + non-admin -> blocked
occupied = VoiceChannel(99, "Busy Room", members=[Member(1), Member(2)])
target = VoiceChannel(10, "General")
check("blocked: occupied, non-admin",
      run_voice(amy, "join", interaction=scene(USER, target, occupied)), "with other people")

# 6. Same, but the requester owns the server -> moves
occupied = VoiceChannel(99, "Busy Room", members=[Member(1), Member(2)])
target = VoiceChannel(10, "General")
check("admin overrides occupied",
      run_voice(amy, "join", interaction=scene(OWNER, target, occupied)), "moved to")

# 7. Elsewhere + empty -> anyone may move her
empty = VoiceChannel(99, "Empty Room", members=[Member(7, is_bot=True)])
target = VoiceChannel(10, "General")
check("move when old channel empty",
      run_voice(amy, "join", interaction=scene(USER, target, empty)), "moved to")

# 7b. /join from nowhere: actually connects to the caller's channel
_joined = []


async def _join_connect(channel):
    _joined.append(channel)


target = VoiceChannel(12, "Fresh")
_orig_connect, _orig_announce = amy.voice.connect_to, amy.announce
amy.voice.connect_to = _join_connect
amy.announce = lambda g, kind, *a, **k: None
amy.music_manager.cleanup(500)
out = run_voice(amy, "join", interaction=scene(USER, target))
amy.voice.connect_to, amy.announce = _orig_connect, _orig_announce
check("/join success says joined", out, "joined")
results.append(record("/join connects to the caller's channel", _joined == [target]))
print(("PASS" if results[-1] else "FAIL"), "| /join connects to the caller's channel")

# 8. /create without Manage Channels
check("/create no Manage Channels",
      run_voice(amy, "create", ["Music"],
                interaction=scene(OWNER, me_perms=Perms(manage_channels=False))),
      "manage channels")

# 8b. /create success: whitespace in the name collapsed, channel made, joined, remembered
class _Made:
    def __init__(self, name):
        self.id, self.name, self.mention = 777, name, "#" + name


_made = {}
_connected = []
_announced = []
sc = scene(OWNER)


async def _create_voice_channel(name, category=None, reason=None):
    _made["name"] = name
    return _Made(name)


async def _connect_to(channel):
    _connected.append(channel)


sc.guild.create_voice_channel = _create_voice_channel
_orig_connect, _orig_announce = amy.voice.connect_to, amy.announce
amy.voice.connect_to = _connect_to
amy.announce = lambda g, kind, *a, **k: _announced.append(kind)
out = run_voice(amy, "create", ["  Chill", "  room "], interaction=sc)
amy.voice.connect_to, amy.announce = _orig_connect, _orig_announce
check("/create success says created", out, "created")
results.append(record("/create joins the new channel", len(_connected) == 1))
print(("PASS" if results[-1] else "FAIL"), "| /create joins the new channel")
results.append(record("/create collapses whitespace in the name",
                      _made.get("name") == amy.voice.sanitize_channel_name("Chill room")))
print(("PASS" if results[-1] else "FAIL"), "| /create collapses whitespace in the name", _made)
results.append(record("/create remembers the channel as Amy-made",
                      amy.voice_manager.is_bot_created(777)))
print(("PASS" if results[-1] else "FAIL"), "| /create remembers the channel")

# 9. /leave when not connected
check("/leave not connected", run_voice(amy, "leave", interaction=scene(OWNER)),
      "not in a voice channel")

# 10. /leave by someone not in the channel and not an admin
ch = VoiceChannel(10, "General")
check("/leave outsider blocked", run_voice(amy, "leave", interaction=scene(USER, None, ch)),
      "need to be in my voice channel")

# 11-12. The cooldown lives in the slash layer now, so exercise it there
amy.voice_cmd_store.clear()
outs = [amy.check_voice_cooldown(USER)[0] for _ in range(amy.VOICE_CMD_MAX + 1)]
results.append(record("cooldown blocks", outs[:-1] == [True] * amy.VOICE_CMD_MAX and outs[-1] is False))
print(("PASS" if results[-1] else "FAIL"), "| cooldown blocks after %d uses" % amy.VOICE_CMD_MAX)

results.append(record("owner is admin", amy.is_admin_member(Guild(OWNER, Member(OWNER)), OWNER) is True))
print(("PASS" if results[-1] else "FAIL"), "| server owner counts as admin (cooldown exempt)")

print()
print("{}/{} passed".format(sum(results), len(results)))
