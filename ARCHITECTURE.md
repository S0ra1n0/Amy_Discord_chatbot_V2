# Architecture

How Amy is put together, and the rules the code depends on. Most rules here are here because
breaking one caused a real bug, so each comes with the reason. [README.md](README.md) covers
setup and usage; [tests/README.md](tests/README.md) covers the test suites and which bugs each
one guards against.

**Changing something in this list?** Update the rule here in the same commit.

## Modules

| Module | Role | Imports |
|---|---|---|
| `Amy_chatbot_V2.py` | Discord client, slash commands, event handlers, the glue between modules | everything |
| `music.py` | Track resolution (yt-dlp), queue, `GuildPlayer`, playback and seeking | discord, yt-dlp |
| `voice.py` | Voice connections and the join/restore decision tables | discord |
| `ui.py` | Embeds, progress bar, the `Reply` wrapper | discord |
| `websearch.py` | DuckDuckGo search, page fetching, the `web_search` tool definition | httpx |
| `llm.py` | Reasoning modes, leak detection, probe judging, startup model choice, system prompt | **nothing external** |
| `config.py` | Parsing settings: a bad value warns and falls back instead of crashing | **nothing external** |
| `models.py` | `ModelManager`: the active model, `/model` switching and probing, warm-up, the startup check | ollama, database, llm |
| `logs.py` | Logging setup: console plus a rotating file | **nothing external** |
| `mixer.py` | `SpeechQueue` and `Mixer`: Amy's voice mixed over the music, with ducking and the hold-music mode | discord |
| `speech.py` | Amy's voice: text clean-up, English check, sentence splitting, voice recipes, audio framing, the swappable engine and its `Speaker` | Kokoro/PyTorch **only when loaded** |
| `database.py` | SQLite: history, settings, voice channels, queue snapshots, schema migrations | sqlite3 |
| `commands_help.py` | `/help` text | — |

**New logic goes in a module as a pure helper**, then gets wired into `Amy_chatbot_V2.py`.
That's why the offline suites can cover queue logic, permissions and model decisions with no
Discord connection. `llm.py` must never import Discord or Ollama (`test_llm.py` enforces it).

**Decisions that are hard to reach go in a pure function.** `voice.decide_join_action` and
`voice.decide_restore_action` take plain values; the caller resolves the Discord objects. The
whole truth table is then tested offline. Try this before writing "needs manual testing".

## Speech

- **Kokoro and PyTorch are imported only inside `KokoroEngine.load`**, via `importlib`. Importing
  the bot - even with `TTS=on` - must never pull them in: they cost ~9 s and 1.1 GB, CI
  doesn't install them, and pyright there can't resolve them. `test_speech.py` and
  `test_config.py` both check this.
- **The model is pinned and local.** `KOKORO_REVISION` is fetched once by `python speech.py
  download` into `kokoro_model/`; `load` passes local paths and sets `HF_HUB_OFFLINE`.
  Left alone, Kokoro checks Hugging Face on every start and follows the repo's latest files.
- **The spaCy model is checked, never installed.** If `en_core_web_sm` is missing, Kokoro
  runs `pip install` by itself at runtime; `load` refuses first with a clear message.
- **One thread does all speech work.** `Speaker` runs load and every synthesis on a single
  dedicated worker: synthesis takes about a second of CPU and must never block the event
  loop, and Kokoro isn't documented as safe from two threads at once.
- **A voice failure never stops the bot.** `Speaker.start` catches everything, sets
  `state = "failed"` with the reason in `problem`, and Amy stays text-only.
- **Blends are weighted here**, not by Kokoro: its own `"a,b"` syntax only averages
  equally. `AMY_VOICE` weights are normalised to sum to 1.
- **Speech audio is normalised to `PEAK_TARGET`** before it leaves `speech.py`, leaving
  headroom for mixing over music (audio adds saturate rather than wrap).
- **Speech text is logged at DEBUG only**, like chat, so it never reaches `amy.log`.
- **With TTS on, every track plays through a `mixer.Mixer`** (wrapped in `music._start`).
  Quiet, it passes the music through untouched - still opus, no CPU cost. With speech
  queued it decodes, fades the music to `DUCK_LEVEL` over 200 ms, adds the voice, and fades
  back. With TTS off nothing is wrapped and playback is exactly as before.
