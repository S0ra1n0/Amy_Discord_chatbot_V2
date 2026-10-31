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
| `lyrics.py` | `/lyrics`: title to search terms, choosing a LRCLIB record, timestamp stripping, paging, the one LRCLIB request | httpx |
| `markup.py` | `escape_markdown`: makes stranger-controlled text (titles, lyrics) render literally in Discord | **nothing external** |
| `llm.py` | Reasoning modes, leak detection, probe judging, startup model choice, system prompt, the decisions of a streamed chat reply (overflow, live/final text, empty-answer reason, tool round) | **nothing external** |
| `config.py` | Parsing settings: a bad value warns and falls back instead of crashing | **nothing external** |
| `models.py` | `ModelManager`: the active model, `/model` switching and probing, warm-up, the startup check | ollama, database, llm |
| `logs.py` | Logging setup: console plus a rotating file, and `log_failure` for boundary catch-alls | **nothing external** |
| `mixer.py` | `SpeechQueue` and `Mixer`: Amy's voice mixed over the music, with ducking and the hold-music mode | discord |
| `voicelines.py` | What Amy says out loud: confirmation phrasings, speakable titles and times, how a chat reply becomes speech, the paraphrase prompt | llm, speech (both pure) |
| `speech.py` | Amy's voice: text clean-up, English check, sentence splitting, voice recipes, audio framing, the swappable engines (Kokoro, Piper) and the `Speaker` | Kokoro/PyTorch or Piper/onnxruntime **only when loaded** |
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
  equally. `AMY_VOICE` weights are normalised to sum to 1. A voice is a 510 x 256 block of
  numbers (a speaking style per input length), so a blend is their weighted sum.
  `KokoroEngine.set_recipe` swaps it without reloading the model (~0.1s vs ~9s), which is
  what `tools/voicelab.py` uses to render many voices in one run.
- **Piper is a second, optional engine and follows the same rules.** `TTS_ENGINE` (`kokoro`
  by default) picks it; `piper` and `onnxruntime` are imported only inside `PiperEngine.load`,
  via `importlib`, after the voice files and `find_spec` have been checked
  (`test_speech.py` fails if importing `speech` pulls either in). `piper-tts` is **GPL-3.0**
  and Amy is MIT, so it lives in `requirements-piper.txt`, never in `requirements.txt` or the
  tests' needs; CI does not install it (`test_piper_live.py` SKIPs without it).
- **The Piper voice is pinned and checksummed.** `PIPER_REVISION` plus `PIPER_SHA256`:
  `python speech.py download-piper` fetches `en_GB-cori-medium` into `piper_model/` over plain
  HTTPS, verifies each file, and only then moves it into place - so a half or altered download
  can never be loaded.
- **Piper's CPU use is bounded by `TTS_THREADS`.** `PiperVoice.load` builds its own onnxruntime
  session with every core, so `PiperEngine.load` builds the session itself with
  `intra_op_num_threads = threads` and hands it to `PiperVoice`.
- **Piper's polish is an FFmpeg step that may be missing, and speech must survive that.**
  `PiperEngine.load` asks the injected FFmpeg (`music.find_ffmpeg()` passed in by the bot;
  `speech.py` never imports `music.py`) which filters it has. No FFmpeg, no `rubberband` or no
  EQ filters each drop that step with one warning; a failed or hung run (`PIPER_FFMPEG_TIMEOUT`)
  returns the unprocessed audio, once-warned. The chain is built by the pure
  `piper_filter_chain` (pitch with rubberband's quality options, then the voice EQ and
  de-esser; `PIPER_SOFT` is the voice-EQ switch, its name kept from an earlier EQ); FFmpeg runs without `shell=True` and without a console window.
