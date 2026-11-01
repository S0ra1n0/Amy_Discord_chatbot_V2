"""
access.py and the DJ role: who may do what with the music (DJ_PLAN.md, Phase 2).

Pure tables first (levels, the action table, the skip decision, the stored-setting parsers),
then the bot: every control walked as a DJ, a listener, an outsider and an admin, with a DJ role
set and without one; the player buttons and the /search picker; and /dj itself.
"""
import asyncio
import inspect
import io
import itertools
import os
import sys
import tempfile

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _fakes import (Interaction, Member, VoiceChannel, VoiceClient, load_bot, make_track,
                    run_music, seed_queue, text_of)
from _check import check, finish

amy = load_bot()
import discord
import access
import config
import markup
from access import Level, SkipDecision
from database import ConversationDB
from discord import app_commands

# ---------------------------------------------------------------------------------------------
print("=== levels: every combination of the five facts ===")


def oracle(is_admin, in_channel, role_set, has_role, alone):
    """The DJ_PLAN rule written out as plain statements, independent of access.py's code."""
    if is_admin:
        return Level.ADMIN
    if not in_channel:
        return Level.OUTSIDER
    if has_role or alone:
        return Level.DJ
    if not role_set:
        return Level.DJ
    return Level.LISTENER


bad = [c for c in itertools.product([False, True], repeat=5)
       if access.access_level(*c) != oracle(*c)]
check("access_level matches the plan for all 32 combinations", not bad, bad[:3])
check("an admin outside the call is still an Admin",
      access.access_level(True, False, True, False, False) is Level.ADMIN)
check("outside the channel a DJ-role holder is only an Outsider",
      access.access_level(False, False, True, True, True) is Level.OUTSIDER)
check("in the call with the role -> DJ", access.access_level(False, True, True, True, False) is Level.DJ)
check("alone in the call -> DJ even without the role",
      access.access_level(False, True, True, False, True) is Level.DJ)
check("in the call, role set, no role, not alone -> Listener",
      access.access_level(False, True, True, False, False) is Level.LISTENER)
check("in the call with no DJ role set -> DJ (the default changes nothing)",
      access.access_level(False, True, False, False, False) is Level.DJ)
check("levels are ordered Admin > DJ > Listener > Outsider",
      Level.ADMIN > Level.DJ > Level.LISTENER > Level.OUTSIDER)

# ---------------------------------------------------------------------------------------------
print()
print("=== the action table ===")
DJ_ONLY = {"stop", "skip_force", "skipto", "previous", "seek", "replay", "filter_set", "loop",
           "shuffle", "pause", "resume", "autoplay_set", "remove_other"}
LISTENER_OK = {"play", "search_pick", "skip_own", "remove_own", "shush"}
ANYONE = {"queue_view", "nowplaying", "lyrics", "filter_status", "autoplay_status"}
check("the table names exactly the planned controls",
      set(access.ACTIONS) == DJ_ONLY | LISTENER_OK | ANYONE, sorted(set(access.ACTIONS)))
check("DJ-only actions need a DJ", all(access.ACTIONS[a] is Level.DJ for a in DJ_ONLY))
check("listener actions need a listener", all(access.ACTIONS[a] is Level.LISTENER for a in LISTENER_OK))
check("looking needs nothing", all(access.ACTIONS[a] is Level.OUTSIDER for a in ANYONE))
bad = [(lv, a) for lv in Level for a in access.ACTIONS
       if access.allowed(lv, a, True) != (lv >= access.ACTIONS[a])]
check("allowed(level, action) is level >= the required one (role set)", not bad, bad[:3])
check("a listener may queue but not pause",
      access.allowed(Level.LISTENER, "play", True) and not access.allowed(Level.LISTENER, "pause", True))
check("an outsider may look but not queue",
      access.allowed(Level.OUTSIDER, "queue_view", True) and not access.allowed(Level.OUTSIDER, "play", True))
check("an admin may do everything", all(access.allowed(Level.ADMIN, a, r)
                                         for a in access.ACTIONS for r in (False, True)))
check("with no DJ role, removing someone else's song stays admin-only (today's rule)",
      not access.allowed(Level.DJ, "remove_other", False) and access.allowed(Level.ADMIN, "remove_other", False))
check("with a DJ role, a DJ may remove someone else's song",
      access.allowed(Level.DJ, "remove_other", True) and not access.allowed(Level.LISTENER, "remove_other", True))
