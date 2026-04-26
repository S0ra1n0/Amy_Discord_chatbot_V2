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
#----------------------------------

#----Setup Discord Bot and Ollama Model------
load_dotenv()
discord_token = os.getenv("DISCORD_TOKEN")

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
    print(f"[DEBUG] Chat function called with message: {user_message[:30]}...")
    
    # Add user message to memory
    store_message(server_id, channel_id, "user", user_message)
    print("[DEBUG] User message stored in memory")
    
    # Get conversation history
    history = get_message(server_id, channel_id)
    print(f"[DEBUG] Conversation history retrieved: {len(history)} messages")
    
    # Prepare messages with system prompt at the beginning
    messages = [{"role": "system", "content": system_prompt}] + history
    print("[DEBUG] Messages prepared for Ollama")
    
    # Call Ollama with conversation history (this is a blocking call)
    print("[DEBUG] Calling Ollama.chat()...")
    try:
        response = ollama.chat(
            model=model,
            messages=messages
        )
        print("[DEBUG] Ollama response received")
    except Exception as e:
        print(f"[ERROR] Ollama call failed: {e}")
        raise
    
    # Extract response content
    assistant_response = response['message']['content'].strip()
    print(f"[DEBUG] Response extracted: {assistant_response[:30]}...")
    
    # Handle empty responses
    if not assistant_response:
        print("[DEBUG] Warning: Ollama returned empty response, using fallback")
        assistant_response = "I apologize, but I'm having difficulty formulating a response at the moment. Could you please rephrase your question?"
    
    # Add assistant response to memory
    store_message(server_id, channel_id, "assistant", assistant_response)
    print("[DEBUG] Assistant response stored in memory")
    
    return assistant_response

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
    if command == "dice" or command == "roll":
        try:
            sides = 6  # Default 6-sided dice
            if len(parts) > 1:
                sides = int(parts[1])
            
            if sides < 1:
                return "Dice must have at least 1 side."
            
            roll = random.randint(1, sides)
            return f"🎲 Rolled a {sides}-sided dice: **{roll}**"
        except ValueError:
            return "Invalid dice command. Usage: `/dice [sides]` (default: 6)\nExample: `/dice 20`"
    
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
    print(f'{bot.user} is online!')

# When a message is received, process it and respond using the Ollama model or execute commands
last_processed_id = None

@bot.event
async def on_message(msg):
    global last_processed_id
    
    # Prevent processing the exact same message twice in rapid succession
    if msg.id == last_processed_id:
        return
    last_processed_id = msg.id
    
    print(f"[DEBUG] Message received from {msg.author}: {msg.content}")
    
    if msg.author == bot.user:
        print("[DEBUG] Ignoring bot's own message")
        return
    
    try:
        # Get server ID (guild ID) and channel ID
        server_id = msg.guild.id if msg.guild else "DM"
        channel_id = msg.channel.id
        print(f"[DEBUG] Server ID: {server_id}, Channel ID: {channel_id}")
        
        # Check if message is a command (starts with /)
        if msg.content.startswith("/"):
            print("[DEBUG] Processing as command")
            response = execute_command(msg.content[1:])
        else:
            # Regular chat with memory (run asynchronously to avoid blocking)
            print("[DEBUG] Processing as chat, calling chat_async...")
            response = await chat_async(msg.content, server_id, channel_id)
            print(f"[DEBUG] Chat response received: {response[:50]}...")
        
        print(f"[DEBUG] Sending reply...")
        await msg.reply(response)
        print("[DEBUG] Reply sent successfully")
    except Exception as e:
        import traceback
        print(f"[ERROR] Error processing message: {e}")
        print(traceback.format_exc())
        try:
            await msg.reply(f"Sorry, I encountered an error: {str(e)[:100]}")
        except:
            print("[ERROR] Failed to send error message to Discord")
#--------------------------------------

# Run the bot with the Discord token
bot.run(discord_token)
