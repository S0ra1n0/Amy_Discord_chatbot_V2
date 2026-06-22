# Amy Chatbot V2

A sophisticated Discord bot powered by local language models using Ollama. Amy acts as a personal assistant with the demeanor of a professional secretary, ready to chat, answer questions, and execute commands.

## Introduction

Amy is an intelligent personal assistant bot that runs on your Discord server. She provides thoughtful responses with context awareness, remembering recent conversations to provide more relevant answers. Beyond conversation, Amy can execute various commands to perform specific tasks (full command list in 'Available Commands' section).

**Key Features:**

- Conversational AI powered by Ollama
- Persistent conversation memory stored in SQLite (survives restarts, remembers last 10 messages per channel)
- Admin access control — admin commands restricted to server owner or users with the `Admin` role
- Rate limiting — non-admin users are capped at 5 chat messages per hour
- Dual command system supporting `/` prefix
- Easy-to-extend command architecture

## Functions & Commands

### Chat

Simply message Amy naturally — she maintains conversation context and responds thoughtfully.

> **Note:** Non-admin users are limited to **5 messages per hour**. Admins (server owner or `Admin` role) have no limit.

### Available Commands

| Command                  | Description                                                               | Access     |
| ------------------------ | ------------------------------------------------------------------------- | ---------- |
| `/help`                  | Display all available commands                                            | Everyone   |
| `/toggle`                | Enable/disable bot responses to chat (commands still work)                | Admin only |
| `/clear`                 | Wipe conversation memory for the current channel (asks for confirmation)  | Admin only |
| `/dice1`                 | Roll a single 6-sided dice                                                | Everyone   |
| `/dice2`                 | Roll two 6-sided dice (shows individual rolls + total)                    | Everyone   |
| `/dice [sides]`          | Roll a custom dice (e.g., `/dice 20` for a 20-sided dice)                 | Everyone   |
| `/dice [sides] [amount]` | Roll multiple custom dice (e.g., `/dice 6 3` for three 6-sided dice)      | Everyone   |
| `/rng [min] [max]`       | Generate a random number between min and max (e.g., `/rng 0 999`)         | Everyone   |

### Admin Access

A user is considered an admin if they meet **any** of the following:
- They are the **server owner**, or
- They have the Discord **Administrator** permission, or
- They have the Discord role named **`Admin`** (configurable via `ADMIN_ROLE_NAME` in `.env`)

> **Note:** `/help` shows admin commands only to users who qualify as admins. Regular users see only the commands available to them.

## How It Runs

1. The bot connects to your Discord server using your bot token
2. When a message is received:
   - If it starts with `/`, it's treated as a command and executed
   - Otherwise, it's passed to Amy who responds using the Ollama model (if the bot is enabled)
3. All conversations are saved to a local SQLite database (`amy_memory.db`) — history persists across restarts
4. Non-admin users are rate-limited to 5 chat messages per hour; excess messages receive a cooldown reply
5. Responses are sent back to Discord as replies
6. Use `/toggle` (Admin only) to enable/disable bot chat responses without shutting down the bot
7. Use `/clear` (Admin only) to wipe conversation memory for a channel — Amy will ask for emoji confirmation first

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

### Step 2: Install Dependencies

```bash
pip install -r requirements.txt
```

### Step 3: Configure Environment Variables

1. Create an application on the Discord Developer Portal and copy your bot token from the "Bot" section
2. Copy `.env.example` to `.env`
3. Edit `.env` and fill in your values:

```
DISCORD_TOKEN=your_actual_discord_bot_token_here
ADMIN_ROLE_NAME=Admin
```

`ADMIN_ROLE_NAME` is the name of the Discord role that grants admin access to bot commands. It defaults to `Admin` if not set.

### Step 4: Start Ollama

Make sure Ollama is running and the model is available:

```bash
ollama pull qwen3:1.7b  # Change if you use a different model
```

Change the model name in `Amy_chatbot_V2.py` if you are not using `qwen3:1.7b`:

```python
model = "qwen3:1.7b"  # Ollama model name (replace with your model name)
```

### Step 5: Run the Bot

```bash
python Amy_chatbot_V2.py
```

The bot should now be online and ready to respond in your Discord server!

## Project Structure

```
Amy_chatbot_V2/
├── Amy_chatbot_V2.py       # Main bot file
├── database.py             # SQLite conversation memory layer
├── commands_help.py        # Help command text
├── requirements.txt        # Python dependencies
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

---

**Happy chatting with Amy!** 🎩✨
