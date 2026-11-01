# access.py
"""
Who may do what with the music: the levels and the one action table (DJ_PLAN.md).

Pure and stdlib-only. The bot works out the facts (admin? in Amy's channel? holds the DJ role?
alone with her?) and asks this module what they add up to; every music command and player
button then asks `allowed` (or `skip_decision`) before it acts, so no control can forget the
check and a button can never be a way around a command. tests/test_access.py walks the table.
"""

import enum
from typing import Dict


class Level(enum.IntEnum):
    """Highest wins. An Admin may do everything a DJ may, and so on down."""
    OUTSIDER = 0    # not in Amy's voice channel: can look, not change
    LISTENER = 1    # in her channel, not a DJ
    DJ = 2          # in her channel with the DJ role, alone with her, or no DJ role is set
    ADMIN = 3       # owner / Administrator / the admin role - even outside the call


def access_level(is_admin: bool, in_channel: bool, dj_role_set: bool,
                 has_dj_role: bool, alone: bool) -> Level:
    """
    The level the facts add up to.

    An admin is an Admin wherever they are. Anyone else outside Amy's channel is an Outsider.
    In the channel: a DJ when they hold the DJ role or are the only person in the call - and
    with no DJ role set at all, everyone in the call is a DJ (the default: nothing changes
    for a server that never uses the feature). Otherwise a Listener.
    """
    if is_admin:
        return Level.ADMIN
    if not in_channel:
        return Level.OUTSIDER
    if not dj_role_set or has_dj_role or alone:
        return Level.DJ
    return Level.LISTENER


# Every control and the level it needs. "Shared playback" is DJ-only; queueing, picking your
# own results, taking back your own songs and quieting Amy are open to the whole call;
# looking is open to everyone. /volume and /clearqueue stay admin-only (checked as such in the
# bot, not through this table); /join and /leave keep their own rules.
ACTIONS: Dict[str, Level] = {
    # DJ only
    "stop": Level.DJ,
    "skip_force": Level.DJ,         # skipping without a vote (someone else's song)
    "skipto": Level.DJ,
    "previous": Level.DJ,
    "seek": Level.DJ,
    "replay": Level.DJ,
    "filter_set": Level.DJ,
    "loop": Level.DJ,
    "shuffle": Level.DJ,
    "pause": Level.DJ,
    "resume": Level.DJ,
    "autoplay_set": Level.DJ,
    "remove_other": Level.DJ,       # someone else's queued song (see ADMIN_WITHOUT_ROLE)
    # Anyone in the call
    "play": Level.LISTENER,
    "search_pick": Level.LISTENER,
    "skip_own": Level.LISTENER,     # skipping a song you queued yourself
    "remove_own": Level.LISTENER,
    "shush": Level.LISTENER,
    # Anyone, in the call or not
    "queue_view": Level.OUTSIDER,
    "nowplaying": Level.OUTSIDER,
    "lyrics": Level.OUTSIDER,
    "filter_status": Level.OUTSIDER,
    "autoplay_status": Level.OUTSIDER,
}

# With no DJ role set, everyone in the call is a DJ - except where today's rule was stricter:
# only an admin may remove somebody else's queued song. A DJ role set opens that to DJs.
ADMIN_WITHOUT_ROLE = frozenset({"remove_other"})


def required_level(action: str, dj_role_set: bool = False) -> Level:
    """The level `action` needs (KeyError for a name not in ACTIONS: a typo must be loud)."""
    if action in ADMIN_WITHOUT_ROLE and not dj_role_set:
        return Level.ADMIN
    return ACTIONS[action]


def allowed(level: Level, action: str, dj_role_set: bool = False) -> bool:
    """Whether someone at `level` may do `action`."""
    return level >= required_level(action, dj_role_set)


class SkipDecision(enum.Enum):
    ALLOWED = "allowed"
    NOT_IN_CALL = "not_in_call"     # an outsider
    DJ_ONLY = "dj_only"             # a listener, someone else's song, a DJ role is set


def skip_decision(level: Level, is_requester: bool, alone: bool,
                  dj_role_set: bool) -> SkipDecision:
    """
    May this person skip the current song at once?

    An admin always may; an outsider never. With no DJ role set, anyone in the call may (as
    before - Phase 3 turns this into a vote for someone else's song). With one set: a DJ, the
    person who queued the song, or someone alone with Amy may; a listener may not.
    """
    if level >= Level.ADMIN:
        return SkipDecision.ALLOWED
    if level < Level.LISTENER:
        return SkipDecision.NOT_IN_CALL
    if not dj_role_set:
        return SkipDecision.ALLOWED
    if level >= Level.DJ or is_requester or alone:
        return SkipDecision.ALLOWED
    return SkipDecision.DJ_ONLY