check("every other DJ-only action is open to a DJ without a role set",
      all(access.allowed(Level.DJ, a, False) for a in DJ_ONLY - {"remove_other"}))
try:
    access.allowed(Level.ADMIN, "no_such_action")
    check("an unknown action name is an error, never silently allowed", False)
except KeyError:
    check("an unknown action name is an error, never silently allowed", True)

# ---------------------------------------------------------------------------------------------
print()
print("=== the skip decision ===")
D = SkipDecision
S = access.skip_decision
check("an admin always skips", all(S(Level.ADMIN, m, a, r) is D.ALLOWED
                                    for m, a, r in itertools.product([False, True], repeat=3)))
check("an outsider never does", all(S(Level.OUTSIDER, m, a, r) is D.NOT_IN_CALL
                                     for m, a, r in itertools.product([False, True], repeat=3)))
check("no DJ role: anyone in the call skips (today)",
      all(S(lv, m, a, False) is D.ALLOWED for lv in (Level.LISTENER, Level.DJ)
          for m, a in itertools.product([False, True], repeat=2)))
check("role set: a DJ skips", S(Level.DJ, False, False, True) is D.ALLOWED)
check("role set: the requester skips their own song", S(Level.LISTENER, True, False, True) is D.ALLOWED)
check("role set: someone alone with Amy skips", S(Level.LISTENER, False, True, True) is D.ALLOWED)
check("role set: a listener cannot skip someone else's song",
      S(Level.LISTENER, False, False, True) is D.DJ_ONLY)
check("the skip decision agrees with the table (skip_force / skip_own)",
      all((S(lv, mine, False, True) is D.ALLOWED)
          == access.allowed(lv, "skip_own" if mine else "skip_force", True)
          for lv in (Level.OUTSIDER, Level.LISTENER, Level.DJ, Level.ADMIN)
          for mine in (False, True)))

# ---------------------------------------------------------------------------------------------
print()
print("=== stored settings are parsed defensively ===")
check("vote share: the three words", [config.parse_vote_share(w) for w in config.VOTE_SHARES]
      == list(config.VOTE_SHARES))
check("vote share: unset / junk -> half",
      config.parse_vote_share(None) == "half" and config.parse_vote_share("banana") == "half"
      and config.DEFAULT_VOTE_SHARE == "half")
check("vote share: case and spaces tolerated", config.parse_vote_share(" Two-Thirds ") == "two-thirds")
check("role id: digits only",
      config.parse_role_id("123456789012345678") == 123456789012345678
      and config.parse_role_id(None) is None and config.parse_role_id("") is None
      and config.parse_role_id("abc") is None and config.parse_role_id("-5") is None
      and config.parse_role_id("0") is None and config.parse_role_id("12 3") is None
      and config.parse_role_id("١٢") is None)

# ---------------------------------------------------------------------------------------------
# A small server: a channel with a DJ, three listeners, an admin outside the call and an
# outsider in another channel.
GID = 9100
DJ, LIS, LIS2, REQ, ADM, OUT = 1, 2, 3, 4, 42, 7
ROLE_ID = 5000
ROLE_NAME = "DJ **Crew**"


class Role:
    def __init__(self, id_, name, bot=False, integration=False, booster=False):
        self.id = id_
        self.name = name
        self._bot, self._integration, self._booster = bot, integration, booster

    def is_bot_managed(self):
        return self._bot

    def is_integration(self):
        return self._integration

    def is_premium_subscriber(self):
        return self._booster


class DMember(Member):
    def __init__(self, id_, channel, name, roles=()):
        super().__init__(id_, channel, name=name)
        self.roles = list(roles)
        self.display_name = name


class DGuild:
    def __init__(self, gid, members, roles, voice_client):
        self.id = gid
        self.owner_id = ADM
        self._members = {m.id: m for m in members}
        self._roles = {r.id: r for r in roles}
        self.voice_client = voice_client
        self.me = type("Me", (), {})()

    def get_member(self, uid):
        return self._members.get(uid)

    def get_role(self, rid):
        return self._roles.get(rid)


class World:
    pass


