# commands_help.py
"""Help command text for the Amy chatbot"""

HELP_EVERYONE = """**Commands — Everyone:**
`/help` - Display command list and usage
`/dice [sides] [amount]` - Roll dice (defaults to one 6-sided)
  • Usage: `/dice`, `/dice 20`, or `/dice 6 3`
`/rng [min] [max]` - Generate a random number between min and max
  • Usage: `/rng 0 999`
`/join` - Bring me into the voice channel you're in
`/leave` - Make me leave the voice channel (must be in it with me, or an admin)

**Music:**
`/play [song, URL, or file]` - Queue a track (I'll join your channel)
  • Usage: `/play never gonna give you up`
`/search [song]` - Show the top 5 matches and pick one
  • Usage: `/search never gonna give you up`
`/pause` / `/resume` - Pause or resume playback
`/skip` - Skip the current track
`/stop` - Stop playback and clear the queue (I stay in the channel)
`/queue [page]` - Show what's playing and what's queued
  • Usage: `/queue 2` for the next page
`/remove [position]` - Remove a track you queued
`/skipto [position]` - Jump ahead to a queued track
`/shuffle` - Shuffle the queued tracks
`/nowplaying` - Show the current track (alias: `/np`)
`/loop [off|track|queue]` - Set repeat mode"""

HELP_ADMIN = """
**Commands — Admin only:**
`/toggle` - Enable/disable bot responses
`/forget` - Wipe conversation memory for this channel
`/status` - Show bot state, Ollama connectivity, memory usage and rate limit info
`/model` - Show the current Ollama model
`/model [name]` - Switch to a different installed Ollama model
  • Usage: `/model qwen2.5:3b`
`/create [name]` - Create a new voice channel and join it
  • Usage: `/create Music Room`
`/volume [0-100]` - Show or set playback volume
`/clearqueue` - Empty the queue (current track keeps playing)"""
