# Amy Chatbot V2

A sophisticated Discord bot powered by local language models using Ollama. Amy acts as a personal assistant with the demeanor of a professional secretary, ready to chat, answer questions, and execute commands.

## Introduction

Amy is an intelligent personal assistant bot that runs on your Discord server. She provides thoughtful responses with context awareness, remembering recent conversations to provide more relevant answers. Beyond conversation, Amy can execute various commands to perform specific tasks (full command list in 'Available Commands' section).

**Key Features:**

- Conversational AI powered by Ollama with **real-time streaming responses**
- **Voice channel support** — summon Amy into your voice channel, or have her create one (groundwork for music playback)
- Runtime model switching — swap the active Ollama model without restarting the bot
- Long responses/replies automatically split across multiple Discord messages instead of being truncated
- Persistent conversation memory stored in SQLite (survives restarts, remembers last 10 messages per channel)
- Automatic daily pruning of old conversation history
- Admin access control — admin commands restricted to server owner or users with the `Admin` role
- Rate limiting — non-admin users are capped at 5 chat messages per hour, and 3 voice commands per minute
- Simple `/`-prefixed command system, easy to extend

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
| `/clear`                 | Wipe conversation memory for the current channel (asks for confirmation)  | Admin only |
| `/dice1`                 | Roll a single 6-sided dice                                                | Everyone   |
| `/dice2`                 | Roll two 6-sided dice (shows individual rolls + total)                    | Everyone   |
| `/dice [sides]`          | Roll a custom dice (e.g., `/dice 20` for a 20-sided dice)                 | Everyone   |
| `/dice [sides] [amount]` | Roll multiple custom dice (e.g., `/dice 6 3` for three 6-sided dice)      | Everyone   |
| `/rng [min] [max]`       | Generate a random number between min and max (e.g., `/rng 0 999`)         | Everyone   |
| `/join`                  | Bring Amy into the voice channel you're currently in                      | Everyone   |
| `/leave`                 | Make Amy leave her voice channel                                          | In-channel or admin |
| `/create [name]`         | Create a new voice channel and have Amy join it (e.g., `/create Music Room`) | Admin only |

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

Voice commands are limited to **3 per minute** per non-admin user, so they can't be used to make Amy flap between channels.

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
DB_PRUNE_DAYS=30
```

- `ADMIN_ROLE_NAME` is the name of the Discord role that grants admin access to bot commands. It defaults to `Admin` if not set.
- `DB_PRUNE_DAYS` controls how many days of conversation history are kept before automatic pruning removes them. Defaults to `30` if not set.

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

### Step 6: Start Ollama

Make sure Ollama is running and the model is available:

```bash
ollama pull qwen3.5:2b  # Change if you use a different model
```

Change the default model name in `Amy_chatbot_V2.py` if you are not using `qwen3.5:2b`:

```python
model = "qwen3.5:2b"  # Ollama model name (replace with your model name)
```

> This sets the model used at startup. Admins can switch to any other installed model at runtime with `/model [name]` without restarting the bot.

### Step 7: Run the Bot

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
├── commands_help.py        # Help command text
├── requirements.txt        # Pinned Python dependencies
├── amy_memory.db           # SQLite conversation store, auto-created (Git ignored)
├── .venv/                  # Virtual environment (Git ignored)
├── .vscode/                # Editor settings, points at .venv (Git ignored)
├── .env                    # Environment variables (Git ignored)
├── .env.example            # Template for environment variables
├── .gitignore              # Git ignore rules
└── README.md               # This file
```

## Troubleshooting

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
- Music playback is not implemented yet — it will additionally require **FFmpeg** on your PATH

---

**Happy chatting with Amy!** 🎩✨