def world(role="set", gid=GID, listeners_only=False):
    """role: 'set' (DJ role saved and present), 'none', or 'deleted' (saved id, no such role)."""
    amy.db.clear_guild_setting(gid, config.DJ_ROLE_KEY)
    amy.db.clear_guild_setting(gid, config.VOTE_SHARE_KEY)
    dj_role = Role(ROLE_ID, ROLE_NAME)
    if role == "set":
        amy.db.set_guild_setting(gid, config.DJ_ROLE_KEY, str(ROLE_ID))
    elif role == "deleted":
        amy.db.set_guild_setting(gid, config.DJ_ROLE_KEY, "123456")
    ch = VoiceChannel(10, "Chung")
    elsewhere = VoiceChannel(11, "elsewhere")
    w = World()
    w.m = {
        DJ: DMember(DJ, ch, "dj", roles=[dj_role]),
        LIS: DMember(LIS, ch, "lis"),
        LIS2: DMember(LIS2, ch, "lis2"),
        REQ: DMember(REQ, ch, "req"),
        ADM: DMember(ADM, elsewhere, "boss"),
        OUT: DMember(OUT, elsewhere, "out"),
    }
    ch.members = [w.m[DJ], w.m[LIS], w.m[LIS2], w.m[REQ]]
    w.ch = ch
    w.vc = VoiceClient(ch)
    w.guild = DGuild(gid, w.m.values(), [dj_role], w.vc)
    w.player = seed_queue(amy, gid, ["mine", "theirs", "third"], current="playing")
    w.player.current.requester_id = REQ
    w.player.current.requested_by = "req"
    for t in w.player.queue:
        t.requested_by, t.requester_id = ("lis", LIS) if t.title == "mine" else ("someone", 55)
    amy.voice_cmd_store.clear()
    return w


def as_(w, who):
    return Interaction(w.m[who], w.guild)


def run(w, who, cmd, args=None):
    it = as_(w, who)
    return run_music(amy, cmd, args, interaction=it), it


# the slow paths are stubbed: only the permission decision is under test here
async def _stub_restart(*a, **k):
    return amy._info("stub restart")


async def _stub_advance(guild):
    return None


async def _stub_resolve(*a, **k):
    raise ValueError("stub: nothing found")


async def _stub_lyrics(*a, **k):
    return None

_real = (amy._restart_playing, amy.announce, amy.music.find_ffmpeg, amy.music.resolve_metadata,
         amy.lyrics.find_lyrics, amy.advance_playback)
amy._restart_playing = _stub_restart
amy.announce = lambda *a, **k: None
amy.music.find_ffmpeg = lambda: "ffmpeg"
amy.music.resolve_metadata = _stub_resolve
amy.lyrics.find_lyrics = _stub_lyrics
amy.advance_playback = _stub_advance

# (action, command, args) - the person acting is a Listener unless a row says otherwise
CONTROLS = [
    ("stop", "stop", None),
    ("skipto", "skipto", [2]),
    ("previous", "previous", None),
    ("seek", "seek", ["0:30"]),
    ("replay", "replay", None),
    ("filter_set", "filter", ["nightcore"]),
    ("loop", "loop", [amy.LoopMode.QUEUE]),
    ("shuffle", "shuffle", None),
    ("pause", "pause", None),
    ("resume", "resume", None),
    ("autoplay_set", "autoplay", [False]),
    ("remove_other", "remove", [2]),
    ("skip_force", "skip", None),
    ("play", "play", ["a song"]),
    ("remove_own", "remove", [1]),
    ("shush", "shush", None),
    ("skip_own", "skip", None),            # run by the requester, REQ
    ("queue_view", "queue", None),
    ("nowplaying", "nowplaying", None),
    ("lyrics", "lyrics", None),
    ("filter_status", "filter", None),
    ("autoplay_status", "autoplay", None),
]
check("the walk below names every action in the table (the /search picker has its own section)",
      {a for a, _, _ in CONTROLS} | {"search_pick"} == set(access.ACTIONS),
      set(access.ACTIONS) ^ {a for a, _, _ in CONTROLS})


def actor(action):
    return REQ if action == "skip_own" else LIS


def refused_dj(text):
    return "Only a DJ can" in text


def refused_out(text):
    return "need to be in my voice channel" in text


def passed(text):
    return not refused_dj(text) and not refused_out(text)


