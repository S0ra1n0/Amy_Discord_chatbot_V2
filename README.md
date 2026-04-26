# Amy Chatbot V2

A sophisticated Discord bot powered by local language models using Ollama. Amy acts as a personal assistant with the demeanor of a professional secretary/maid, ready to chat, answer questions, and execute commands.

## Introduction

Amy is an intelligent personal assistant bot that runs on your Discord server. She provides thoughtful responses with context awareness, remembering recent conversations to provide more relevant answers. Beyond conversation, Amy can execute various commands to perform specific tasks like rolling dice.

**Key Features:**

- Conversational AI powered by Ollama (Qwen 3.5 9B model)
- Conversation memory (remembers last 10 messages per channel)
- Dual command system supporting `/` prefix
- Professional and polite personality
- Easy-to-extend command architecture

## Functions & Commands

### Chat

Simply message Amy naturally - she maintains conversation context and responds thoughtfully.

### Available Commands

| Command            | Description                                                       |
| ------------------ | ----------------------------------------------------------------- |
| `/help`            | Display all available commands                                    |
| `/dice1`           | Roll a single 6-sided dice                                        |
| `/dice2`           | Roll two 6-sided dice (shows individual rolls + total)            |
| `/dice [sides]`    | Roll a custom dice (e.g., `/dice 20` for a 20-sided dice)         |
| `/roll [sides]`    | Alias for the dice command                                        |
| `/rng [min] [max]` | Generate a random number between min and max (e.g., `/rng 0 999`) |

## How It Runs

1. The bot connects to your Discord server using your bot token
2. When a message is received:
   - If it starts with `/`, it's treated as a command and executed
   - Otherwise, it's passed to Amy who responds using the Ollama model
3. All conversations are stored in memory (last 10 messages per channel) for context
4. Responses are sent back to Discord as replies

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

1. Create and setup an application on Discord Developer Portal and the token should be on the "Bot" section to be generated and copied
2. Copy `.env.example` to `.env`
3. Edit `.env` and add your Discord bot token:

```
DISCORD_TOKEN=your_actual_discord_bot_token_here
```

### Step 4: Start Ollama

Make sure Ollama is running and the model is available:

```bash
ollama pull qwen3:1.7b  # Change if you use a different model like: ollama pull qwen2.5:2a
```

Change the model name in Amy_chatbot_V2.py if you're not using model qwen3:1.7b

```
model = "qwen3:1.7b" # Ollama model name (replace with your model name)
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
├── requirements.txt        # Python dependencies
├── .env                    # Environment variables (Git ignored)
├── .env.example            # Template for environment variables
├── .gitignore              # Git ignore rules
└── README.md               # This file
```

## Troubleshooting

**Bot doesn't respond:**

- Ensure Ollama is running (`ollama serve`)
- Check that the model `qwen3:1.7b` is installed
- Verify Discord token is correct in `.env`

**"Dice must have at least 1 side" error:**

- Use a positive number: `/dice 6`

**Commands not working:**

- Make sure messages start with `/`
- Use `/help` to see available commands

## Notes

- Amy maintains conversation memory per Discord channel
- All responses are generated locally using Ollama
- No data is sent to external servers
- Adjust `MAX_MEMORY_MESSAGES` in the code to change memory size

---

More updates comming soon!

**Happy chatting with Amy!** 🎩✨
