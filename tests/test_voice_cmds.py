import io
import os
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
# Resolve the project root from this file, so the suite runs from any checkout
PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _fakes import (Guild, Interaction, Member, Perms, VoiceChannel, VoiceClient,
                    load_bot, run_voice)

amy = load_bot()

OWNER, USER = 42, 99
results = []


def check(label, got, expect):
    ok = expect.lower() in got.lower()
    results.append(ok)
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

# 8. /create without Manage Channels
check("/create no Manage Channels",
      run_voice(amy, "create", ["Music"],
                interaction=scene(OWNER, me_perms=Perms(manage_channels=False))),
      "manage channels")

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
results.append(outs[:-1] == [True] * amy.VOICE_CMD_MAX and outs[-1] is False)
print(("PASS" if results[-1] else "FAIL"), "| cooldown blocks after %d uses" % amy.VOICE_CMD_MAX)

results.append(amy.is_admin_member(Guild(OWNER, Member(OWNER)), OWNER) is True)
print(("PASS" if results[-1] else "FAIL"), "| server owner counts as admin (cooldown exempt)")

print()
print("{}/{} passed".format(sum(results), len(results)))
sys.exit(0 if all(results) else 1)