# ---------------------------------------------------------------------------------------------
print()
print("=== with a DJ role set: each control, as DJ / listener / outsider / admin ===")
for action, cmd, args in CONTROLS:
    who = actor(action)
    level = access.ACTIONS[action]
    # the listener (or the requester, for skip_own)
    w = world("set")
    before = (len(w.player.queue), w.vc.stopped, w.player.loop_mode, w.player.filter_name)
    text, it = run(w, who, cmd, args)
    if action in ("skip_own",):
        ok = passed(text)       # the requester of the song may skip it
    elif level is Level.DJ:
        ok = refused_dj(text)
        ok = ok and (len(w.player.queue), w.vc.stopped, w.player.loop_mode, w.player.filter_name) == before
    else:
        ok = passed(text)
    check("%-16s listener: %s" % (action, "allowed" if (level is not Level.DJ and action != "skip_force")
                                  or action == "skip_own" else "refused, nothing changed"), ok, text)
    # a DJ
    w = world("set")
    text, _ = run(w, DJ, cmd, args)
    check("%-16s DJ: allowed" % action, passed(text), text)
    # an outsider (in another channel)
    w = world("set")
    before = (len(w.player.queue), w.vc.stopped)
    text, _ = run(w, OUT, cmd, args)
    if level is Level.OUTSIDER:
        ok = passed(text)
    else:
        ok = refused_out(text) and (len(w.player.queue), w.vc.stopped) == before
    check("%-16s outsider: %s" % (action, "allowed" if level is Level.OUTSIDER else "refused"), ok, text)
    # an admin outside the call
    w = world("set")
    text, _ = run(w, ADM, cmd, args)
    check("%-16s admin outside the call: allowed" % action, passed(text), text)

print()
print("=== with NO DJ role: behaviour is today's, for every control ===")
for action, cmd, args in CONTROLS:
    who = actor(action)
    w = world("none")
    text, _ = run(w, who, cmd, args)
    if action == "remove_other":
        ok = "only remove your own" in text and len(w.player.queue) == 3
        label = "a member may not remove someone else's song (admin-only, as before)"
    else:
        ok = passed(text)
        label = "anyone in the call: allowed"
    check("%-16s %s" % (action, label), ok, text)
    w = world("none")
    text, _ = run(w, OUT, cmd, args)
    check("%-16s outsider: %s" % (action, "allowed" if access.ACTIONS[action] is Level.OUTSIDER
                                  else "refused"),
          passed(text) if access.ACTIONS[action] is Level.OUTSIDER else refused_out(text), text)
w = world("none")
text, _ = run(w, ADM, "remove", [2])
check("remove_other: an admin removes anyone's song", "Removed" in text and len(w.player.queue) == 2, text)

print()
print("=== a deleted role counts as no role ===")
w = world("deleted")
check("the saved id is still stored", amy.db.get_guild_setting(GID, config.DJ_ROLE_KEY) == "123456")
check("dj_role_id ignores a role the server no longer has", amy.dj_role_id(w.guild) is None)
text, _ = run(w, LIS, "pause")
check("so a listener can pause again", passed(text), text)
check("a listener's level is DJ again (everyone in the call is)", amy.member_level(w.guild, LIS) is Level.DJ)
check("/dj show says the role was deleted", "deleted" in amy.dj_overview(w.guild), amy.dj_overview(w.guild))
w = world("set")
check("with the role present dj_role_id returns it", amy.dj_role_id(w.guild) == ROLE_ID)
check("a junk stored value counts as no role",
      (amy.db.set_guild_setting(GID, config.DJ_ROLE_KEY, "banana"), amy.dj_role_id(w.guild))[1] is None)

print()
print("=== alone with Amy: a DJ ===")
w = world("set")
w.ch.members = [w.m[LIS]]
check("the only person in the call is a DJ", amy.member_level(w.guild, LIS) is Level.DJ)
text, _ = run(w, LIS, "pause")
check("...so they can pause", passed(text), text)
text, _ = run(w, LIS, "skip")
check("...and skip someone else's song", "Skipped" in text, text)
w = world("set")
w.ch.members = [w.m[LIS], DMember(77, w.ch, "robot")]
w.ch.members[1].bot = True
check("a bot in the channel doesn't count as company", amy.member_level(w.guild, LIS) is Level.DJ)
w.ch.members = [w.m[LIS], w.m[LIS2]]
check("two people: a listener again", amy.member_level(w.guild, LIS) is Level.LISTENER)

print()
print("=== skipping the current song ===")
def slash_run(w, who, handler, *args):
    """Through the slash layer (simple_music -> send_reply), where privacy is decided."""
    it = as_(w, who)
    asyncio.run(amy.simple_music(it, handler, *args, cooldown=False))
    return text_of(None, it), it


