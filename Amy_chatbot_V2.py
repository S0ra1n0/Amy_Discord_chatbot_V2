# Amy_chatbot_V2.py

#-------Import Libraries----------
import os
from dotenv import load_dotenv
import ollama
import discord
import random
import asyncio
from concurrent.futures import ThreadPoolExecutor
from commands_help import HELP_COMMAND
from datetime import datetime
#----------------------------------

#----Utility Functions------
def safe_print(message):
    """
    Safe print function that handles Unicode encoding errors on Windows.
    Writes directly to stdout buffer to bypass cp1252 encoding.
    """
    try:
        print(message)
    except UnicodeEncodeError:
        import sys
        # Write directly to stdout buffer with UTF-8 encoding to bypass console codec
        sys.stdout.buffer.write(message.encode('utf-8', errors='replace'))
        sys.stdout.buffer.write(b'\n')
        sys.stdout.buffer.flush()

def validate_input(user_message):
    """
    Validate user input for truly unreadable content.
    Returns (is_valid, error_message) tuple.
    """
    # Accept everything - handle encoding errors during processing instead
    return True, None
#--------------------------------------

#----Setup Discord Bot and Ollama Model------
load_dotenv()
discord_token = os.getenv("DISCORD_TOKEN")

# Validate that required credentials are present
if not discord_token:
    safe_print("[ERROR] DISCORD_TOKEN not found in .env file. Please add it and try again.")
    exit(1)

intents = discord.Intents.default()
intents.message_content = True
bot = discord.Client(intents=intents)

