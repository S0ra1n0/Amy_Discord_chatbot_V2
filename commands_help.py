# commands_help.py
"""Help command text for the Amy chatbot"""

HELP_EVERYONE = """**Commands — Everyone:**
`/help` - Display command list and usage
`/dice1` - Roll a single 6-sided dice
`/dice2` - Roll two 6-sided dice
`/dice [sides]` - Roll a custom dice
  • Usage: `/dice 20`
`/dice [sides] [amount]` - Roll multiple custom dice
  • Usage: `/dice 6 3` (rolls 3 six-sided dice)
`/rng [min] [max]` - Generate a random number between min and max
  • Usage: `/rng 0 999`
`/join` - Bring me into the voice channel you're in
`/leave` - Make me leave the voice channel (must be in it with me, or an admin)

**Music:**
`/play [song, URL, or file]` - Queue a track (I'll join your channel)
  • Usage: `/play never gonna give you up`
`/pause` / `/resume` - Pause or resume playback
`/skip` - Skip the current track
`/stop` - Stop, clear the queue, and leave
`/queue` - Show what's playing and what's next
`/nowplaying` - Show the current track (alias: `/np`)
`/loop [off|track|queue]` - Set repeat mode"""

HELP_ADMIN = """
**Commands — Admin only:**
`/toggle` - Enable/disable bot responses
`/clear` - Wipe conversation memory for this channel
`/status` - Show bot state, Ollama connectivity, memory usage and rate limit info
`/model` - Show the current Ollama model
`/model [name]` - Switch to a different installed Ollama model
  • Usage: `/model qwen2.5:3b`
`/create [name]` - Create a new voice channel and join it
  • Usage: `/create Music Room`
`/volume [0-100]` - Show or set playback volume"""