w = world("set")
text, it = slash_run(w, LIS, amy.music_skip)
check("role set: a listener is told only a DJ can skip someone else's song",
      "Only a DJ can skip someone else's song." in text and not w.vc.stopped and not w.player.skip_requested, text)
check("...privately (ephemeral)", it.sent and it.sent[-1].ephemeral is True, [m.ephemeral for m in it.sent])
w = world("set")
text, it = slash_run(w, REQ, amy.music_skip)
check("a success is public (not ephemeral)", "Skipped" in text and it.sent[-1].ephemeral is False)
w = world("set")
text, _ = run(w, REQ, "skip")
check("role set: whoever queued the song skips it", "Skipped" in text and w.vc.stopped, text)
w = world("set")
text, _ = run(w, DJ, "skip")
check("role set: a DJ skips it", "Skipped" in text and w.vc.stopped, text)
w = world("set")
w.player.current.is_autoplay, w.player.current.requester_id, w.player.current.requested_by = True, None, "lis"
text, _ = run(w, LIS, "skip")
check("an Autoplay song is nobody's: a listener cannot skip it (a DJ can)",
      "Only a DJ can skip" in text, text)
w = world("none")
text, _ = run(w, LIS, "skip")
check("no role: anyone in the call still skips at once (Phase 3 makes this a vote)",
      "Skipped" in text and w.vc.stopped, text)
w = world("set")
text, _ = run(w, ADM, "skip")
check("an admin outside the call skips", "Skipped" in text, text)

print()
print("=== refusals never ping the role ===")
w = world("set")
text, it = slash_run(w, LIS, amy.music_pause)
check("the refusal names the role, escaped", markup.escape_markdown(ROLE_NAME) in text and ROLE_NAME not in text, text)
check("...and does not mention it by id", "<@&" not in text and str(ROLE_ID) not in text, text)
check("...it is an embed (embeds never ping) and ephemeral", it.sent[-1].embed is not None
      and it.sent[-1].ephemeral is True)
w = world("set")
text, _ = run(w, LIS, "remove", [2])
check("a listener removing someone else's song gets the DJ line",
      refused_dj(text) and len(w.player.queue) == 3, text)
w = world("set")
text, _ = run(w, DJ, "remove", [2])
check("a DJ removes someone else's song", "Removed" in text and len(w.player.queue) == 2, text)
w = world("set")
text, _ = run(w, LIS, "remove", [1])
check("a listener removes their own", "Removed" in text and len(w.player.queue) == 2, text)

# ---------------------------------------------------------------------------------------------
print()
print("=== no DJ role: an outsider's refusals are still private ===")
for action, cmd, args in CONTROLS:
    if access.ACTIONS[action] is Level.OUTSIDER:
        continue
    w = world("none")
    text, it = slash_run(w, OUT, getattr(amy, "music_" + cmd), *(args or []))
    check("%-16s outsider's refusal is ephemeral" % action,
          refused_out(text) and it.sent and it.sent[-1].ephemeral is True, text)

print()
print("=== a refusal on a deferred command replaces the public 'thinking' message ===")
_choice = app_commands.Choice(name="nightcore", value="nightcore")
for name, call in (("play", lambda it: amy.tree.get_command("play").callback(it, "a song")),
                   ("seek", lambda it: amy.tree.get_command("seek").callback(it, "0:30")),
                   ("filter", lambda it: amy.tree.get_command("filter").callback(it, _choice))):
    w = world("set")
    it = as_(w, OUT)
    asyncio.run(call(it))
    last = it.sent[-1] if it.sent else None
    check("/%s by an outsider: original deleted once, ephemeral follow-up, nothing changed" % name,
          it.deleted_original == 1 and last is not None and last.kind == "followup"
          and last.ephemeral is True and refused_out(text_of(None, it))
          and len(w.player.queue) == 3 and not w.vc.stopped, (it.deleted_original, text_of(None, it)))
    w = world("set")
    it = as_(w, OUT)
    it.delete_raises = discord.HTTPException(type("R", (), {"status": 404, "reason": "x"})(), "gone")
    crashed = None
    try:
        asyncio.run(call(it))
    except discord.HTTPException as e:
        crashed = e
    check("/%s: if the delete fails the refusal still goes out, privately" % name,
          crashed is None and it.deleted_original == 1 and it.sent and it.sent[-1].ephemeral is True
          and refused_out(text_of(None, it)), crashed or text_of(None, it))
