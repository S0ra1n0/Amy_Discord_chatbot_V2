# Amy Chatbot V2

A sophisticated Discord bot powered by local language models using Ollama. Amy acts as a personal assistant with the demeanor of a professional secretary, ready to chat, answer questions, and execute commands.

## Introduction

Amy is an intelligent personal assistant bot that runs on your Discord server. She provides thoughtful responses with context awareness, remembering recent conversations to provide more relevant answers. Beyond conversation, Amy can execute various commands to perform specific tasks (full command list in 'Available Commands' section).

**Key Features:**

- Conversational AI powered by Ollama with **real-time streaming responses**
- **Web search** — Amy searches on her own when a question needs current information, then answers in her own words with sources
- **Voice channel support** — summon Amy into your voice channel, or have her create one
- **Music playback** — queue tracks from YouTube (search, URL, or whole playlists), direct audio URLs, or local files
- **Queue management** — paginated listing, remove, shuffle, skip-to, and clear
- **`/search` picker** — choose from the top 5 matches instead of accepting the first hit
- **Rich embeds and player buttons** — now-playing card with thumbnail, plus Pause/Skip/Stop buttons that survive a bot restart
- Runtime model switching — swap the active Ollama model without restarting the bot
- Long responses/replies automatically split across multiple Discord messages instead of being truncated
- Persistent conversation memory stored in SQLite (survives restarts, remembers last 10 messages per channel)
- Automatic daily pruning of old conversation history
- Admin access control — admin commands restricted to server owner or users with the `Admin` role
- Rate limiting — non-admin users are capped at 5 chat messages per hour, and 3 voice commands per minute
- **Native slash commands** — Amy's commands appear in Discord's own autocomplete, with typed arguments Discord validates before they reach the bot

## Functions & Commands

### Chat

Simply message Amy naturally — she maintains conversation context and responds thoughtfully. Amy replies with a live streaming message that updates in real-time as she generates her response, so you see output immediately instead of waiting for the full reply.

> **Note:** Non-admin users are limited to **5 messages per hour**. Admins (server owner or `Admin` role) have no limit.

### Available Commands

| Command                  | Description                                                               | Access     |
| ------------------------ | ------------------------------------------------------------------------- | ---------- |
| `/help`                  | Display all available commands                                            | Everyone   |
| `/toggle`                | Enable/disable bot responses to chat (commands still work)                | Admin only |
| `/status`                | Show bot state, Ollama connectivity, memory stats and rate limit info     | Admin only |
| `/model`                 | Show the current Ollama model                                             | Admin only |
| `/model [name]`          | Switch to a different installed Ollama model (e.g., `/model qwen2.5:3b`)  | Admin only |
| `/forget`                | Wipe conversation memory for the current channel (asks for confirmation)  | Admin only |
| `/dice [sides] [amount]` | Roll dice — defaults to one 6-sided; shows each roll when rolling several | Everyone   |
| `/rng [min] [max]`       | Generate a random number between min and max (e.g., `/rng 0 999`)         | Everyone   |
| `/websearch [query]`     | Search the web and show the results                                       | Everyone   |
| `/join`                  | Bring Amy into the voice channel you're currently in                      | Everyone   |
| `/leave`                 | Make Amy leave her voice channel                                          | In-channel or admin |
| `/create [name]`         | Create a new voice channel and have Amy join it (e.g., `/create Music Room`) | Admin only |
| `/play [query]`          | Queue a track — song name, YouTube/audio URL, or local file path          | In-channel |
| `/search [song]`         | Show the top 5 matches and pick one from a menu                           | In-channel |
| `/pause` / `/resume`     | Pause or resume playback                                                  | In-channel |
| `/skip`                  | Skip the current track                                                    | In-channel |
| `/stop`                  | Stop playback and clear the queue (Amy stays in the channel)              | In-channel or admin |
| `/queue [page]`          | Show what's playing and what's queued, 10 per page                        | Everyone   |
| `/remove [position]`     | Remove a queued track (your own; admins can remove any)                   | In-channel |
| `/skipto [position]`     | Jump ahead to a queued track, dropping the ones before it                 | In-channel |
| `/shuffle`               | Shuffle the queued tracks (current track keeps playing)                   | In-channel |
| `/clearqueue`            | Empty the queue without stopping the current track                        | Admin only |
| `/nowplaying`            | Show the current track                                                    | Everyone   |
| `/loop [off\|track\|queue]` | Set repeat mode                                                        | In-channel |
| `/volume [0-100]`        | Show or set playback volume                                               | Admin only |