- **`Mixer.read()` never raises** - discord.py treats an exception as the end of the song
  and the queue advances. After one failure it plays the rest of that song unmixed: a
  repeating fault would otherwise lose the packet read in each failed attempt and play
  every other one. **It returns `b""` only when music and speech are both finished.**
- **The first two packets of every passed-through song are Ogg headers** (`OpusHead`,
  `OpusTags`); decoding them raises `OpusError`, and the Mixer skips them. A song starting
  while Amy is mid-sentence used to be the trigger: unhandled, one `/skip` while she talked
  would have ended every song in the queue in turn. Decoded audio is re-sliced into exact
  20 ms frames, so music in 40 or 60 ms packets mixes too.
- **The speech queue lives on the player** (`GuildPlayer.speech`), not on the Mixer, so a
  sentence carries on across `/skip`, `/seek` and `/previous`. `stop_all` clears it.
- **"Is something playing" now has two meanings.** With nothing loaded, Amy speaks through a
  speech-only Mixer and `player.speech_only` is set. Code asking whether a TRACK is loaded
  uses `music.music_active(vc, player)`, never `vc.is_playing()`; code asking whether the
  music is paused uses `music.music_paused(vc)`. Pausing and resuming go through
  `music.pause_music` / `resume_music`. When a track starts during speech-only playback,
  `music._start` stops that source and the remaining speech plays over the music.
- **Speaking over paused music holds the track.** `vc.pause()` silences everything,
  speech included, so `ensure_speaking` sets `Mixer.hold_music` and resumes: the music
  isn't read (it doesn't move on, and its clock stays paused), only speech plays, then
  the Mixer calls `on_hold_done` from the audio thread and the bot pauses again. Until
  then the Mixer sends silence, never an empty read.
- **Synthesis never runs inside an interaction's 3-second window.** `start_saying`
  validates and returns; `_speak_sentences` runs in the background via `music.spawn`.
- **`/volume` reaches the volume stage through the Mixer** (`mixer.music_source`).

## Logging

- **Log with `logging.getLogger("amy.<area>")`, never `print`.** `logs.setup_logging` sends
  every record to the console and to the rotating `AMY_LOG_FILE`. Anything printed
  bypasses the file, which is the whole point of having one.
- **DEBUG never reaches the file.** Those lines carry the full text of chat messages; a
  live console is fine, a file that accumulates for weeks is not.
- **The console format `[LEVEL] message` is load-bearing**: `smoke_start.py` waits for
  "is online!" and `test_config.py` reads `[WARNING]`/`[ERROR]` lines.
- `bot.run(..., log_handler=None)` stops discord.py installing its own handler; its
  records reach ours. Chatty libraries are capped in `logs.QUIET` (discord.py at DEBUG logs
  raw gateway payloads, httpx at INFO logs every request).
- Exceptions go through `log.exception` or `exc_info=`, so the traceback lands in the file.

## Settings

- **Nothing reads a setting at import in a module the bot imports before `load_dotenv()`.**
  `database.py` once read `HISTORY_LIMIT` at import, about 60 lines before `.env` loaded, so
  the `.env` value was silently ignored for its whole life. Settings are parsed in the bot,
  after `load_dotenv`, with `config.parse_*`, and passed in. `database.py` reads no
  environment variables at all. `test_config.py` imports the bot in a fresh process per
  scenario to catch a regression.
- **A bad value warns and falls back**, never crashes and never silently flips. A setting in
  `.env` that the system environment overrides is reported by name only, never by value,
  because values can be secrets (`DISCORD_TOKEN`).
- **History depth has one knob.** `HISTORY_LIMIT` (default 10, minimum 2) goes to
  `ConversationDB(max_messages=)`, which trims storage, and to `get_messages(limit=)`.
  `test_regression.py` asserts the two agree.
- `/toggle` and `/model` persist in the `settings` table. They used to reset on restart.

## Database

- **Schema changes are migrations.** The version lives in SQLite's `PRAGMA user_version`;
  `database.MIGRATIONS[i]` upgrades version *i* to *i+1*. To change the schema, **append** a
  migration and add its hash to `SHIPPED` in `tests/test_schema.py`. Never edit one that
  has shipped: operators' databases have already run it, so an edit forks their schema from
  a fresh install's. The test fails if a shipped migration's text changes.
- Each migration runs in one transaction with its version bump, so a failure leaves the
  database exactly as it was. A database at a **newer** version than the code is refused
  (`SchemaTooNewError`) rather than read and written by code that doesn't know its shape; the
  bot prints one `[ERROR]` line and exits. `ConversationDB` closes its connection when opening
  fails, or Windows keeps the file locked.