w = world("set")
it = as_(w, LIS)
asyncio.run(amy.tree.get_command("seek").callback(it, "0:30"))
check("a DJ refusal on a deferred command is private too",
      it.deleted_original == 1 and it.sent[-1].ephemeral is True and refused_dj(text_of(None, it)))
w = world("set")
it = as_(w, DJ)
asyncio.run(amy.tree.get_command("seek").callback(it, "0:30"))
check("an allowed deferred command deletes nothing", it.deleted_original == 0)

print()
print("=== a required admin level answers with the admin wording ===")
w = world("none")
check("no role: a member asking for remove_other is told it is admin-only",
      amy.access_refusal(w.guild, LIS, "remove_other", "outsider words") == amy.ADMIN_ONLY_MSG)
check("...an outsider still gets the command's own wording",
      amy.access_refusal(w.guild, OUT, "remove_other", "outsider words") == "outsider words")
check("...an admin is not refused", amy.access_refusal(w.guild, ADM, "remove_other", "x") is None)
w = world("set")
check("with a role set the same action is a DJ matter for a listener",
      amy.access_refusal(w.guild, LIS, "remove_other", "x").startswith("Only a DJ can do that"))

print()
print("=== every control consults the table (walk) ===")
recorded = []
_real_allowed, _real_skip = access.allowed, access.skip_decision
access.allowed = lambda level, action, *a, **k: (recorded.append(action), _real_allowed(level, action, *a, **k))[1]
access.skip_decision = lambda *a, **k: (recorded.append("skip_decision"), _real_skip(*a, **k))[1]
try:
    for action, cmd, args in CONTROLS:
        recorded.clear()
        w = world("set")
        run(w, actor(action), cmd, args)
        expect = "skip_decision" if action in ("skip_force", "skip_own") else action
        check("/%-9s asks access.py for %r" % (cmd, expect), expect in recorded, recorded)
finally:
    access.allowed, access.skip_decision = _real_allowed, _real_skip

exempt = {"music_search": "shows results only; the picker asks (search_pick)",
          "music_volume": "admin-only", "music_clearqueue": "admin-only"}
handlers = sorted(n for n in dir(amy) if n.startswith("music_") and inspect.iscoroutinefunction(getattr(amy, n)))
for name in handlers:
    if name in exempt:
        continue
    src = inspect.getsource(getattr(amy, name))
    if name in ("music_seek", "music_replay"):
        src = inspect.getsource(amy._restart_current)
    check("%s asks the access rule in its own source" % name,
          "refusal(" in src or "access.allowed(" in src, name)
for name in ("music_volume", "music_clearqueue"):
    check("%s stays admin-only" % name, "is_admin()" in inspect.getsource(getattr(amy, name)))
check("no handler lacks both a check and an exemption", len(handlers) >= 17, handlers)
src_all = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "Amy_chatbot_V2.py"), encoding="utf-8").read()
for action in sorted(set(access.ACTIONS) - {"skip_force", "skip_own"}):
    check("the bot names action %r" % action, '"%s"' % action in src_all)
check("the skip pair goes through access.skip_decision",
      "access.skip_decision(" in inspect.getsource(amy.skip_refusal))
check("MusicContext has no copy of the old blanket rule", not hasattr(amy.MusicContext, "may_control"))

# ---------------------------------------------------------------------------------------------
print()
print("=== the player buttons: same checks, answered privately ===")
view = amy.PlayerControls()
check("no blanket interaction_check on PlayerControls", "interaction_check" not in vars(amy.PlayerControls))
for btn in ("previous_button", "pause_button", "skip_button", "stop_button", "shush_button"):
    src = inspect.getsource(getattr(amy.PlayerControls, btn))
    check("%s reaches a handler or the shared check" % btn,
          any(h in src for h in ("music_previous(", "music_pause", "music_skip(", "music_stop(", ".refusal(")), btn)