### Web search

Amy's model has no built-in knowledge of current events. She now decides for herself when a
question needs looking up — ask about news, recent results or anything time-sensitive and
she searches, then answers in her own words citing what she found. Ordinary questions
(maths, greetings, writing) are answered directly with no search.

`/websearch <query>` does the same search manually and shows the raw results, which is
useful when you want the sources themselves, or when she judges a question doesn't need
looking up.

> **How reliable is the automatic part?** The model decides, so it's a judgement call rather
> than a rule. In testing it searched all four time-sensitive questions and correctly skipped
> maths, a greeting and a haiku — but borderline questions (a historical fact phrased like a
> current one) can go either way. `/websearch` is the deterministic fallback.

Search uses **DuckDuckGo with no API key**. It parses their HTML page rather than an API, so
like `yt-dlp` it can break when they change their markup — set `WEB_SEARCH=false` in `.env`
to turn the feature off if that happens. Failures degrade to "I couldn't find anything"
rather than breaking the conversation.

### How commands work

Amy uses Discord's **native slash commands**. Type `/` in any channel and her commands appear
in the autocomplete list with descriptions and typed arguments, the same as any other bot.

Discord validates arguments before they reach Amy: `/dice` won't accept more than 100 dice,
`/volume` is limited to 0–100, and `/loop` presents a three-option picker rather than free
text. Permission refusals are **ephemeral** — only you see them, so they don't clutter the
channel.

Commands register to the server named by `GUILD_ID` in `.env`, which makes them appear
instantly. Without it they register globally and can take up to an hour to show up.

> Typing a command as an ordinary message (`/play something`) no longer works — use the
> real slash command. Talking to Amy normally is unchanged.

### Admin Access

A user is considered an admin if they meet **any** of the following:
- They are the **server owner**, or
- They have the Discord **Administrator** permission, or
- They have the Discord role named **`Admin`** (configurable via `ADMIN_ROLE_NAME` in `.env`)

> **Note:** `/help` shows admin commands only to users who qualify as admins. Regular users see only the commands available to them.

### Voice Channels

Amy can join **guild voice channels** — the ones in your server's sidebar. (Discord does not allow bots into DM or group-DM calls at all, so voice features are server-only.)

- **`/join`** — you're already in a voice channel and want Amy there too. If she's sitting in another channel that still has people in it, she won't abandon them: only an admin can pull her away. If that channel is empty, anyone can move her.
- **`/create [name]`** — Amy makes a brand-new voice channel (in the same category as the text channel you typed in) and joins it, then you hop in. Name defaults to `Amy's Room`.
- **`/leave`** — disconnects her. If she created the channel, it's deleted on the way out.

Amy **auto-leaves after 60 seconds** once the last human leaves her channel, deleting the channel if she created it. Channels she created also get cleaned up on the next startup if the bot restarts while one is still lying around.

Voice commands are limited to **3 per minute** per non-admin user, so they can't be used to make Amy flap between channels. `/queue` and `/nowplaying` are exempt — checking what's playing never costs you a slot.

### Music

`/play` accepts four kinds of input:

| Input | Example |
| ----- | ------- |
| Song name (searches YouTube) | `/play never gonna give you up` |
| A URL (YouTube, SoundCloud, direct audio, radio stream) | `/play https://youtu.be/dQw4w9WgXcQ` |
| A **playlist** URL (queues up to 50 tracks) | `/play https://youtube.com/playlist?list=...` |
| A local file, if `MUSIC_DIR` is configured | `/play song.mp3` |

If Amy isn't in a voice channel, `/play` pulls her into yours automatically. Tracks queue up to **100** deep, and `/queue` pages through them 10 at a time (`/queue 2`). Positions shown are absolute, so the numbers line up with what `/remove` and `/skipto` expect.

**Playlists.** Pasting a playlist URL queues up to **50** tracks at once; Amy reports how many loaded and how many were skipped as unavailable or over the cap. Share links that name a single video (`watch?v=VIDEO&list=PLAYLIST` and `youtu.be/VIDEO?list=PLAYLIST`) queue **only that one video**, so an ordinary YouTube link can't dump hundreds of tracks into the queue. Unavailable entries (private, deleted, age-restricted) are skipped rather than failing the whole request.

