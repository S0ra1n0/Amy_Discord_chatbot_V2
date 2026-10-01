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
Individual suites can also be run directly: `python tests/test_music.py`. Run directly or through the runner, no suite touches your real database: each one uses a throwaway `AMY_DB_PATH`.

## Tiers

**Offline** — pure logic, no network, no Discord. These are the ones to run constantly.

| Suite | Covers |
| ----- | ------ |
| `test_music.py` | duration/volume parsing, loop modes, queue advance, `MusicManager` |
| `test_voice.py` | channel-name sanitising, the `decide_join_action` and `decide_restore_action` truth tables, voice-channel DB |
| `test_ui.py` | every embed builder, Discord's length limits, `PlayerControls` persistence rules |
| `test_queue_mgmt.py` | playlist-URL detection, pagination, remove/shuffle/skipto helpers |
| `test_queue_cmds.py` | queue commands end-to-end through mocked Discord objects |
| `test_voice_cmds.py` | voice command permissions and cooldowns |
| `test_regression.py` | rate limiters, message splitting, `<think>`-tag stripping, history window, model warm-up, settings persistence, the slash error handler |
| `test_security.py` | the `MUSIC_DIR` sandbox — path traversal must stay blocked |
| `test_commands_audit.py` | `/dice` caps, removed commands, `/stop` staying connected |
| `test_slash.py` | the slash-command tree: naming rules, descriptions, bounds, admin gating, 3s defers |
| `test_websearch.py` | result parsing, page-text extraction, domain dedup, recency inference, the tool contract |
| `test_docs_audit.py` | every command appears in both the help text and the README |

**Network** — needs internet; slower and can fail if YouTube changes.

`test_resolve.py`, `test_search.py`, `test_audio_path.py`, `test_model_live.py` (needs
Ollama rather than the internet: it checks that a model's reasoning mode is detected on
switch and that the outgoing model is released), and `test_websearch_live.py` —
a thin shim that re-runs `test_websearch.py` with `--network` so the runner picks up its live
DuckDuckGo section (recency filtering, region pinning, page fetching). The runner invokes each
suite with no arguments, which is why that shim exists rather than a flag.

**Live** — needs a valid `DISCORD_TOKEN`; briefly brings the bot online.

`smoke_start.py`

## Why these exist

Several were written *after* a bug reached the running bot, and now stop it recurring:

- `test_security.py` — `/play` could once probe any file on the host, including `.env`
- `test_commands_audit.py` — `/dice 6 1000000000` froze the entire bot for minutes
- `test_queue_mgmt.py` — `/skip` silently did nothing under `loop track`
- `test_ui.py` — a long track title exceeded Discord's limit and rejected the whole message
- **Every suite that imports the bot** — used to run against the real `amy_memory.db`. The bot reads `/model` and `/toggle` from its database at import time, and one warm-up test could overwrite a saved `/model` choice; the live smoke test ran real startup, which restores saved queues and rejoins voice. Now `run_tests.py` gives each suite a throwaway `AMY_DB_PATH`, every loader calls `_fakes.isolate_db()` before importing, and `assert_isolated_db()` fails the suite if it ever points at the real file.
- `test_regression.py` — a failed model warm-up used to be read as "the saved model was uninstalled": Amy switched to the default and saved that, so Ollama merely not being up at login erased the admin's choice. Now she falls back only when Ollama confirms the model is gone, only in memory, and never over a `/model` made meanwhile.
- `test_queue_cmds.py` — **its own exit gate.** The only `if fails: sys.exit(1)` used to sit
  above the snapshot/restore section, so ~30 checks there could print FAIL while the script
  exited 0 and reported success. Found by review, not by a failing run. Each of the restore
  fixes below was then mutation-checked: undo it, and the suite fails.
- `test_queue_cmds.py` — restore safety: a stale snapshot used to replace a live queue when
  `/play` landed during startup, a re-sent READY restored a second time and queued the
  playing track twice, `/join` never started a quietly restored queue, `/play` said
  "Loading <your song>" while a restored one played, and `/leave` left the snapshot behind
  for up to 20s so a restart could rejoin the channel.
- `test_queue_cmds.py` — the queue-restore resume path. `restore_player` narrows with
  `isinstance(channel, discord.VoiceChannel)`, so a duck-typed fake was invisible to it and
  the most important branch of queue persistence had no test at all. `_fakes.RealVoiceChannel`
  subclasses the real class to get past the narrowing, and the decision itself now lives in
  `voice.decide_restore_action` as a pure truth table.
- `test_model_live.py` — switching models used to hold both in memory at once, which filled
  an 8GB GPU and made one `/model` call take **337 seconds**. It also guards the detection
  that stops a model pasting its own reasoning into replies.
- `test_websearch.py` — two separate regressions in how Amy searches. The tool description
  decides whether she searches at all (a vague one scored 4/7, the explicit one 7/7), and
  adding a second tool parameter dropped correct searches from 12/12 to 7/12. The suite pins
  both the wording cues and the single-parameter shape.

When fixing a bug, add the check that would have caught it.