- **`piper_text` runs for Piper only**, inside `PiperEngine.synthesize`. It fixes what espeak-ng
  mispronounces in track lists ("feat.", "#3", "2 min", "1:05", and the `PIPER_NAMES`
  respellings). Names are respelt, never given as `[[ ... ]]` raw phonemes: Piper drops the full
  stop after a raw block, and `piper_text` strips every `[` and `]` (stripping pairs in one
  pass let `[]][x][[]` rebuild a block) so chat text can't inject one.
  "feat." and "ft." are in `split_sentences`' abbreviations so a credit isn't cut in two before
  `piper_text` sees it - including "(feat." / "[ft.", which is why the last word is compared
  with any opening bracket stripped. Known, accepted edges of the "featuring" rewrite: a
  capitalised word before "Feat." ("a Great Feat. Next...") is read as a credit, and lower-case
  artists ("feat. bbno$") or "Song 2 feat. X" are left as "feat". The engine's output is at the voice's own rate (22.05 kHz);
  `to_discord_frames` resamples it like any other.
- **Speech audio is normalised to `PEAK_TARGET`** before it leaves `speech.py`, leaving
  headroom for mixing over music (audio adds saturate rather than wrap).
- **Speech text is logged at DEBUG only**, like chat, so it never reaches `amy.log`.
- **With TTS on, every track plays through a `mixer.Mixer`** (wrapped in `music._start`).
  Quiet, it passes the music through untouched - still opus, no CPU cost. With speech
  queued it decodes, fades the music to `DUCK_LEVEL` (55%) over 200 ms, adds the voice (already
  scaled to `PEAK_TARGET * VOICE_LEVEL` by `Speaker`), and fades
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
- **What Amy says lives in `voicelines.py`, pure.** Confirmations are plain text with
  several phrasings, never a model call (instant). Every phrasing of every event is checked
  by `test_voicelines.py` to format cleanly into speakable English.
- **A chat reply is calmed before it is spoken, not before it is written.** `voicelines.calm_reply`
  drops trailing stock filler (closing offers of help, "How about you?") and turns "!" into "."
  in `speak_reply`. It runs on the CLEANED text (inside `plan_reply`, and on the output of
  `judge_paraphrase`), never on raw markdown: it joins sentences with spaces, which would defeat
  the line-anchored bullet/heading/quote stripping. The 2b chat model ignores soft prompt lines,
  so the prompt names the bans and this is the net underneath. It never touches `/say` or music
  lines, and never empties a reply.
- **Chat replies are spoken only to someone in the call with her** (`in_call_with_amy`),
  checked in `on_message`, and always in the background (`music.spawn(speak_reply(...))`):
  the text reply is already out, and the paraphrase plus synthesis take seconds.
