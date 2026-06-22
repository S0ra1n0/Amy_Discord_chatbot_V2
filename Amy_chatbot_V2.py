# Amy_chatbot_V2.py

#-------Import Libraries----------
import os
import re
import time
import asyncio
import random
import httpx
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Dict, List, Tuple, Union

from dotenv import load_dotenv
import ollama
import discord

from commands_help import HELP_EVERYONE, HELP_ADMIN
from database import ConversationDB
#----------------------------------

#----Utility Functions------
def safe_print(message: str) -> None:
    """
    Safe print function that handles Unicode encoding errors on Windows.
    Writes directly to stdout buffer to bypass cp1252 encoding.
    """
    try:
        print(message)
    except UnicodeEncodeError:
        import sys
        sys.stdout.buffer.write(message.encode('utf-8', errors='replace'))
        sys.stdout.buffer.write(b'\n')
        sys.stdout.buffer.flush()

def strip_think_tags(text: str) -> str:
    """Remove <think>...</think> reasoning blocks emitted by qwen3 models."""
    return re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL).strip()
#--------------------------------------

#----Setup Discord Bot and Ollama Model------
load_dotenv()
discord_token = os.getenv("DISCORD_TOKEN")
ADMIN_ROLE_NAME: str = os.getenv("ADMIN_ROLE_NAME", "Admin")

if not discord_token:
    safe_print("[ERROR] DISCORD_TOKEN not found in .env file. Please add it and try again.")
    exit(1)

intents = discord.Intents.default()
intents.message_content = True
intents.reactions = True
bot = discord.Client(intents=intents)

model = "qwen3:1.7b"
system_prompt = '''You are Amy, a sophisticated and helpful personal assistant with the demeanor of a professional secretary.

Personality Traits:
- Poised and professional, yet warm and approachable
- Efficient and resourceful in solving problems
- Respectful and courteous in all interactions

Your Capabilities:
- Answer questions accurately and thoughtfully on virtually any topic
- Execute commands when instructed (command details will be provided during the conversation)
- Manage schedules, reminders, and personal tasks

Limitations:
- You should decline requests that are harmful, illegal, or unethical
- Always prioritize security and privacy

Remember: You are here to make your master's life easier, more organized, and more productive. Approach each interaction with dedication and a desire to be helpful.
'''
#----------------------------------------------

#----Database------
db = ConversationDB()
#----------------------------------------------

#----Bot State------
bot_enabled: bool = True
#----------------------------------------------

#----Admin Helper------
def is_admin(msg: discord.Message) -> bool:
    """Returns True if the author is the server owner, has Administrator permission, or has the admin role."""
    if msg.guild is None:
        return False
    if msg.guild.owner_id == msg.author.id:
        return True
    # Resolve to Member to access roles/permissions (msg.author may be a bare User when not cached)
    member = msg.guild.get_member(msg.author.id)
    if member is None:
        return False
    if member.guild_permissions.administrator:
        return True
    return any(role.name == ADMIN_ROLE_NAME for role in member.roles)
#----------------------------------------------

#----Rate Limiting------
rate_limit_store: Dict[int, List[float]] = defaultdict(list)
RATE_LIMIT_MAX: int = 5
RATE_LIMIT_WINDOW: int = 3600  # 1 hour in seconds

def check_rate_limit(user_id: int) -> Tuple[bool, int]:
    """
    Returns (allowed, seconds_until_reset).
    Prunes timestamps outside the window before checking.
    """
    now = time.time()
    rate_limit_store[user_id] = [t for t in rate_limit_store[user_id] if now - t < RATE_LIMIT_WINDOW]
    if len(rate_limit_store[user_id]) >= RATE_LIMIT_MAX:
        oldest = rate_limit_store[user_id][0]
        reset_in = int(RATE_LIMIT_WINDOW - (now - oldest))
        return False, reset_in
    rate_limit_store[user_id].append(now)
    return True, 0
#----------------------------------------------

#----Pending /clear Confirmations------
# Maps confirmation_message_id → (requesting_user_id, channel_id, server_id)
pending_clear: Dict[int, Tuple[int, int, Union[int, str]]] = {}
#----------------------------------------------

