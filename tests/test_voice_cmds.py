import sys, os, io, asyncio, importlib.util
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
# Resolve the project root from this file, so the suite runs from any checkout
PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJ)
os.chdir(PROJ)

spec = importlib.util.spec_from_file_location("amy", os.path.join(PROJ, "Amy_chatbot_V2.py"))
amy = importlib.util.module_from_spec(spec)
sys.modules["amy"] = amy
spec.loader.exec_module(amy)

# The real get_voice_client/active_channel isinstance-check against discord.VoiceClient
# and discord.VoiceChannel. These lightweight mocks aren't real discord objects, so swap
# in duck-typed equivalents. The narrowing itself is exercised in production types;
# what these tests target is the command decision flow.
amy.voice.get_voice_client = lambda guild: guild.voice_client

def _duck_active_channel(vc):
    if vc is None or not vc.is_connected():
        return None
    return vc.channel

amy.voice.active_channel = _duck_active_channel


class Perms:
    def __init__(self, connect=True, speak=True, manage_channels=True, administrator=False):
        self.connect = connect
        self.speak = speak
        self.manage_channels = manage_channels
        self.administrator = administrator


class VC:  # voice channel
    def __init__(self, id_, name, members=(), perms=None):
        self.id = id_
        self.name = name
        self.members = list(members)
        self._perms = perms or Perms()
    def permissions_for(self, _who):
        return self._perms


class VoiceClient:
    def __init__(self, channel):
        self.channel = channel
        self.moved_to = None
    def is_connected(self):
        return True
    async def move_to(self, ch):
        self.moved_to = ch
        self.channel = ch


class Member:
    def __init__(self, id_, voice_channel=None, is_bot=False):
        self.id = id_
        self.bot = is_bot
        self.roles = []
        self.guild_permissions = Perms()
        self.voice = type("VS", (), {"channel": voice_channel})() if voice_channel else None


class Guild:
    def __init__(self, owner_id, member, voice_client=None, me_perms=None):
        self.id = 500
        self.owner_id = owner_id
        self._member = member
        self.voice_client = voice_client
        self.me = type("Me", (), {"guild_permissions": me_perms or Perms()})()
    def get_member(self, uid):
        return self._member if self._member and self._member.id == uid else None


class Msg:
    def __init__(self, author_id, guild):
        self.author = type("A", (), {"id": author_id})()
        self.guild = guild
        self.channel = type("C", (), {"id": 77, "category": None})()


def run(cmd, msg, reset_cooldown=True):
    # Each case is independent unless it is explicitly testing the cooldown
    if reset_cooldown:
        amy.voice_cmd_store.clear()
    return text_of(asyncio.run(amy.execute_command(cmd, msg)))

def text_of(resp):
    """Flatten a handler result (str or ui.Reply) into searchable text."""
    if isinstance(resp, str):
        return resp
    parts = [resp.content or ""]
    e = resp.embed
    if e is not None:
        parts += [e.title or "", e.description or ""]
        if e.author:
            parts.append(e.author.name or "")
        for f in e.fields:
            parts += [f.name or "", str(f.value or "")]
    return chr(10).join(parts)




OWNER, USER = 42, 99
results = []

def check(label, got, expect_substr):
    ok = expect_substr.lower() in got.lower()
    results.append(ok)
    print(("PASS" if ok else "FAIL"), "|", label)
    if not ok:
        print("      expected substring:", expect_substr)
        print("      got:", got)


# 1. Voice command in a DM
check("DM rejected", run("join", Msg(USER, None)), "only work in a server")

# 2. User not in any voice channel
m = Member(USER)
check("not in a VC", run("join", Msg(USER, Guild(OWNER, m))), "not in a voice channel")

# 3. Bot lacks Connect permission
target = VC(10, "General", perms=Perms(connect=False))
m = Member(USER, voice_channel=target)
check("no Connect perm", run("join", Msg(USER, Guild(OWNER, m))), "permission to connect")

# 4. Bot lacks Speak permission
target = VC(10, "General", perms=Perms(connect=True, speak=False))
m = Member(USER, voice_channel=target)
check("no Speak perm", run("join", Msg(USER, Guild(OWNER, m))), "not allowed to speak")

# 5. Already in the target channel
target = VC(10, "General")
m = Member(USER, voice_channel=target)
g = Guild(OWNER, m, voice_client=VoiceClient(target))
check("already there", run("join", Msg(USER, g)), "already in")

# 6. Elsewhere + occupied + non-admin -> blocked
occupied = VC(99, "Busy Room", members=[Member(1), Member(2)])
target = VC(10, "General")
m = Member(USER, voice_channel=target)
g = Guild(OWNER, m, voice_client=VoiceClient(occupied))
check("blocked: occupied, non-admin", run("join", Msg(USER, g)), "with other people")

# 7. Same, but requester is the server owner (admin) -> moves
occupied = VC(99, "Busy Room", members=[Member(1), Member(2)])
target = VC(10, "General")
m = Member(OWNER, voice_channel=target)
g = Guild(OWNER, m, voice_client=VoiceClient(occupied))
check("admin overrides occupied", run("join", Msg(OWNER, g)), "moved to")

# 8. Elsewhere + empty -> non-admin may move
empty = VC(99, "Empty Room", members=[Member(7, is_bot=True)])  # bot only = no humans
target = VC(10, "General")
m = Member(USER, voice_channel=target)
g = Guild(OWNER, m, voice_client=VoiceClient(empty))
check("move when old channel empty", run("join", Msg(USER, g)), "moved to")

# 9. /create as non-admin
m = Member(USER)
check("/create non-admin", run("create Music", Msg(USER, Guild(OWNER, m))), "don't have permission")

# 10. /create without Manage Channels
m = Member(OWNER)
g = Guild(OWNER, m, me_perms=Perms(manage_channels=False))
check("/create no Manage Channels", run("create Music", Msg(OWNER, g)), "manage channels")

# 11. /leave when not connected
m = Member(OWNER)
check("/leave not connected", run("leave", Msg(OWNER, Guild(OWNER, m))), "not in a voice channel")

# 12. /leave by an outsider (not admin, not in the channel)
ch = VC(10, "General")
m = Member(USER)  # not in any VC
g = Guild(OWNER, m, voice_client=VoiceClient(ch))
check("/leave outsider blocked", run("leave", Msg(USER, g)), "need to be in my voice channel")

# 13. Voice cooldown kicks in for non-admins (VOICE_CMD_MAX = 3)
amy.voice_cmd_store.clear()
m = Member(USER)
g = Guild(OWNER, m)
outs = [run("join", Msg(USER, g), reset_cooldown=False) for _ in range(5)]
check("cooldown after 3 uses", outs[-1], "too many voice commands")

# 14. Admins are exempt from the cooldown
amy.voice_cmd_store.clear()
m = Member(OWNER)
g = Guild(OWNER, m)
outs = [run("join", Msg(OWNER, g), reset_cooldown=False) for _ in range(6)]
check("admin exempt from cooldown", outs[-1], "not in a voice channel")

print()
print("{}/{} passed".format(sum(results), len(results)))
sys.exit(0 if all(results) else 1)