- Migration 1 is the schema as it stood before versioning, written with `IF NOT EXISTS`, so
  an existing unversioned `amy_memory.db` passes through it untouched and is just stamped.
- **Queue snapshots are whole, never appended.** `save_player_state` deletes and rewrites a
  guild's `saved_queue` rows in one transaction. The snapshot task fires every 20s
  (`SNAPSHOT_INTERVAL`); appending would grow the queue on every tick. Slot 0 is the track
  that was playing.

## Playback

- **`vc.play(after=...)` runs its callback on another thread.** Advancing the queue must hop
  back to the event loop (`loop.call_soon_threadsafe`, in `music.make_after_callback`), or it
  silently stalls. The coroutine is created on the loop, inside that callback, and started
  with `spawn()`.
- **Position comes from the clock, not FFmpeg**, which exposes no playback cursor.
  `GuildPlayer.mark_started/paused/resumed/stopped` maintain it. **Every `vc.pause()` and
  `vc.resume()` call site must update it**, or the progress bar and `/seek` drift.
- **Build the new source before touching the old one.** `music.prepare_source` does the slow
  resolve and build; `restart_at` only calls `vc.stop()` once it has a source in hand. It
  used to stop first, so a failed resolve dropped the track. It also re-pauses a paused track.
- **`player.generation` lets `/stop` beat a start that is still resolving.** `play_track` and
  `restart_at` capture it before the seconds-long resolve and give up (cleaning up the FFmpeg
  source) if it changed. **Every deliberate stop goes through `GuildPlayer.stop_all()`**,
  which bumps it. Never clear the queue by hand. When an abandoned start leaves nothing
  playing, `advance_playback` calls `finish_playback` itself, since no after-callback will.
- **`player.resume_position` is one-shot**, consumed by the next `advance_playback` like
  `skip_requested`. Otherwise a later track would also start partway through.
- **History for `/previous` is recorded in one place**, `music.advance_with_history`: a track
  goes on `player.history` only when playback moves past it (not when TRACK loop replays it),
  and `stop_all` adds what was playing (unless it's already the newest entry: while the
  next track loads, `current` is the one just recorded). `step_back` puts the previous track first and the
  interrupted one right after it, then sets `back_requested` (one-shot) so that advance takes
  the front and records nothing - recording the interrupted track would make a second
  `/previous` bounce back to it instead of walking further back. Bounded by `MAX_HISTORY`,
  in memory only, cleared by `reset` when Amy leaves voice.
- **`go_back` refuses while `player.lock` is held**: an advance loading the next track, or a
  `/seek` rebuilding the stream. Nothing is audible then and `current` names the track just
  left, so going back would queue behind the track already loading. It is synchronous on
  purpose, so nothing can change between its checks and the queue edit, and the button and
  `/previous` both call it, so they share checks and wording.
- **Fire-and-forget tasks go through `music.spawn()`**, which holds a reference until they
  finish. asyncio keeps only a weak one, and the Python docs warn an unreferenced task can be
  collected before it completes. In practice a task waiting on a lock or I/O is kept alive by
  that wait, so this is about not depending on it - no lost track has actually been seen.
- **The periodic snapshot skips a player whose lock is held.** Mid-advance, the next track
  exists only in a local variable.
- **Stream URLs expire** (about 6 hours). Metadata is resolved when a track is queued; the
  stream URL only when it plays. Never resolve a whole queue up front.
- **At 100% volume Opus passes straight through**; below that it must be decoded to PCM,
  which costs noticeably more CPU and can stutter. That's why the default volume is 1.0.

### FFmpeg

- **`-reconnect*` flags are HTTP options**, not general input options. Passed for a local
  file, FFmpeg exits with "Option reconnect not found"; the source still constructs and plays
  silence, so local playback failed with no error anywhere. `seek_before_options` adds them
  only for http(s).
- **`from_probe` hands the probed bitrate to libopus**, which rejects anything over 512 kbps.
  A WAV probes at 1536, so lossless files also played silence. `build_source` probes and
  constructs separately so `clamp_bitrate` can sit in between.
- **`-ss` goes before `-i`**, or FFmpeg decodes and discards everything up to the seek point
  instead of jumping by the container index.