**Local files are off by default.** Set `MUSIC_DIR` in `.env` to a folder to enable them, and `/play` will only read files inside it — `..`, symlinks, and absolute paths pointing elsewhere are all rejected. Without this restriction, any server member could name any path on the host and confirm whether it exists.

`/play` replies immediately with **🔍 Searching...**, then updates that same message as it resolves and starts playing, so you can see it's working during the few seconds YouTube lookup takes.

**Loop modes** — `/loop off` (default), `/loop track` (repeat current), `/loop queue` (rotate the whole queue endlessly). An explicit `/skip` or `/skipto` always moves on, even under `track` loop — otherwise the song would repeat forever with no way out. Under `queue` loop a skipped track still rotates to the back.

### Searching

`/play` takes whichever result YouTube ranks first, which is not always the one you meant.
`/search <song>` instead lists the **top 5** matches and gives you a dropdown to pick from.

Only the person who ran the search can choose from their own results, and the menu expires
after **60 seconds** — it then greys out rather than acting on a stale search. Picking a
track behaves exactly like `/play`: Amy joins your channel if she isn't already, and either
starts playing or adds it to the queue.

### Player controls

The now-playing card carries three buttons — **Pause/Resume**, **Skip** and **Stop**.

They follow exactly the same permission rule as the equivalent commands: you must be in Amy's
voice channel, or be an admin. Pressing one without permission gets a private (ephemeral)
refusal that nobody else sees, so a button is never a way around a command's check.

The buttons are *persistent*: they have no expiry and keep working after Amy restarts. A
default Discord component set expires after three minutes, which would leave dead buttons
partway through a song.

Amy keeps **one** now-playing card per server and edits it in place as tracks change, rather
than posting a new message for every track in a long queue. `/nowplaying` always posts a
fresh card if the old one has scrolled away.

### Volume and audio quality

Volume defaults to **100%**, and that's deliberate. At full volume Amy copies the opus stream from YouTube straight to Discord with no decoding or re-encoding — far less CPU, which is the main cause of stuttering.

Setting volume **below 100%** forces her to decode audio to raw PCM so the level can be scaled. That works fine, but costs noticeably more CPU and is more likely to stutter on a busy machine. The change also applies from the **next track**, since a passthrough stream has no volume stage to adjust mid-song.

If you just want Amy quieter, prefer Discord's own per-user volume slider (right-click Amy → Volume). It's client-side, costs nothing, and is per-listener.

**Amy disconnects on her own in two cases:** 60 seconds after the last person leaves her channel, and 5 minutes after the queue runs dry. Either way the queue is cleared, so she never resumes stale music when re-summoned.

> **A note on YouTube:** playback relies on `yt-dlp`. YouTube changes its internals regularly, so if `/play` suddenly stops finding tracks, update it — `pip install -U yt-dlp` — rather than assuming the bot broke. Be aware that streaming YouTube audio is contrary to YouTube's Terms of Service; that's the reason large public music bots have been shut down. Fine for a private personal bot, but worth knowing.

## How It Runs

1. The bot connects to your Discord server using your bot token
2. When a message is received:
   - If it starts with `/`, it's treated as a command and executed
   - Otherwise, it's passed to Amy who responds using the Ollama model (if the bot is enabled)
3. All conversations are saved to a local SQLite database (`amy_memory.db`) — history persists across restarts
4. A background task runs every 24 hours and deletes conversation history older than `DB_PRUNE_DAYS` (default 30 days)
5. Non-admin users are rate-limited to 5 chat messages per hour; excess messages receive a cooldown reply
6. Chat responses stream token-by-token: Amy sends a "Thinking..." placeholder then edits it live as the model generates output
7. Any reply or streamed response longer than Discord's 2000-character limit is automatically split across multiple messages, breaking at a word/line boundary where possible
8. Use `/toggle` (Admin only) to enable/disable bot chat responses without shutting down the bot
9. Use `/status` (Admin only) to check bot state, Ollama connectivity, memory stats, and how many users are currently throttled
10. Use `/model` (Admin only) to view or switch the active Ollama model at runtime — the new model is validated against Ollama's installed model list before switching
11. Use `/clear` (Admin only) to wipe conversation memory for a channel — Amy will ask for emoji confirmation first
12. Use `/join`, `/create` and `/leave` to control which voice channel Amy sits in; she auto-leaves 60s after the last person departs
13. `/play` queues music and pulls Amy into your voice channel if she isn't already there; the queue advances automatically, and she disconnects 5 minutes after it runs dry

## Setup & Installation

### Prerequisites

