# Tests

Plain Python scripts — no pytest, no extra dependencies. Each one prints its own checks and
exits non-zero on failure, so the runner and CI both work off exit codes.

## Running

```bash
python tests/run_tests.py             # offline only (~8s) — safe to run anywhere
python tests/run_tests.py --network   # + suites that reach YouTube and FFmpeg
python tests/run_tests.py --live      # + starts the real bot against Discord
python tests/run_tests.py --all       # everything
```

The runner finds `.venv` automatically and falls back to the current interpreter.
Individual suites can also be run directly: `python tests/test_music.py`.

## Tiers

**Offline** — pure logic, no network, no Discord. These are the ones to run constantly.

| Suite | Covers |
| ----- | ------ |
| `test_music.py` | duration/volume parsing, loop modes, queue advance, `MusicManager` |
| `test_voice.py` | channel-name sanitising, the `decide_join_action` truth table, voice-channel DB |
| `test_ui.py` | every embed builder, Discord's length limits, `PlayerControls` persistence rules |
| `test_queue_mgmt.py` | playlist-URL detection, pagination, remove/shuffle/skipto helpers |
| `test_queue_cmds.py` | queue commands end-to-end through mocked Discord objects |
| `test_voice_cmds.py` | voice command permissions and cooldowns |
| `test_regression.py` | rate limiters, message splitting, `<think>`-tag stripping |
| `test_security.py` | the `MUSIC_DIR` sandbox — path traversal must stay blocked |
| `test_commands_audit.py` | `/dice` caps, removed commands, `/stop` staying connected |
| `test_docs_audit.py` | every command appears in both the help text and the README |

**Network** — needs internet; slower and can fail if YouTube changes.

`test_resolve.py`, `test_search.py`, `test_audio_path.py`

**Live** — needs a valid `DISCORD_TOKEN`; briefly brings the bot online.

`smoke_start.py`

## Why these exist

Several were written *after* a bug reached the running bot, and now stop it recurring:

- `test_security.py` — `/play` could once probe any file on the host, including `.env`
- `test_commands_audit.py` — `/dice 6 1000000000` froze the entire bot for minutes
- `test_queue_mgmt.py` — `/skip` silently did nothing under `loop track`
- `test_ui.py` — a long track title exceeded Discord's limit and rejected the whole message

When fixing a bug, add the check that would have caught it.