#----Chat and Command Functions------
def chat(user_message: str, server_id: Union[int, str], channel_id: int) -> str:
    """Send a message to the chatbot and get a response (blocking, runs in thread)."""
    try:
        safe_print(f"[DEBUG] Chat function called with message: {user_message[:30]}...")

        db.store_message(server_id, channel_id, "user", user_message)
        safe_print("[DEBUG] User message stored in memory")

        history = db.get_messages(server_id, channel_id)
        safe_print(f"[DEBUG] Conversation history retrieved: {len(history)} messages")

        current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        current_day = datetime.now().strftime("%A")
        system_with_time = f"{system_prompt}\n\n[IMPORTANT] Current date and time: {current_time} ({current_day}). Use this information when answering questions about time."
        messages = [{"role": "system", "content": system_with_time}] + history
        safe_print("[DEBUG] Messages prepared for Ollama")

        safe_print("[DEBUG] Calling Ollama.chat()...")
        try:
            response = ollama.chat(model=model, messages=messages)
            safe_print("[DEBUG] Ollama response received")
        except httpx.ConnectError as e:
            safe_print(f"[WARNING] Ollama is unavailable: {e}")
            db.pop_last_message(server_id, channel_id)
            return "I apologize, but I'm currently having trouble connecting to my knowledge system. Please try again in a moment."
        except Exception as e:
            safe_print(f"[ERROR] Ollama call failed: {e}")
            db.pop_last_message(server_id, channel_id)
            raise

        assistant_response = strip_think_tags(response['message']['content'])
        safe_print(f"[DEBUG] Response extracted: {assistant_response[:30]}...")

        if not assistant_response:
            safe_print("[DEBUG] Warning: Ollama returned empty response, using fallback")
            assistant_response = "I apologize, but I'm having difficulty formulating a response at the moment. Could you please rephrase your question?"

        db.store_message(server_id, channel_id, "assistant", assistant_response)
        safe_print("[DEBUG] Assistant response stored in memory")

        return assistant_response
    except UnicodeDecodeError as e:
        safe_print(f"[ERROR] Encoding error in chat: {e}")
        return "I cannot process your message since it has unreadable character(s), please try again with readable text."
    except Exception as e:
        safe_print(f"[ERROR] Unexpected error in chat: {e}")
        raise