- Python 3.11 or higher
- Ollama installed and running locally
- Discord bot token from [Discord Developer Portal](https://discord.com/developers/applications)
- Git (optional, for cloning)

### Step 1: Clone or Download the Repository

```bash
git clone <repository-url>
cd Amy_chatbot_V2
```

### Step 2: Create a Virtual Environment

Strongly recommended. If you have more than one Python installed, a venv guarantees the bot, your editor, and your terminal all use the same interpreter and packages.

```bash
py -3.11 -m venv .venv
```

Activate it:

```bash
.venv\Scripts\activate
```

On macOS/Linux use `source .venv/bin/activate`. Once active, plain `python` and `pip` refer to the venv. VS Code auto-detects `.venv` and uses it for both analysis and running.

### Step 3: Install Dependencies

```bash
pip install -r requirements.txt
```

Versions are pinned to the exact set the project is tested against. If you skipped the venv, make sure you install into the same interpreter you'll run the bot with — e.g. `py -3.11 -m pip install -r requirements.txt`.

> ⚠️ Install via `requirements.txt`, not by hand. It specifies `discord.py[voice]`, and that **`[voice]` extra is required** — it pulls `PyNaCl` and `davey`, both of which discord.py needs before it will open a voice connection. Installing plain `discord.py` leaves the bot starting up perfectly fine but failing on `/join` at runtime.

Verify the install:

```bash
python -c "import discord.voice_client as v; print(v.has_nacl, v.has_dave)"
```

Both must print `True`.

### Step 4: Configure Environment Variables

1. Create an application on the Discord Developer Portal and copy your bot token from the "Bot" section
2. Copy `.env.example` to `.env`
3. Edit `.env` and fill in your values:

```
DISCORD_TOKEN=your_actual_discord_bot_token_here
ADMIN_ROLE_NAME=Admin
GUILD_ID=your_server_id
DB_PRUNE_DAYS=30
FFMPEG_PATH=
```

- `ADMIN_ROLE_NAME` is the name of the Discord role that grants admin access to bot commands. It defaults to `Admin` if not set.
- `WEB_SEARCH` enables Amy's web search. On by default; set `false` to disable it entirely.
- `OLLAMA_THINK` controls whether the model's reasoning phase is enabled. **Off by default.** Reasoning models like `qwen3.5` can spend their entire token budget thinking and return an empty answer; disabling it fixes that and cut replies from ~33s to ~4s in testing.
- `GUILD_ID` is your server's ID. Slash commands register to that guild and appear **instantly**; leave it blank to register globally, which can take up to an hour to propagate. Enable Developer Mode in Discord, then right-click your server → Copy Server ID.
- `DB_PRUNE_DAYS` controls how many days of conversation history are kept before automatic pruning removes them. Defaults to `30` if not set.
- `FFMPEG_PATH` is optional. Leave it blank to find FFmpeg on PATH; set it to the full path of `ffmpeg.exe` if PATH isn't picking it up (see Step 6).
- `MUSIC_DIR` is optional and blank by default, which **disables local file playback**. Set it to a music folder to let `/play` read files from there. Only that folder is reachable.

### Step 5: Enable the Server Members Intent (required)

Voice features need this. In the [Discord Developer Portal](https://discord.com/developers/applications) → your application → **Bot** → **Privileged Gateway Intents**, turn on **Server Members Intent**.

> ⚠️ The code sets `intents.members = True`. If you don't enable it in the portal, **the bot will not start** — it exits with `PrivilegedIntentsRequired`. The two must match.

Amy also needs these permissions in your server (Server Settings → Roles → her role):

| Permission | Needed for |
| ---------- | ---------- |
| **Connect** | `/join`, `/create` — joining voice at all |
| **Speak** | Audio playback (and checked up-front by `/join`) |
| **Manage Channels** | `/create` — making and deleting voice channels |

Note that a single voice channel can override the role-level setting, so a channel-specific deny will still block her there. `/join` reports exactly which permission is missing.

### Step 6: Install FFmpeg (required for music)

FFmpeg decodes every audio source. Without it the bot runs fine but `/play` refuses to start.

```bash
winget install Gyan.FFmpeg
```

macOS: `brew install ffmpeg`. Linux: `sudo apt install ffmpeg`.

**Restart your terminal afterwards** — the installer updates PATH, but already-running shells keep the old copy. Verify:

```bash
ffmpeg -version
```

If you can't restart, or FFmpeg lives somewhere unusual, set `FFMPEG_PATH` in `.env` to the full path of `ffmpeg.exe` instead. `/status` reports whether Amy can currently find it.

### Step 7: Start Ollama

Make sure Ollama is running and the model is available:

```bash
ollama pull qwen3.5:2b  # Change if you use a different model
```

Change the default model name in `Amy_chatbot_V2.py` if you are not using `qwen3.5:2b`:

```python
model = "qwen3.5:2b"  # Ollama model name (replace with your model name)
```

> This sets the model used at startup. Admins can switch to any other installed model at runtime with `/model [name]` without restarting the bot.

### Step 8: Run the Bot

With the venv active:

```bash
python Amy_chatbot_V2.py
```

Or without activating it:

```bash
.venv\Scripts\python.exe Amy_chatbot_V2.py
```

> **`ModuleNotFoundError: No module named 'discord'`** means you're running a different Python than the one the packages are installed in. Activate the venv, or call `.venv\Scripts\python.exe` directly. This is by far the most common setup problem — the venv in Step 2 exists to prevent it.

The bot should now be online and ready to respond in your Discord server!

## Project Structure

```
Amy_chatbot_V2/
├── Amy_chatbot_V2.py       # Main bot file
├── database.py             # SQLite conversation memory layer
├── voice.py                # Voice channel connection management
├── music.py                # Track resolution, queues, and playback engine
├── ui.py                   # Embed builders and the reply wrapper
├── websearch.py            # DuckDuckGo search and the web_search tool
├── commands_help.py        # Help command text
├── tests/                  # Test suites (see tests/README.md)
├── requirements.txt        # Pinned Python dependencies
├── amy_memory.db           # SQLite conversation store, auto-created (Git ignored)
├── .venv/                  # Virtual environment (Git ignored)
├── .vscode/                # Editor settings, points at .venv (Git ignored)
├── .env                    # Environment variables (Git ignored)
├── .env.example            # Template for environment variables
├── .gitignore              # Git ignore rules
└── README.md               # This file
```

## Testing

```bash
python tests/run_tests.py             # offline suites only (~8s)
python tests/run_tests.py --all       # + network and live-Discord checks
```

14 suites covering queue logic, voice permissions, embed limits, the `MUSIC_DIR` sandbox,
and a docs audit that fails if a command is missing from the help text or this README.
Offline suites need no network and no Discord token. See [tests/README.md](tests/README.md).

## Troubleshooting

**Amy replies "I ran out of room before I could finish that answer":**

The model hit its token ceiling before writing anything. This happens with reasoning models
that over-think a question. Make sure `OLLAMA_THINK=false` in `.env` (the default), or raise
the limit with a larger model. Rephrasing the question usually won't help.

**Amy takes 30+ seconds to reply:**

Almost always the reasoning phase. Set `OLLAMA_THINK=false` in `.env`. In testing this took a
question from 33s down to 4s and turned an empty answer into a complete one.

**Bot doesn't respond to chat:**

- Ensure Ollama is running (`ollama serve`)
- Check that the model you want to use is installed (`ollama list`)
- Verify your Discord token is correct in `.env`
- Check if the bot was disabled with `/toggle`
- Check if you have hit the rate limit (5 messages/hour for non-admins)
- Use `/status` (Admin) to quickly diagnose connectivity and bot state

**Amy replies with "Thinking..." but never updates:**

- Ollama may have become unavailable mid-stream — Amy will edit the message with an error after the stream times out
- Confirm Ollama is still running with `ollama serve`

**`/model [name]` says "Model not found":**

- The model must already be pulled locally. Run `ollama pull <model_name>` first, then try `/model <model_name>` again
- Use `/model` with no arguments to see the current model, or `ollama list` in a terminal to see what's installed

**Bot won't start — `PrivilegedIntentsRequired`:**

- The code sets `intents.members = True` but the **Server Members Intent** isn't enabled in the Developer Portal. Enable it (see Step 5).

**`/join` says "Voice support isn't fully installed":**

discord.py 2.7 needs **two** libraries before it will open a voice connection — `PyNaCl` and `davey` (Discord's DAVE encrypted-voice protocol). Installing plain `discord.py`, or installing `PyNaCl` on its own, leaves voice broken at runtime even though the bot starts fine.

Always install the extra, which pulls both at the versions discord.py supports:

```bash
pip install "discord.py[voice]"
```

To check which one is missing:

```bash
python -c "import discord.voice_client as v; print(v.has_nacl, v.has_dave)"
```

Both must print `True`. Note that discord.py pins `PyNaCl<1.6`, so a bare `pip install PyNaCl` can pull an unsupported version — let the extra resolve it.

**`/join` says it can't connect or can't speak:**

- Amy's role is missing **Connect** / **Speak**, or that specific voice channel overrides her role and denies it. Check both the role permissions and the channel's own permission overrides.

**`/create` says it needs Manage Channels:**

- Grant **Manage Channels** to Amy's role. She needs it both to create the channel and to delete it afterwards.

**`/play` says FFmpeg isn't available:**

- FFmpeg isn't installed, or isn't on the PATH of the process running the bot. Install it (Step 6) and **restart your terminal** — a fresh install won't reach an already-running shell. Alternatively set `FFMPEG_PATH` in `.env`.
- `/status` shows whether Amy can currently find FFmpeg.

**`/play` can't find any tracks / used to work and now doesn't:**

- Update yt-dlp: `pip install -U yt-dlp`. YouTube changes its internals often, and this is the usual cause rather than a bug in the bot.

**Music stutters or cuts out:**

Discord audio must deliver a packet every 20ms, so stuttering is almost always the host machine missing that deadline rather than a bug. In rough order of impact:

- **Check CPU load.** A machine already running near capacity will drop packets. Note that Ollama inference spikes CPU hard — if Amy stutters *while she's answering a chat message*, that's the cause. A smaller model, or not chatting during playback, fixes it.
- **Keep volume at 100%.** Below that, Amy must decode and re-encode audio instead of passing it through, which costs meaningfully more CPU. Use Discord's per-user volume slider instead (right-click Amy → Volume).
- **Network.** The stream reconnects automatically through brief drops (`-reconnect`, `-reconnect_on_network_error`), but a weak link to YouTube or Discord will still be audible.

Long queues are not a factor — stream URLs are resolved one track at a time, immediately before playing, so they can't expire while waiting.

**Amy leaves voice on her own / never leaves:**

- She auto-leaves 60 seconds after the last non-bot member departs. If she *never* leaves, occupancy detection is failing — this is the classic symptom of the **Server Members Intent** being off, since `VoiceChannel.members` can't resolve members without it.

**Admin commands say "You don't have permission":**

- Make sure you have the Discord role matching `ADMIN_ROLE_NAME` in `.env` (default: `Admin`), or that you are the server owner

**`/clear` confirmation times out:**

- You have 60 seconds to react with ✅ after typing `/clear`. If it expires, use `/clear` again.

**"Dice must have at least 1 side" error:**

- Use a positive number: `/dice 6`

**Commands not working:**

- Make sure messages start with `/`
- Use `/help` to see available commands

## Notes

- Amy maintains conversation memory per Discord channel, stored persistently in `amy_memory.db`
- All responses are generated locally using Ollama — no data is sent to external servers
- Adjust `MAX_MEMORY_MESSAGES` in `database.py` to change how many messages are remembered per channel
- Adjust `DB_PRUNE_DAYS` in `.env` to change how long conversation history is retained before automatic pruning
- Messages longer than Discord's 2000-character limit are split across multiple messages automatically — no configuration needed
- Voice is limited to guild voice channels; Discord does not expose DM or group-DM calls to bots
- Amy self-deafens when joining voice (she never needs to receive audio)
- Embeds are used for music and voice replies; chat, `/help`, `/status`, dice and `/rng` stay plain text
- Music requires **FFmpeg**; YouTube/SoundCloud sources additionally require **yt-dlp** (both covered in setup)
- Stream URLs are resolved one track at a time, immediately before playing — YouTube links expire after a few hours, so resolving a long queue up front would leave later tracks pointing at dead links
- At 100% volume audio is passed through as opus with no transcoding; below 100% it decodes to PCM so `PCMVolumeTransformer` can scale it, which costs more CPU
- `/volume` below 100% takes effect from the next track, since a passthrough stream has no volume stage to adjust mid-song
- Adjust `IDLE_DISCONNECT_DELAY`, `MAX_QUEUE_SIZE`, `MAX_PLAYLIST_TRACKS`, `QUEUE_PAGE_SIZE`, or `DEFAULT_VOLUME` in `music.py` to tune playback behaviour
- Playlists are fetched with yt-dlp's `extract_flat`, so a 50-track playlist costs one request rather than fifty; each track's stream URL is still resolved individually just before it plays

---

**Happy chatting with Amy!** 🎩✨