- **`discord.py[voice]` is required**, not plain `discord.py`. Without the extra the bot starts
  fine and voice fails at runtime: it pulls `PyNaCl` (held below 1.6 by discord.py) and `davey`.

## Queue restore after a restart

- **`restore_player` never touches a player in use.** A `/play` can land while `on_ready` is
  still syncing, and a stale snapshot used to clear it away. If the player is active (playing,
  queued or connected), `decide_restore_action` returns `NOTHING` and the snapshot is dropped.
- **Restore runs once per process** (`_queues_restored`), because `on_ready` fires again after
  a failed RESUME and would queue the playing track twice.
- It only resumes when people are in the channel. Otherwise the queue, and its position, waits
  for `/join` (`start_waiting_queue`) or `/play`, which reports the real queue position
  (`music.starts_immediately`). Every leave path goes through `cleanup_guild_music`, which
  clears the snapshot too.

## Voice

- **`guild.voice_client` is typed `VoiceProtocol`**, not `VoiceClient`, and `vc.channel` is a
  union. Narrow with `voice.get_voice_client()` / `voice.active_channel()`, or pyright fails.

## Music commands

- **One typed handler per command** (`music_play`, `music_seek`, `music_volume`, ...). The
  slash command converts and bounds its arguments through Discord's typing (`Range`,
  `Choice`) and passes ints, a `LoopMode` or the query string straight in. Never turn an
  argument back into text to parse it again: that's what the old single dispatcher did, and
  it squashed spaces in `/play` queries and kept validation that could no longer fire.
- **`MusicContext`** resolves the player, voice client and member once per call.
  `ctx.connected()` returns the voice client only if it's actually connected, typed so the
  checker narrows it. Commands that need a connection check that first, then permission.
- **There is one playback-permission rule**, `may_control_playback`: in Amy's channel, or an
  admin. Commands use it through `ctx.may_control()` and the player buttons call it directly,
  so a button can't bypass a command's check or the other way round.
- Fast commands go through `simple_music(interaction, handler, *args)`; read-only ones pass
  `cooldown=False`. Slow ones (`/play`, `/search`, `/seek`, `/replay`) gate, defer, then call
  their handler themselves - `test_slash.py` reads each callback's source for the defer.

## Discord

- **Slash commands must answer within 3 seconds.** Anything doing network work (`/play`,
  `/search`, `/status`, `/websearch`) calls `interaction.response.defer()` first, or Discord
  shows "application did not respond". `test_slash.py` asserts this.
- **`followup.send()` returns `None` unless passed `wait=True`**; editing the result without it
  crashes. Pyright catches it as an Optional access.
- **`@app_commands.guild_only()` does not narrow the type**: `interaction.guild` stays
  Optional. Use `require_guild()`.
- **`tree.error` is the only error handler for slash commands.** No command wraps its own body,
  so without it an exception leaves the interaction unanswered. It stays quiet on
  `CheckFailure` (`admin_only` and `voice_gate` have already replied through `deny()`), picks
  follow-up or response from `is_done()`, and swallows `HTTPException` for expired tokens.
- **Persistent views need `timeout=None`, fixed `custom_id`s and `bot.add_view()` in
  `on_ready`.** A default view dies after 180s, leaving dead buttons mid-song.
- **A disabled component fails silently**: the client greys it out with a "not allowed" cursor
  and no error. Any view that disables itself must say why in the message. `SearchResults`
  times out after 180s and rewrites its footer to say it expired.
- **`NO_MASS_PINGS` on the client is the only thing stopping mass pings.** Without a
  client-wide `allowed_mentions` Discord parses every mention, and Amy echoes model output and
  YouTube titles. It covers sends, edits (the streamed reply) and interaction follow-ups.
  Never pass a looser `allowed_mentions` on an individual send. `websearch.neutralise_mentions`
  is the second layer, on web text.
- **Messages over 2000 characters are rejected outright.** Send plain text through
  `send_reply`, which splits. `/help` is around 1750 and grows with each command.
- Discord caches the command list client-side. After clearing stale commands, check with the
  API; the client keeps showing them until it is restarted (Ctrl+R).

## The model (Ollama)

- **The right `think` setting is per model.** `false` suits qwen3.5:2b (2.3s, clean) and ruins
  qwen3:4b, which pastes 3,800 characters of its own reasoning into the reply and takes 27.8s.
  `auto` (sent as `think=None`) routes reasoning to a separate `thinking` field that never
  reaches Discord. `true` is almost never right: on qwen3.5:2b it returned nothing. `/model`
  probes and stores `think_mode:<model>`; `ModelManager.think_mode_for()` reads it.