for btn, expect_blocked in (("previous_button", True), ("pause_button", True), ("skip_button", True),
                            ("stop_button", True), ("shush_button", False)):
    w = world("set")
    w.player.history.append(make_track(amy, "earlier"))
    it = as_(w, LIS)
    asyncio.run(getattr(amy.PlayerControls(), btn).callback(it))
    text = text_of(None, it)
    state_changed = w.vc.stopped or w.vc._paused or len(w.player.queue) != 3 or w.player.back_requested
    if expect_blocked:
        ok = refused_dj(text) and not state_changed and it.sent[-1].ephemeral is True
    else:
        ok = passed(text)
    check("%s as a listener with a DJ role set: %s" % (btn, "refused privately, nothing changed" if expect_blocked
                                                        else "allowed"), ok, text)
    w = world("set")
    w.player.history.append(make_track(amy, "earlier"))
    it = as_(w, DJ)
    asyncio.run(getattr(amy.PlayerControls(), btn).callback(it))
    check("%s as a DJ: not refused" % btn, passed(text_of(None, it)), text_of(None, it))
    w = world("set")
    it = as_(w, OUT)
    asyncio.run(getattr(amy.PlayerControls(), btn).callback(it))
    check("%s as an outsider: refused (%s)" % (btn, "privately"),
          refused_out(text_of(None, it)) and it.sent[-1].ephemeral is True, text_of(None, it))
w = world("none")
it = as_(w, LIS)
asyncio.run(amy.PlayerControls().pause_button.callback(it))
check("with no role the Pause button works for anyone in the call", w.vc._paused, text_of(None, it))
w = world("set")
it = as_(w, LIS)
w.player.history.append(make_track(amy, "earlier"))
asyncio.run(amy.PlayerControls().skip_button.callback(it))
check("the Skip button refuses a listener on someone else's song like /skip",
      "Only a DJ can skip someone else's song." in text_of(None, it) and not w.vc.stopped, text_of(None, it))

print()
print("=== the /search picker ===")
import discord


def pick(w, who):
    p = w.player
    p.current = None
    w.vc.stopped = True
    view = amy.SearchResults([make_track(amy, "Found")], who, w.guild, query="q")
    view.picker._values = ["0"]
    it = as_(w, who)
    asyncio.run(view.picker.callback(it))
    return len(p.queue), it


n0 = 3
n, it = pick(world("set"), LIS)
check("a listener may pick even with a DJ role set", n == n0 + 1, n)
n, it = pick(world("set"), OUT)
check("an outsider is refused, privately", n == n0 and it.sent[0].ephemeral
      and "to queue tracks" in text_of(None, it), text_of(None, it))
n, it = pick(world("set"), ADM)
check("an admin outside the call may pick", n == n0 + 1, n)

# ---------------------------------------------------------------------------------------------
print()
print("=== /dj ===")
w = world("none")
role = Role(ROLE_ID, ROLE_NAME)
reply = amy.dj_update(w.guild, "role", role, None)
check("/dj role stores the id", amy.db.get_guild_setting(GID, config.DJ_ROLE_KEY) == str(ROLE_ID))
check("...and the reply escapes the role name", markup.escape_markdown(ROLE_NAME) in text_of(reply), text_of(reply))
check("...and now a listener is refused", refused_dj(run(w, LIS, "pause")[0]))
reply = amy.dj_update(w.guild, "role", None, None)
check("/dj role without a role asks for one", reply.error and amy.db.get_guild_setting(GID, config.DJ_ROLE_KEY) == str(ROLE_ID))
reply = amy.dj_update(w.guild, "role", Role(GID, "@everyone"), None)
check("@everyone is refused as the DJ role", reply.error and amy.db.get_guild_setting(GID, config.DJ_ROLE_KEY) == str(ROLE_ID))
for what, bad_role in (("another bot's role", Role(6001, "Bot", bot=True)),
                       ("an integration role", Role(6002, "Twitch", integration=True))):
    reply = amy.dj_update(w.guild, "role", bad_role, None)
    check("%s is refused (no member can hold it)" % what,
          reply.error and "bot or an integration" in text_of(reply)
          and amy.db.get_guild_setting(GID, config.DJ_ROLE_KEY) == str(ROLE_ID), text_of(reply))
reply = amy.dj_update(w.guild, "role", Role(6003, "Server Booster", booster=True), None)
check("the booster role is allowed", not reply.error
      and amy.db.get_guild_setting(GID, config.DJ_ROLE_KEY) == "6003", text_of(reply))
amy.dj_update(w.guild, "role", role, None)
shown = amy.dj_overview(w.guild)
check("/dj show lists the role (escaped), the share and the DJs in Amy's channel",
      markup.escape_markdown(ROLE_NAME) in shown and "half" in shown and "dj" in shown.split("DJs in my channel now:")[1],
      shown)
