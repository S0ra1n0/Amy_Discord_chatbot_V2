# commands_help.py
"""Help command text for the Amy chatbot"""

HELP_EVERYONE = """**Commands — Everyone:**
`/help` - Show the command list
`/dice [sides] [amount]` - Roll dice (default: one 6-sided)
`/rng [min] [max]` - Random number between min and max
`/websearch [query] [recency]` - Search the web and show the results
  • Optional recency: past day, week, month or year
  • I also search on my own when needed
`/join` - Bring me into the voice channel you're in
`/leave` - Make me leave the voice channel (be in it with me, or admin)

**Music:**
`/play [song, URL, or file]` - Queue a track (I'll join your channel)
`/search [song]` - Show the top 5 matches and pick one
`/pause` / `/resume` - Pause or resume playback
`/skip` - Skip the current track
`/previous` - Go back to the previous track
`/shush` - Stop me talking (the music carries on)
`/seek [position]` - Jump to a spot (`1:30`, `1:02:03` or `90`)
`/replay` - Restart the current track from the beginning
`/stop` - Stop playback and clear the queue (I stay in the channel)
`/queue [page]` - Show what's playing and what's queued
`/remove [position]` - Remove a track you queued
`/skipto [position]` - Jump ahead to a queued track
`/shuffle` - Shuffle the queued tracks
`/nowplaying` - Show the current track
`/lyrics [song]` - Lyrics for the current track, or search one
`/loop [off|track|queue]` - Set repeat mode
`/autoplay [on|off]` - Play related songs when the queue ends
`/filter [preset]` - Bass boost, nightcore, slowed or off"""

HELP_ADMIN = """
**Commands — Admin only:**
`/say [text]` - Make me say something in my voice channel
`/voice [on|off] [up_next]` - My voice in calls on/off, announce next song, or status
`/toggle` - Enable/disable bot responses (remembered across restarts)
`/forget` - Wipe conversation memory for this channel
`/status` - Show bot, Ollama and voice status
`/model [name]` - Show or switch the model (remembered)
`/create [name]` - Create a voice channel and join it
`/volume [0-100]` - Show or set playback volume
`/clearqueue` - Empty the queue (current track keeps playing)"""