async def chat_async(user_message: str, server_id: Union[int, str], channel_id: int) -> str:
    """Async wrapper for chat function to avoid blocking the bot."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, chat, user_message, server_id, channel_id)

async def execute_command(command_text: str, msg: discord.Message) -> str:
    """Execute a command based on the command text."""
    parts = command_text.strip().split()

    if not parts:
        return "Invalid command. Use /help for available commands."

    command = parts[0].lower()

    if command == "help":
        return HELP_EVERYONE + (HELP_ADMIN if is_admin(msg) else "")

    if command == "toggle":
        if not is_admin(msg):
            return "🚫 You don't have permission to use this command. (Admin only)"
        global bot_enabled
        bot_enabled = not bot_enabled
        state = "enabled" if bot_enabled else "disabled"
        safe_print(f"[INFO] Bot is now {state}")
        return f"🤖 Bot is now **{state}**"

    if command == "clear":
        if not is_admin(msg):
            return "🚫 You don't have permission to use this command. (Admin only)"
        server_id = msg.guild.id if msg.guild else "DM"
        channel_id = msg.channel.id
        confirm_msg = await msg.reply("⚠️ This will wipe all conversation memory for this channel. React with ✅ to confirm, or ❌ to cancel.")
        await confirm_msg.add_reaction("✅")
        await confirm_msg.add_reaction("❌")
        pending_clear[confirm_msg.id] = (msg.author.id, channel_id, server_id)

        # Auto-cancel after 60 seconds
        async def timeout_clear(confirm_id: int) -> None:
            await asyncio.sleep(60)
            if confirm_id in pending_clear:
                del pending_clear[confirm_id]
                try:
                    await confirm_msg.edit(content="⚠️ Clear request timed out. Cancelled.")
                    await confirm_msg.clear_reactions()
                except Exception:
                    pass
        asyncio.create_task(timeout_clear(confirm_msg.id))
        return ""  # Reply already sent above

    if command == "dice1":
        roll = random.randint(1, 6)
        return f"🎲 Rolled 1d6: **{roll}**"

    if command == "dice2":
        roll1 = random.randint(1, 6)
        roll2 = random.randint(1, 6)
        total = roll1 + roll2
        return f"🎲 Rolled 2d6: **{roll1}** + **{roll2}** = **{total}**"

    if command == "dice":
        try:
            amount = 1
            sides = 6
            if len(parts) > 1:
                sides = int(parts[1])
                if sides < 1:
                    return "Invalid dice command. Sides must be at least 1."
            if len(parts) > 2:
                amount = int(parts[2])
                if amount < 1:
                    return "Invalid dice command. Amount must be at least 1."
            total = sum(random.randint(1, sides) for _ in range(amount))
            return f"🎲 Rolled {amount} {sides}-sided dice(s): **{total}**"
        except ValueError:
            return "Invalid dice command. Usage: /dice [sides] or /dice [sides] [amount]\nExample: /dice 20 or /dice 6 3"

    if command == "rng":
        try:
            if len(parts) < 3:
                return "Invalid rng command. Usage: `/rng min max`\nExample: `/rng 0 999`"
            n = int(parts[1])
            m = int(parts[2])
            if n > m:
                return "Invalid rng command. Min must be less than or equal to Max."
            result = random.randint(n, m)
            return f"🎲 Random number between {n} and {m}: **{result}**"
        except ValueError:
            return "Invalid rng command. Both arguments must be integers.\nUsage: `/rng min max`\nExample: `/rng 0 999`"

    return f"Unknown command: `{command}`. Use `/help` for available commands."
#--------------------------------------

#----Event Handlers for Discord Bot------
@bot.event
async def on_ready() -> None:
    safe_print(f'{bot.user} is online!')
    activity = discord.Activity(type=discord.ActivityType.watching, name="conversations")
    await bot.change_presence(activity=activity, status=discord.Status.online)
    safe_print("[INFO] Bot status set to: Watching conversations")

@bot.event
async def on_connect() -> None:
    safe_print("[INFO] Bot connected to Discord")

@bot.event
async def on_disconnect() -> None:
    safe_print("[WARNING] Bot disconnected from Discord, attempting to reconnect...")

last_processed_id: Union[int, None] = None

@bot.event
async def on_message(msg: discord.Message) -> None:
    """Process messages from users - execute commands or send to chat."""
    global last_processed_id

    if msg.id == last_processed_id:
        return
    last_processed_id = msg.id

    safe_print(f"[DEBUG] Message received from {msg.author}: {msg.content}")

    if msg.author == bot.user:
        safe_print("[DEBUG] Ignoring bot's own message")
        return

    try:
        server_id = msg.guild.id if msg.guild else "DM"
        channel_id = msg.channel.id
        safe_print(f"[DEBUG] Server ID: {server_id}, Channel ID: {channel_id}")

        if msg.content.startswith("/"):
            safe_print("[DEBUG] Processing as command")
            response = await execute_command(msg.content[1:], msg)
            if response:
                await msg.reply(response)
            safe_print("[DEBUG] Command handled")
            return

        # Check if bot is enabled before processing chat
        if not bot_enabled:
            safe_print("[DEBUG] Bot is disabled, ignoring chat message")
            return

        # Rate limit check (admins are exempt)
        if not is_admin(msg):
            allowed, reset_in = check_rate_limit(msg.author.id)
            if not allowed:
                minutes = reset_in // 60
                seconds = reset_in % 60
                await msg.reply(f"⏳ You've reached the limit of {RATE_LIMIT_MAX} messages per hour. Try again in {minutes}m {seconds}s.")
                return

        safe_print("[DEBUG] Processing as chat, calling chat_async...")
        response = await chat_async(msg.content, server_id, channel_id)
        safe_print(f"[DEBUG] Chat response received: {response[:50]}...")

        safe_print("[DEBUG] Sending reply...")
        await msg.reply(response)
        safe_print("[DEBUG] Reply sent successfully")

    except ConnectionError:
        safe_print("[ERROR] Connection error - Ollama may be unavailable")
        try:
            await msg.reply("I'm currently unable to connect to my knowledge system. Please try again later.")
        except Exception as e:
            safe_print(f"[ERROR] Failed to send error message: {e}")
    except Exception as e:
        import traceback
        safe_print(f"[ERROR] Error processing message: {e}")
        safe_print(traceback.format_exc())
        try:
            await msg.reply("Sorry, I encountered an unexpected error. Please try again.")
        except Exception:
            safe_print("[ERROR] Failed to send error message to Discord")

@bot.event
async def on_reaction_add(reaction: discord.Reaction, user: discord.User) -> None:
    """Handle reactions for /clear confirmation."""
    if user.bot:
        return

    confirm_id = reaction.message.id
    if confirm_id not in pending_clear:
        return

    requesting_user_id, channel_id, server_id = pending_clear[confirm_id]

    # Only the user who issued /clear can confirm
    if user.id != requesting_user_id:
        return

    del pending_clear[confirm_id]

    if str(reaction.emoji) == "✅":
        db.clear_channel(server_id, channel_id)
        await reaction.message.edit(content="🗑️ Conversation memory for this channel has been cleared.")
    else:
        await reaction.message.edit(content="Cancelled.")

    try:
        await reaction.message.clear_reactions()
    except Exception:
        pass
#--------------------------------------

if __name__ == "__main__":
    try:
        safe_print("[INFO] Starting Amy Chatbot...")
        bot.run(discord_token)
    except KeyboardInterrupt:
        safe_print("[INFO] Bot interrupted by user (Ctrl+C)")
    except Exception as e:
        safe_print(f"[ERROR] Fatal error: {e}")
        import traceback
        safe_print(traceback.format_exc())
        exit(1)