check("...listeners are not in the DJ list", "lis" not in shown.split("DJs in my channel now:")[1].replace("dj", ""), shown)
w.m[DJ].display_name = "[x](http://e.example)"
shown = amy.dj_overview(w.guild)
check("...DJ names are escaped", "\\[x\\]" in shown or "[x](http" not in shown, shown)
w.m[DJ].display_name = "dj"
reply = amy.dj_update(w.guild, "votes", None, "two-thirds")
check("/dj votes stores the share", amy.vote_share(GID) == "two-thirds"
      and amy.db.get_guild_setting(GID, config.VOTE_SHARE_KEY) == "two-thirds")
reply = amy.dj_update(w.guild, "votes", None, "everyone")
check("/dj votes rejects an unknown share", reply.error and amy.vote_share(GID) == "two-thirds")
reply = amy.dj_update(w.guild, "votes", None, None)
check("/dj votes without a share asks for one", reply.error)
check("the default share is half", (amy.db.clear_guild_setting(GID, config.VOTE_SHARE_KEY), amy.vote_share(GID))[1] == "half")
amy.dj_update(w.guild, "votes", None, "majority")
check("a different server is unaffected",
      amy.vote_share(GID + 1) == "half" and amy.dj_role_id(world("none", gid=GID + 1).guild) is None)
w = world("set")
amy.dj_update(w.guild, "votes", None, "majority")
reopened = ConversationDB(amy.db.path)
check("the role and the share survive a restart (a fresh database connection)",
      config.parse_role_id(reopened.get_guild_setting(GID, config.DJ_ROLE_KEY)) == ROLE_ID
      and config.parse_vote_share(reopened.get_guild_setting(GID, config.VOTE_SHARE_KEY)) == "majority")
reopened.conn.close()
amy.dj_update(w.guild, "clear", None, None)
check("/dj clear removes the role", amy.db.get_guild_setting(GID, config.DJ_ROLE_KEY) is None
      and amy.dj_role_id(w.guild) is None and passed(run(w, LIS, "pause")[0]))
check("...and keeps the vote share", amy.vote_share(GID) == "majority")
shown = amy.dj_overview(w.guild)
check("/dj show with no role says everyone in the call is a DJ", "none - everyone in the call is a DJ" in shown, shown)
w = world("set")
w.guild.voice_client = None
check("/dj show without Amy in a channel says so", "not in a voice channel" in amy.dj_overview(w.guild))

# the slash command itself
w = world("none")
it = Interaction(w.m[ADM], w.guild)
cb = amy.tree.get_command("dj").callback
asyncio.run(cb(it, app_commands.Choice(name="role", value="role"), role, None))
check("the /dj slash command sets the role and answers ephemerally",
      amy.dj_role_id(w.guild) == ROLE_ID and it.sent and it.sent[-1].ephemeral is True)
it = Interaction(w.m[ADM], w.guild)
asyncio.run(cb(it, app_commands.Choice(name="show", value="show"), None, None))
check("/dj show answers ephemerally", it.sent and it.sent[-1].ephemeral is True
      and "DJ role" in text_of(None, it), text_of(None, it))
it = Interaction(w.m[ADM], w.guild)
asyncio.run(cb(it, app_commands.Choice(name="votes", value="votes"), None, app_commands.Choice(name="half", value="half")))
check("/dj votes via the slash command", amy.vote_share(GID) == "half" and it.sent[-1].ephemeral is True)
dj_cmd = amy.tree.get_command("dj")
check("/dj is admin-gated", bool(dj_cmd.checks))
check("/dj offers show, role, clear and votes",
      [c.value for c in {p.name: p for p in dj_cmd.parameters}["action"].choices] == ["show", "role", "clear", "votes"])
check("/dj share offers the three vote shares",
      [c.value for c in {p.name: p for p in dj_cmd.parameters}["share"].choices] == list(config.VOTE_SHARES))
check("the role option is a Discord role", {p.name: p for p in dj_cmd.parameters}["role"].type
      is discord.AppCommandOptionType.role)

(amy._restart_playing, amy.announce, amy.music.find_ffmpeg, amy.music.resolve_metadata,
 amy.lyrics.find_lyrics, amy.advance_playback) = _real

finish("ALL ACCESS TESTS PASSED")
