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
  • Usage: `/rng 0 999`"""

HELP_ADMIN = """
**Commands — Admin only:**
`/toggle` - Enable/disable bot responses
`/clear` - Wipe conversation memory for this channel
`/status` - Show bot state, Ollama connectivity, memory usage and rate limit info"""