- **Release the old model before loading the new one.** `OLLAMA_KEEP_ALIVE` (default `30m`)
  holds it resident, so switching loaded both at once; on an 8GB GPU one `/model` took 337s.
  Freeing first: 14s. The probe uses a short `PROBE_KEEP_ALIVE` so a rejected candidate
  doesn't stay loaded either.
- **The probe needs about 256 tokens.** In `auto` mode the reasoning uses the budget first, so
  a small cap returns empty content. Empty content with a filled `thinking` field means the
  mode works.
- **A failed load is not evidence a model is gone.** `ModelManager.warm` only preloads and
  never changes the active model. At startup `verify_restored` falls back only when
  `ollama.list()` confirms the model is absent, in memory only, and not if a `/model` happened
  meanwhile.
- The model is preloaded at startup in the background (`ollama.chat(messages=[])` loads
  without generating). It must never block and must swallow errors. An idle model is unloaded
  after about 5 minutes and reloading costs ~4.3s, hence the keep-alive on every call.
- **Tool results need `tool_name`**, or the model ignores its own search results.

## Web search

- **The system prompt must match the tools actually offered.** It once claimed a search tool
  unconditionally; with `WEB_SEARCH=false` Amy answered current-events questions from memory
  9 times out of 9 without saying she couldn't check. `build_system_prompt` assembles it.
- **Don't tell the model it has no internet.** "You do not know current events" made it say
  it couldn't access the web in 2 of 5 runs while holding search results. Saying it has a
  working search tool: 0 of 5.
- **The `web_search` tool description is load-bearing** (vague: 4/7 correct search decisions;
  explicit: 7/7), and **the tool stays single-parameter**: adding a `recency` argument dropped
  correct decisions from 12/12 to 7/12. Freshness is inferred from the query by
  `websearch.infer_recency` instead. `test_websearch.py` pins both.
- **Recency cues match on word boundaries**: substring matching made `news` fire inside
  `Newsom`, filtering out every result an unfiltered search found. Ambiguous queries stay
  unfiltered, and phrases like "history of" or "in 1998" veto a filter.
- **`fetch_page` only fetches public addresses.** Redirects are followed by hand and every hop
  is resolved and checked, or a result page could redirect to `127.0.0.1:11434` and the reply
  would carry it into Discord. Bodies are capped at 2MB (`MAX_PAGE_BYTES`) and each page has
  an 8-second overall deadline (`PAGE_DEADLINE`), because httpx timeouts are per read.
- **Page fetches send browser headers**: a plain user agent got 403 from a third of sites. A
  page that can't be read falls back to its snippet; a result is never dropped.
- **DuckDuckGo throttles with an empty HTTP 202**, not 429. `search()` retries once after 1.5s.

## Security

- **Local file playback is sandboxed to `MUSIC_DIR`** and off by default; otherwise any member
  could probe host files through `/play`. Don't loosen `resolve_local_path`. Local tracks are
  re-checked at play time in `resolve_stream_url`, since restored queues carry absolute paths
  from a previous run.
- **`/dice` runs on the event loop**, so its limits are what stop a huge roll freezing the bot.
- Mentions and SSRF: see [Discord](#discord) and [Web search](#web-search) above.

## Tests

The details are in [tests/README.md](tests/README.md). The rules that matter when changing code:

- **No test touches the real database.** The bot reads settings from it at import, so every
  loader calls `_fakes.isolate_db()` first, which points `AMY_DB_PATH` at a temp file (and
  supplies a dummy token on a fresh clone).
- **Checks record through `tests/_check.py`**, whose gate fails the process at exit, so a
  check's position in the file can't hide its failure.
- **Exit 77 means skipped**, never passed.
- **Branches behind `isinstance(x, discord.VoiceChannel)` need `_fakes.RealVoiceChannel`**; a
  duck-typed fake is invisible to the narrowing.
- **Prove a security or timing test can fail.** Undo the fix and run it (a mutation check).
  Test redirects with `httpx.MockTransport` and a positive control, and make any timeout test
  bound itself with `asyncio.wait_for` so a regression fails in seconds instead of hanging.
- **Add a regression test with every bug fix**, and list it in tests/README.md.
