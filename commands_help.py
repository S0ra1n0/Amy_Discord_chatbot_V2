# commands_help.py
"""Help command text for the Amy chatbot"""

HELP_EVERYONE = """**Commands — Everyone:**
`/help` - Display command list and usage
`/dice [sides] [amount]` - Roll dice (defaults to one 6-sided)
  • Usage: `/dice`, `/dice 20`, or `/dice 6 3`
`/rng [min] [max]` - Generate a random number between min and max
  • Usage: `/rng 0 999`
`/websearch [query] [recency]` - Search the web and show the results
  • Optional recency: past day, week, month or year
  • I also search on my own when a question needs current info
`/join` - Bring me into the voice channel you're in
`/leave` - Make me leave the voice channel (must be in it with me, or an admin)

**Music:**
`/play [song, URL, or file]` - Queue a track (I'll join your channel)
`/search [song]` - Show the top 5 matches and pick one
`/pause` / `/resume` - Pause or resume playback
`/skip` - Skip the current track
`/seek [position]` - Jump to a spot (`1:30`, `1:02:03` or `90`)
`/replay` - Restart the current track from the beginning
`/stop` - Stop playback and clear the queue (I stay in the channel)
`/queue [page]` - Show what's playing and what's queued
`/remove [position]` - Remove a track you queued
`/skipto [position]` - Jump ahead to a queued track
`/shuffle` - Shuffle the queued tracks
`/nowplaying` - Show the current track
`/loop [off|track|queue]` - Set repeat mode"""

HELP_ADMIN = """
**Commands — Admin only:**
`/toggle` - Enable/disable bot responses (remembered across restarts)
`/forget` - Wipe conversation memory for this channel
`/status` - Show bot state, Ollama connectivity, memory usage and rate limit info
`/model` - Show the current Ollama model
`/model [name]` - Switch to another installed model (remembered)
`/create [name]` - Create a new voice channel and join it
`/volume [0-100]` - Show or set playback volume
`/clearqueue` - Empty the queue (current track keeps playing)"""