- **The paraphrase prompt is load-bearing and measured.** A first version made qwen3.5:2b
  answer the reply as if the user had written it, and flip a fact ("rain Thursday" for "Thursday
  stays dry"). Telling it the text is its OWN answer and to keep every fact: 4/4 faithful.
  `test_voicelines.py` pins the cues; changing them means re-measuring. A paraphrase that
  times out (`PARAPHRASE_TIMEOUT`), runs long, isn't English or reads like reasoning is
  replaced by the opening sentences plus "the rest is in the chat" - never spoken as is.
- **`announce` speaks only after an action succeeded**: errors and read-only commands stay
  silent, and `can_speak` (voice loaded and in a call) gates every line.
- **One utterance at a time per server** (`GuildPlayer.speech_turn`, held by
  `_speak_sentences`). Two replies at once used to take turns on the single speech thread
  and interleave their sentences ("Alpha one, Bravo one, Alpha two..."). Start speech with
  `_queue_utterance`, which captures the generation when it's ASKED for, so a /stop also
  cancels an utterance still waiting its turn.
- **`TTS_THREADS` is a stutter setting, not just a speed one.** Measured under load (music,
  non-stop speech and a chat reply): 6 threads kept audio frames as punctual as plain music
  (p99 8 ms late); 8 threads made them late enough to hear (p99 58 ms) and was slower.
  The only slow mixer read in a song is frame 0 (~190 ms): FFmpeg opening the stream.
- **Every stop of speech goes through `SpeechQueue.clear()`, which bumps its `generation`.**
  Speech is synthesised a sentence at a time in the background; `_speak_sentences` captures
  the generation when it starts and drops its remaining sentences once it moves. Before
  this, `/stop` mid-way through a long line cleared the queue while the rest kept arriving
  - she carried on talking. `/stop`, `/leave`, `/shush` and `/voice off` all go through it.
- **`voice_enabled` (`/voice`, saved) mutes without unloading**: `voice_problem` reports it,
  so `can_speak`, replies, confirmations and `/say` all go quiet together. The Shush button
  is removed from `PlayerControls` when there's no speaker at all (`TTS=off`) - decided once
  per process, so the registered persistent view and every card agree.
- **"Up next" is spoken only when a song ends by itself**
  (`voicelines.should_announce_next`): a different track follows one that finished - not a
  `/skip`/`/skipto` (`force_next`), `/previous`, a restored mid-track start, or a repeat (same
  track object). `advance_playback` announces right after `play_track` succeeds and BEFORE
  the `refresh_now_playing` await: `/skip` and `/stop` don't take the player lock, so one
  landing during that Discord edit would otherwise be followed by a stale "Up next". The
  announce has its own try, so a speech failure is never taken for a bad track. `up_next`
  (`/voice up_next:`, saved, default on) is independent of `voice_enabled`.
- **Autoplay happens only when a YouTube song really played and ended on its own, with listeners present**
  (`music.should_autoplay`). `advance_playback` calls it when `advance_with_history` finds
  nothing next, using `previous`: `/stop`, `/leave`, the Stop button and idle leaves clear
  `current` (`stop_all`), so there is nothing to follow; a `/previous`, a `/skip` of a song
  somebody queued, a local file, an empty channel and the `autoplay` setting (saved, default
  on) also say no. A `/skip` of an Autoplay song (`requested_by == AUTOPLAY_REQUESTER`) moves
  on. The song must have played `AUTOPLAY_MIN_PLAYED` seconds (measured from the position
  clock BEFORE `finish_playback` resets it; `played_enough`): an unreadable stream ends at
  once and would otherwise chain through the Mix forever. The pick is the first entry of YouTube's Mix (`related_tracks`, bounded with
  `playlistend`: the Mix is endless) not among `player.history`, the ended song and
  `last_played`, not live and at most `AUTOPLAY_MAX_SECONDS`, compared by video id (`youtube_video_id`).
  **The lookup runs with the player lock RELEASED** (2-20 s): `advance_playback` decides under
  the lock, sets `player.autoplay_lookup` (a token; `current` stays the ended song, so
  `/join`'s `start_waiting_queue` and `stop_all`'s history rule are unchanged), releases the
  lock for `autoplay_candidates`, then re-takes it and re-checks in this order: marker still
  ours (else another advance took over: do nothing, no finish card), vc connected (else
  close the card, `reset` and clear the saved queue), `generation` unmoved (a `/stop`:
  finish), no track started, then a song queued meanwhile plays instead (not announced "Up
  next": the `/play` speaks for itself), else the picks - unless `/autoplay off` came
  meanwhile, which drops them (a queued song still plays). Any other `advance_playback` that gets the lock first (a `/play`, a `/previous`)
  calls `supersede_autoplay_lookup`, which clears the marker, sets `current = None` (the
  ended song is already on `history` and `last_played`, and a set `current` would make a
  `/loop` mode replay or rotate it instead of the queued song) and suppresses "Up next".
  Cancellation anywhere in the unlocked or re-locking stretch clears a marker that is still
  ours. `reset` clears the marker too; `stop_all` does not (the lookup must see the moved `generation`). Any
  failure finishes as before. The lookup yields up to
  `AUTOPLAY_ATTEMPTS` spares; a pick that fails to start moves to the next, and when they run
  out playback finishes (a user-queued track that fails still just skips). The pick goes
  through the normal play path, so the usual "Up next" applies.
- **`/leave` says goodbye after its permission check and before disconnecting**, waiting
  at most `GOODBYE_WAIT`; a refused `/leave` says nothing. Idle and empty-channel leaves
  don't say goodbye - nobody is listening.

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

## Error handling

- **A boundary may catch `Exception`; inner code catches what it expects.** Boundaries are
  places where any failure must not crash the bot or leave a user unanswered: command and
  button handlers, background loops, startup steps, thread/after-callbacks, cleanup that
  must run (rolling back `filter_name`, deleting an orphaned channel). Everything else names
  its exceptions (`RuntimeError` from a closing loop, `DISCORD_ERRORS` from a reply,
  `httpx.HTTPError`/`OSError`/`ValueError`/`ollama.ResponseError` from Ollama), so a TypeError
  or `AttributeError` surfaces instead of reading as "Unavailable".
- **A broad catch never logs just `{e}`.** `logs.log_failure(msg, exc, expected, level)` logs
  a failure in `expected` as one line (type name included: asyncio's TimeoutError has an empty
  message) and anything else with its traceback. The tuples live next to what raises them:
  `music.lookup_errors()` for yt-dlp/FFmpeg (resolved on first use: importing yt-dlp costs
  ~0.4 s at startup), `DISCORD_ERRORS`/`VOICE_ERRORS` in `voice.py`. Persistence catches
  (`Could not save/restore/clear the queue`) use `exc_info=True`. `test_failure_logging.py`
  guards it.
- **Code on the audio thread checks for shutdown first.** After `Client.close()` the bot's
  loop is discord.py's MISSING sentinel, so `_on_hold_done` returns before scheduling:
  anything that raises inside `Mixer.read` fails the mixer.
- **No silent `pass` without a reason.** The quiet case is a best-effort
  `except discord.HTTPException: pass` on a cosmetic edit or delete.
- **Every module has been audited.** Calls to Ollama catch `models.OLLAMA_ERRORS`
  (`httpx.HTTPError`/`OSError`/`ValueError`/`ollama.ResponseError`; `/status` uses the same
  tuple); the after-callback catches `RuntimeError` (a closed loop); page fetches catch
  `websearch.FETCH_ERRORS` and log anything else with a traceback. The remaining broad catches
  are boundaries and say so in a comment: `Mixer.read` (audio thread), `ModelManager.warm` and
  `paraphrase`, `Speaker.start`/`frames_for`, the web search/enrichment wrappers, the Opus
  probe fallback, and best-effort source cleanup - each logs through `log_failure` (or
  `exc_info`). `database.py`'s and `speech.download_piper`'s broad catches clean up and
  re-raise, and `_SafeConsoleHandler.emit` follows the logging contract (`handleError`).

## Settings

- **Nothing reads a setting at import in a module the bot imports before `load_dotenv()`.**
  `database.py` once read `HISTORY_LIMIT` at import, about 60 lines before `.env` loaded, so
  the `.env` value was silently ignored for its whole life. Settings are parsed in the bot,
  after `load_dotenv`, with `config.parse_*`, and passed in. `database.py` reads no
  environment variables at all. `test_config.py` re-imports the bot freshly per
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
- **Position is TRACK seconds, whatever the audio filter.** A speed filter (`/filter nightcore`
  1.25, `slowed` 0.8; `music.FILTER_PRESETS` is the one table of chains and rates) moves through
  the track at `rate` track seconds per wall second, so `elapsed_seconds` scales only the
  wall-clock part by `GuildPlayer.rate` (the seek offset is already track time, and pauses
  still freeze the clock). Everything reads that one number: the progress bar, `/seek` and
  `-ss` (applied to the input, before the filter), the queue snapshot's `resume_position`, and
  autoplay's `played_enough` (track seconds). The only wall-time use is `end_timestamp`, which
  divides the time left by the rate. `play_track` and `restart_at` capture `player.filter_name`
  BEFORE the resolve and pass that same name to `build_source` and `_start`, and `_start` records
  it as `player.active_filter`; `player.rate` is a read-only property derived from it (never set
  separately, so it can't disagree). `active_filter` describes the source that is really
  playing, `filter_name` only what the NEXT source will use. Every live card is built by
  `ui.player_card(player, paused)`, which reads `active_filter` and `rate` (never `filter_name`),
  so it shows and times what is audible. `/seek`, `/replay` and `/filter` share one restart path,
  `_restart_playing`: it takes `player.lock` and calls the command's `decide()` UNDER it. For
  `/filter` that is where "same preset already set", "playing or idle" and "can't rebuild this
  track" are decided: an advance loading a track holds the lock with nothing audible yet, and that
  track has already captured the old filter, so the command waits and then restarts it; two
  identical `/filter` at once restart once. Idle (or a non-http track) only stores the preset and
  speaks the `filter_later` line, never the "changed" one. A restart that fails or is overtaken by
  `/stop` puts `filter_name` back; because `restart_at` can fail after `vc.stop()`, the failure
  reply checks `music_active` and says playback stopped instead of "still playing". A filter forces
  the PCM path in `build_source` (opus passthrough can't change samples). Filters touch the music
  source only, so Amy's voice, mixed in by the `Mixer`, is never filtered. The filter is per
  player, kept by `/stop`, cleared by `reset` (leaving), and not persisted.
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
  `/previous` both call it, so they share checks and wording. During autoplay's Mix lookup the
  lock is free, so it is NOT refused: it goes back to the song that just ended.
- **Fire-and-forget tasks go through `music.spawn()`**, which holds a reference until they
  finish. asyncio keeps only a weak one, and the Python docs warn an unreferenced task can be
  collected before it completes. In practice a task waiting on a lock or I/O is kept alive by
  that wait, so this is about not depending on it - no lost track has actually been seen.
- **The periodic snapshot skips a player whose lock is held, or whose `autoplay_lookup` is
  set.** Mid-advance, the next track exists only in a local variable; mid-lookup, `current` is
  still the ended song.
- **Stream URLs expire** (about 6 hours). Metadata is resolved when a track is queued; the
  stream URL only when it plays. Never resolve a whole queue up front.
- **At 100% volume Opus passes straight through**; below that it must be decoded to PCM,
  which costs noticeably more CPU and can stutter. That's why the default volume is 1.0. An
  active audio filter decodes too, whatever the volume.

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
- **One typed handler per voice command** (`voice_join`, `voice_create`, `voice_leave`), each
  taking the interaction and an already-narrowed guild; `/create`'s name is passed as a string
  (whitespace collapsed, then `voice.sanitize_channel_name`). The slash commands call them
  directly after `voice_gate`/`require_guild`/`defer`.

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
  `send_reply`, which splits. `/help` is the exception: it is embeds (`commands_help.help_pages` -> `ui.help_embeds`, one description per page, admins get a second), so it has 4,096 characters per description and 6,000 per message to grow into; `test_slash.py` guards both limits.
- Discord caches the command list client-side. After clearing stale commands, check with the
  API; the client keeps showing them until it is restarted (Ctrl+R).

## Lyrics

- **Lyrics come from LRCLIB through `lyrics.py` only**, with the same rules as the other
  network code: `search_lrclib` is bounded by one overall `TIMEOUT` (httpx's own limits are per
  read), sends a descriptive User-Agent, and turns every failure into `LyricsError`; the command
  logs it at WARNING and answers politely, so a lookup can never crash a command. "No match" is
  `None`, not an error. Tests fake `lyrics.search_lrclib`; nothing reaches the network.
- **The match is shown, never assumed.** `choose_result` prefers the record within
  `DURATION_TOLERANCE` seconds of the track's length (else the first usable); `find_lyrics` with a
  known duration keeps searching (the free-text `q` after the artist pair) until a record is
  within tolerance, and only then falls back to the first attempt's guess. And the embed is
  titled with the matched "Artist - Title". Matching covers and remixes wrongly is expected.
- **One embed per message.** Discord caps the total text of a message's embeds at 6000
  characters, so three 4000-character pages in one message would be rejected; `music_lyrics`
  sends each page as its own follow-up (`lyric_pages` caps at `MAX_PAGES`).
- **Anything a stranger controls is escaped with `markup.escape_markdown` before it goes into
  message text or an embed DESCRIPTION** (lyrics records, YouTube titles, LRCLIB names): a masked
  link or spoiler would otherwise render. That covers every reply that names a track
  (/play and search-select "Loading", /skip, /previous, /skipto, /remove, /replay, /seek, the
  /filter failure replies, /status, /queue, /search), plus requester display names (/queue,
  the "Requested by" embed fields, the /remove refusal) and /websearch results
  (`ui.search_web_embed`: the snippet is escaped; the title is used as masked-link TEXT, where
  Discord may not honour `\]`, so its `[` and `]` are replaced by parentheses before escaping
  rather than relying on escapes; `ui.safe_link_url` allows only http(s) without whitespace
  and percent-encodes `)`, otherwise the result is listed without a link).
  Deliberately NOT escaped: embed TITLES
  (`ui.py` player/lyrics/playlist cards; they render far less markdown), select-option labels,
  anything spoken (`announce`, voicelines), logs and LLM context. `music_lyrics` escapes the lyrics BEFORE `lyric_pages` splits them
  (escaping adds characters, so splitting first could push a page over the embed limit;
  `lyrics.py` itself never escapes and imports only httpx). `split_pages` never cuts right after
  a lone backslash and escapes a `#`, `>` or `-` that starts a continuation page. The not-found
  reply's title and the instrumental reply's heading are escaped after `_shown` truncates (a cut
  can't strand a backslash); the searched terms sit in an inline code span with backticks
  removed. `music.render_queue` (/queue) and `ui.search_results_embed` escape track titles too.
  `markup.py` is stdlib only so `music.py` can use it (`ui.py` imports `music.py`).
- **Free-text matches must match the artist.** In `find_lyrics`' `q` round the strict duration
  pick applies only to records passing `names_match`: with an artist in the terms, the record's
  artist (or its track name, for "Song - Artist" order) must contain it or be contained by it
  after `_fold_name` (accents on Latin letters dropped, casefolded, "&" = "and"; marks on
  other scripts stay, so Japanese dakuten and Hangul are not folded); with no artist, the title. If
  some pass but none is the right length, the first passing record is the guess. If NONE pass
  (a romanized name vs a native one cannot be matched), the length is the only evidence, so the
  duration preference over all results applies. Local files go through `local_title` (extension
  dropped; a leading "7. " number always, a "NN - " one only when zero-padded or the rest still has " - ") before
  `search_terms`. `split_pages` rejects `limit < 2`.
- **Lyrics are never spoken** and never go to the model: `music_lyrics` has no `announce`.

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
- **`chat_streaming` only does the I/O.** The worker thread, the queue, the 1.5 s edit throttle
  and the Discord calls stay in the bot; every decision is a pure `llm.py` helper:
  `plan_overflow` (which pieces to finalize once pending text passes `STREAM_CHUNK_LIMIT`),
  `live_message_text` / `final_message_text`, `final_reply` (empty answer: token ceiling vs
  ordinary stop), `tool_round_call` / `tool_call_query` / `tool_round_messages` (one search,
  never a loop) and `system_with_time`. `test_llm.py` pins them; `test_regression.py` drives
  the whole function over scripted streams.

## Web search

- **The system prompt must match the tools actually offered.** It once claimed a search tool
  unconditionally; with `WEB_SEARCH=false` Amy answered current-events questions from memory
  9 times out of 9 without saying she couldn't check. `build_system_prompt` assembles it.
  The same goes for actions: chat can't run slash commands, so `BASE_SYSTEM_PROMPT` tells her
  to name the command (e.g. `/play`) rather than claim it's done, and claims no reminders.
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
