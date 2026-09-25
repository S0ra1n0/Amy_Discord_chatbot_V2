import io, os, sys, tempfile
from collections import deque
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
# Resolve the project root from this file, so the suite runs from any checkout
PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJ); os.chdir(PROJ)
import voice
from voice import (JoinAction, RestoreAction, decide_join_action,
                   decide_restore_action, sanitize_channel_name, VoiceManager)

assert sanitize_channel_name("") == "Amy's Room"
assert sanitize_channel_name("   ") == "Amy's Room"
assert sanitize_channel_name("Music Room") == "Music Room"
assert sanitize_channel_name("  Music   Room  ") == "Music Room"
assert len(sanitize_channel_name("x" * 250)) == 100
print("sanitize_channel_name: OK (5 cases)")

assert decide_join_action(None, False, 10, False) is JoinAction.CONNECT
assert decide_join_action(None, False, 10, True) is JoinAction.CONNECT
assert decide_join_action(None, True, 10, False) is JoinAction.CONNECT
assert decide_join_action(10, False, 10, False) is JoinAction.ALREADY_THERE
assert decide_join_action(10, True, 10, True) is JoinAction.ALREADY_THERE
assert decide_join_action(99, False, 10, False) is JoinAction.MOVE
assert decide_join_action(99, False, 10, True) is JoinAction.MOVE
assert decide_join_action(99, True, 10, False) is JoinAction.BLOCKED_OCCUPIED
assert decide_join_action(99, True, 10, True) is JoinAction.MOVE
print("decide_join_action: OK (9 cases, full truth table)")

# decide_restore_action - what startup does with a saved queue. Every branch of this used to
# be reachable only by restarting the bot with a real voice channel, which meant the most
# important behaviour in queue persistence had no regression test at all.
#                     tracks, channel_found, has_humans
CASES = [
    (0, False, False, RestoreAction.NOTHING),
    (0, True,  False, RestoreAction.NOTHING),
    (0, True,  True,  RestoreAction.NOTHING),   # nothing rebuildable beats a full channel
    (3, False, False, RestoreAction.QUEUE_ONLY),
    (3, False, True,  RestoreAction.QUEUE_ONLY),  # no channel, so "humans" is meaningless
    (3, True,  False, RestoreAction.QUEUE_ONLY),  # empty room - restore, don't play to nobody
    (3, True,  True,  RestoreAction.RESUME),
    (1, True,  True,  RestoreAction.RESUME),
]
for tracks, found, humans, want in CASES:
    got = decide_restore_action(tracks, found, humans)
    assert got is want, "tracks=%d found=%s humans=%s -> %s, wanted %s" % (
        tracks, found, humans, got, want)
assert decide_restore_action(-1, True, True) is RestoreAction.NOTHING, "negative count"
print("decide_restore_action: OK (%d cases, full truth table)" % (len(CASES) + 1))

# The only case that plays audio is the one where somebody is there to hear it.
resuming = [c for c in CASES if c[3] is RestoreAction.RESUME]
assert all(found and humans for _, found, humans, _ in resuming),     "Amy must never resume into an empty or missing channel"
assert all(tracks > 0 for tracks, _, _, _ in resuming)
print("resume only with an audience: OK")

from database import ConversationDB
tmp = tempfile.mktemp(suffix=".db")
db = ConversationDB(tmp)
assert db.get_bot_voice_channels() == []
db.add_bot_voice_channel(1111, 2222)
db.add_bot_voice_channel(3333, 2222)
assert sorted(db.get_bot_voice_channels()) == [(1111, 2222), (3333, 2222)]
db.add_bot_voice_channel(1111, 2222)
assert len(db.get_bot_voice_channels()) == 2
db.remove_bot_voice_channel(1111)
assert db.get_bot_voice_channels() == [(3333, 2222)]
db.remove_bot_voice_channel(9999)
assert db.get_bot_voice_channels() == [(3333, 2222)]
print("database bot_voice_channels: OK (add/list/dedupe/remove/missing)")

mgr = VoiceManager(db)
assert not mgr.is_bot_created(4444)
mgr.mark_created(4444, 2222)
assert mgr.is_bot_created(4444) and (4444, 2222) in db.get_bot_voice_channels()
mgr.unmark_created(4444)
assert not mgr.is_bot_created(4444) and (4444, 2222) not in db.get_bot_voice_channels()
mgr.unmark_created(4444)
print("VoiceManager mark/unmark + DB sync: OK")

assert mgr.lock_for(1) is mgr.lock_for(1)
assert mgr.lock_for(1) is not mgr.lock_for(2)
print("VoiceManager per-guild locks: OK")

m2 = VoiceManager(None)
m2.mark_created(7777, 8888)
assert m2.is_bot_created(7777)
m2.unmark_created(7777)
print("VoiceManager with db=None: OK")

db.conn.close(); os.remove(tmp)
print()
print("ALL VOICE TESTS PASSED")