model = "qwen3:1.7b" # Ollama model name (replace with your model name)
system_prompt = '''You are Amy, a sophisticated and helpful personal assistant with the demeanor of a professional secretary/maid.

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

#----Conversation Memory System------
conversation_memory = {}
MAX_MEMORY_MESSAGES = 10  # Store last 10 messages (approximately 5 exchanges)

def store_message(server, channel, role, content):
    if server not in conversation_memory:
        conversation_memory[server] = {}
    if channel not in conversation_memory[server]:
        conversation_memory[server][channel] = []
    
    # Append new message
    conversation_memory[server][channel].append({"role": role, "content": content})
    
    # Keep only the last MAX_MEMORY_MESSAGES messages
    if len(conversation_memory[server][channel]) > MAX_MEMORY_MESSAGES:
        conversation_memory[server][channel] = conversation_memory[server][channel][-MAX_MEMORY_MESSAGES:]

def get_message(server, channel):
    if server in conversation_memory and channel in conversation_memory[server]:
        return conversation_memory[server][channel]
    return []  # Return empty list if channel doesn't exist yet
#--------------------------------------

#----Chat and Command Functions------
def chat(user_message, server_id, channel_id):
    """Send a message to the chatbot and get a response (blocking, runs in thread)"""
    try:
        safe_print(f"[DEBUG] Chat function called with message: {user_message[:30]}...")
        
        # Add user message to memory
        store_message(server_id, channel_id, "user", user_message)
        safe_print("[DEBUG] User message stored in memory")
        
        # Get conversation history
        history = get_message(server_id, channel_id)
        safe_print(f"[DEBUG] Conversation history retrieved: {len(history)} messages")
        
        # Get current timestamp for bot context
        current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        current_day = datetime.now().strftime("%A")
        
        # Prepare messages with system prompt and current time at the beginning
        system_with_time = f"{system_prompt}\n\n[IMPORTANT] Current date and time: {current_time} ({current_day}). Use this information when answering questions about time."
        messages = [{"role": "system", "content": system_with_time}] + history
        safe_print("[DEBUG] Messages prepared for Ollama")
        
        # Call Ollama with conversation history (this is a blocking call)
        safe_print("[DEBUG] Calling Ollama.chat()...")
        try:
            response = ollama.chat(
                model=model,
                messages=messages
            )
            safe_print("[DEBUG] Ollama response received")
        except Exception as e:
            safe_print(f"[ERROR] Ollama call failed: {e}")
            raise
        
        # Extract response content
        assistant_response = response['message']['content'].strip()
        safe_print(f"[DEBUG] Response extracted: {assistant_response[:30]}...")
        
        # Handle empty responses
        if not assistant_response:
            safe_print("[DEBUG] Warning: Ollama returned empty response, using fallback")
            assistant_response = "I apologize, but I'm having difficulty formulating a response at the moment. Could you please rephrase your question?"
        
        # Add assistant response to memory
        store_message(server_id, channel_id, "assistant", assistant_response)
        safe_print("[DEBUG] Assistant response stored in memory")
        
        return assistant_response
    except UnicodeDecodeError as e:
        safe_print(f"[ERROR] Encoding error in chat: {e}")
        return "I cannot process your message since it has unreadable character(s), please try again with readable text."
    except Exception as e:
        safe_print(f"[ERROR] Unexpected error in chat: {e}")
        raise

async def chat_async(user_message, server_id, channel_id):
    """Async wrapper for chat function to avoid blocking the bot"""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, chat, user_message, server_id, channel_id)

def execute_command(command_text):
    """Execute a command based on the command text"""
    parts = command_text.strip().split()
    
    if not parts:
        return "Invalid command. Use /help for available commands."
    
    command = parts[0].lower()
    
    # Help command
    if command == "help":
        return HELP_COMMAND # This is imported from commands_help.py
    
    # Dice1 command - single 6-sided dice
    if command == "dice1":
        roll = random.randint(1, 6)
        return f"🎲 Rolled 1d6: **{roll}**"
    
    # Dice2 command - two 6-sided dice
    if command == "dice2":
        roll1 = random.randint(1, 6)
        roll2 = random.randint(1, 6)
        total = roll1 + roll2
        return f"🎲 Rolled 2d6: **{roll1}** + **{roll2}** = **{total}**"
    
    # Dice roll command with custom sides
    if command == "dice":
        try:
            amount = 1  # Default to rolling 1 dice
            sides = 6  # Default 6-sided dice
            if len(parts) > 1:
                sides = int(parts[1])
                if sides < 1:
                    return "Invalid dice command. Sides must be at least 1."
            
            if len(parts) > 2:
                amount = int(parts[2])
                if amount < 1:
                    return "Invalid dice command. Amount must be at least 1."
            
            sum = 0
            for _ in range(0, amount):
                roll = random.randint(1, sides)
                sum += roll
            return f"🎲 Rolled {amount} {sides}-sided dice(s): **{sum}**"
        except ValueError:
            return "Invalid dice command. Usage: /dice [sides] or /dice [sides] [amount]\nExample: /dice 20 or /dice 6 3"

    # Random number generator command
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
# When the bot is ready, print a message to the console
@bot.event
async def on_ready():
    safe_print(f'{bot.user} is online!')

# Connection status tracking
@bot.event
async def on_connect():
    safe_print("[INFO] Bot connected to Discord")

@bot.event
async def on_disconnect():
    safe_print("[WARNING] Bot disconnected from Discord, attempting to reconnect...")

# When a message is received, process it and respond using the Ollama model or execute commands
last_processed_id = None

@bot.event
async def on_message(msg):
    global last_processed_id
    
    # Prevent processing the exact same message twice in rapid succession
    if msg.id == last_processed_id:
        return
    last_processed_id = msg.id
    
    safe_print(f"[DEBUG] Message received from {msg.author}: {msg.content}")
    
    if msg.author == bot.user:
        safe_print("[DEBUG] Ignoring bot's own message")
        return
    
    try:
        # Get server ID (guild ID) and channel ID
        server_id = msg.guild.id if msg.guild else "DM"
        channel_id = msg.channel.id
        safe_print(f"[DEBUG] Server ID: {server_id}, Channel ID: {channel_id}")
        
        # Check if message is a command (starts with /)
        if msg.content.startswith("/"):
            safe_print("[DEBUG] Processing as command")
            response = execute_command(msg.content[1:])
        else:
            # Regular chat with memory (run asynchronously to avoid blocking)
            safe_print("[DEBUG] Processing as chat, calling chat_async...")
            response = await chat_async(msg.content, server_id, channel_id)
            safe_print(f"[DEBUG] Chat response received: {response[:50]}...")
        
        safe_print(f"[DEBUG] Sending reply...")
        await msg.reply(response)
        safe_print("[DEBUG] Reply sent successfully")
    except Exception as e:
        import traceback
        safe_print(f"[ERROR] Error processing message: {e}")
        safe_print(traceback.format_exc())
        try:
            await msg.reply(f"Sorry, I encountered an error: {str(e)[:100]}")
        except:
            safe_print("[ERROR] Failed to send error message to Discord")
#--------------------------------------

# Run the bot with the Discord token
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
