import io
import os
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
# Resolve the project root from this file, so the suite runs from any checkout
PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _fakes import (Guild, Interaction, Member, VoiceChannel, VoiceClient,
                    load_bot, run_music, seed_queue)

amy = load_bot()

OWNER, USER = 42, 99
GUILD = 500
fails = []


def check(label, ok, got=""):
    print(("  PASS " if ok else "  FAIL ") + label)
    if not ok:
        fails.append(label)
        print("        got:", str(got)[:130])


def command(name):
    return amy.tree.get_command(name)


print("=== the /dice freeze bug is now structurally impossible ===")
dice = command("dice")
params = {p.name: p for p in dice.parameters}
check("/dice registered", dice is not None)
check("amount is bounded by Discord",
      params["amount"].max_value == amy.MAX_DICE_AMOUNT, params["amount"].max_value)
check("amount has a floor of 1", params["amount"].min_value == 1)
check("sides is bounded by Discord",
      params["sides"].max_value == amy.MAX_DICE_SIDES, params["sides"].max_value)
check("sides has a floor of 1", params["sides"].min_value == 1)
print("        (Range bounds are enforced client-side, so /dice 6 1000000000 never")
print("         reaches the event loop at all - it used to freeze the bot for ~300s)")

print()
print("=== deleted commands really are gone ===")
for gone in ("dice1", "dice2", "clear", "np"):
    check("/%s not registered" % gone, command(gone) is None)

print()
print("=== the surviving commands are registered ===")
expected = [
    "help", "dice", "rng", "join", "leave", "create", "play", "search",
    "pause", "resume", "skip", "stop", "queue", "nowplaying", "remove",
    "skipto", "shuffle", "loop", "clearqueue", "volume", "toggle", "status",
    "model", "forget",
]
for name in expected:
    check("/%s registered" % name, command(name) is not None)

print()
print("=== /stop stops but stays connected ===")
ch = VoiceChannel(10, "Chung")
member = Member(USER, ch)
vcl = VoiceClient(ch)
guild = Guild(OWNER, member, vcl, guild_id=GUILD)
p = seed_queue(amy, GUILD, ["t1", "t2", "t3"])
p.loop_mode = amy.LoopMode.QUEUE
out = run_music(amy, "stop", interaction=Interaction(member, guild))
check("queue cleared", len(p.queue) == 0, len(p.queue))
check("loop reset", p.loop_mode is amy.LoopMode.OFF, p.loop_mode)
check("playback stopped", vcl.stopped)
check("STILL CONNECTED (the behaviour change)", not vcl.disconnected)
check("reply mentions staying", "stay" in out.lower(), out)
check("reply points at /leave", "/leave" in out, out)

print()
if fails:
    print("%d CHECK(S) FAILED" % len(fails))
    sys.exit(1)
print("ALL COMMAND AUDIT TESTS PASSED")
