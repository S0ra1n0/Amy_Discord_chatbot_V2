"""
Which Ollama model Amy talks with, and keeping it loaded.

Moved out of Amy_chatbot_V2.py, where it was module-level functions sharing two globals
(`model` and `_warmup_task`). `ModelManager` owns that state now. The decisions themselves -
judging a probe reply, choosing a model at startup - are pure functions in llm.py; this is
the part that talks to Ollama and the settings table.

The rules that shaped this code are in ARCHITECTURE.md under "The model (Ollama)".
"""
import asyncio
import logging
import time
from typing import List, Optional, Tuple, Union

import ollama

from database import ConversationDB
from llm import (extract_model_names, judge_probe_reply, normalise_think_mode,
                 startup_model_choice, think_value)

log = logging.getLogger("amy.models")

# Bounds on the /model reasoning probe. Switching models forces Ollama to load the new one,
# so an unbounded probe can take minutes - one measured run sat at 337s. A leaking model
# gives itself away in its first few words, so a short generation is enough - but in "auto"
# mode the reasoning spends the budget first, so 48 tokens read as an empty failure.
PROBE_MAX_TOKENS: int = 256
PROBE_TIMEOUT: float = 60.0
# Long enough that the second probe attempt reuses the loaded model, short enough that a
# rejected candidate does not sit in VRAM.
PROBE_KEEP_ALIVE: str = "2m"
# A spoken paraphrase is one or two sentences; this caps a model that rambles.
PARAPHRASE_MAX_TOKENS: int = 120


def set_model_residency(name: str, keep_alive: Union[str, int]) -> None:
    """
    Load `name` and hold it for `keep_alive`, or unload it at once with keep_alive=0.
    Blocking - run in a thread.

    An empty message list is Ollama's way of asking for exactly this and nothing else: it
    returns done=True with zero characters generated. The one place both preloading and
    releasing a model go through.
    """
    ollama.chat(model=name, messages=[], keep_alive=keep_alive)


class ModelManager:
    """The active model, its per-model reasoning mode, and its warm-up."""

    def __init__(self, db: ConversationDB, default: str, think_default: str,
                 keep_alive: str) -> None:
        self.db = db
        self.default = default              # OLLAMA_MODEL, or the built-in default
        self.think_default = think_default  # OLLAMA_THINK
        self.keep_alive = keep_alive        # OLLAMA_KEEP_ALIVE
        # Restored from settings so a /model survives a restart. Verified against Ollama at
        # startup (verify_restored), because checking it needs a network call.
        self.current: str = db.get_setting("model") or default
        # Held so the warm-up task isn't garbage collected mid-flight
        self.warmup_task: Optional[asyncio.Task] = None

    def think_mode_for(self, model_name: str) -> str:
        """
        The reasoning mode to use with `model_name`.

        Per-model, because the correct setting is not the same for every model: think=False
        suits qwen3.5:2b but makes qwen3:4b write its reasoning into the reply. /model records
        what it found for each one; anything unrecorded falls back to the configured default.
        """
        return normalise_think_mode(
            self.db.get_setting(f"think_mode:{model_name}"), fallback=self.think_default)

    async def probe_think_mode(self, model_name: str) -> Optional[str]:
        """
        Work out which reasoning mode keeps `model_name`'s replies clean.

        Tries the configured default first, then "auto". Returns the winning mode, or None if
        neither worked or Ollama could not be reached - the caller then leaves the per-model
        setting alone rather than recording a guess.
        """
        probe = [{"role": "user", "content": "What is 2 + 2? Answer in one short sentence."}]

        def ask(mode: str) -> Tuple[str, str]:
            # num_predict keeps this cheap: only the opening words are needed to tell an
            # answer from a model narrating its own reasoning, and a leaking model would
            # otherwise run to thousands of tokens. A short keep_alive, not the usual one:
            # long enough that the second attempt doesn't reload the model, short enough that
            # a rejected candidate isn't squatting in VRAM for half an hour.
            reply = ollama.chat(model=model_name, messages=probe,
                                think=think_value(mode), keep_alive=PROBE_KEEP_ALIVE,
                                options={"num_predict": PROBE_MAX_TOKENS})
            message = reply["message"]
            return ((message.get("content") or "").strip(),
                    (message.get("thinking") or "").strip())

        loop = asyncio.get_running_loop()
        # Each mode once: with the default already "auto" this used to probe "auto" twice,
        # spending up to a second full timeout on a model that had just failed it.
        for mode in dict.fromkeys((self.think_default, "auto")):
            try:
                content, thinking = await asyncio.wait_for(
                    loop.run_in_executor(None, ask, mode), timeout=PROBE_TIMEOUT)
            except asyncio.TimeoutError:
                log.warning(f"Probing {model_name} in '{mode}' mode timed out "
                            f"after {PROBE_TIMEOUT}s")
                return None
            except Exception as e:
                log.warning(f"Could not probe {model_name} in '{mode}' mode: {e}")
                return None

            if judge_probe_reply(content, thinking):
                return mode
            log.info(f"{model_name} gave no usable reply in '{mode}' mode")
        return None

    async def switch(self, name: str) -> str:
        """Show the active Ollama model, or switch to another installed one (/model)."""
        if not name:
            return (f"\U0001F9E0 Current model: `{self.current}` "
                    f"(reasoning: {self.think_mode_for(self.current)})\nPass a name to switch.")

        loop = asyncio.get_running_loop()
        try:
            available = await loop.run_in_executor(None, ollama.list)
        except Exception as e:
            return f"\U0001F6AB Could not reach Ollama to verify the model: {e}"

        model_names = extract_model_names(available)
        if name not in model_names:
            names_list = ", ".join(f"`{n}`" for n in model_names) or "none installed"
            return f"\U0001F6AB Model `{name}` not found. Installed models: {names_list}"

        # Release the outgoing model FIRST. The keep-alive would otherwise hold it for half
        # an hour after nothing can use it, and the replacement then has to load alongside
        # it - on an 8GB GPU that leaves ~300MB free, and one measured switch took 337s.
        # Freeing first: 14s.
        previous = self.current
        if previous and previous != name:
            try:
                await loop.run_in_executor(None, set_model_residency, previous, 0)
                log.info(f"Released {previous} from memory")
            except Exception as e:
                log.warning(f"Could not release {previous}: {e}")

        # Establish how this model handles reasoning before committing to it. Without this,
        # switching to a model that ignores think=False silently fills Discord with the
        # model's internal monologue, several times slower, and nothing says why.
        working = await self.probe_think_mode(name)

        self.current = name
        self.db.set_setting("model", name)                    # survives a restart
        if working:
            self.db.set_setting(f"think_mode:{name}", working)
        log.info(f"Model switched to {name} (reasoning: {working or 'unverified'})")

        # Load the new one properly now, so the first real message doesn't pay for it.
        self.warmup_task = asyncio.create_task(self.warm(name))

        note = ""
        if working is None:
            note = ("\n⚠️ I couldn't confirm how it handles reasoning. If replies come out "
                    "rambling or very slow, switch back.")
        elif working != self.think_default:
            note = (f"\nℹ️ This one needs `{working}` reasoning mode rather than the usual "
                    f"`{self.think_default}`, so I've set that for it.")
        return f"\U0001F9E0 Model switched to `{name}`{note}"

    def preload(self, name: str) -> None:
        """Load `name` and keep it resident for the keep-alive window. Blocking."""
        set_model_residency(name, self.keep_alive)

    async def warm(self, name: Optional[str] = None) -> bool:
        """
        Take the model load off the first real message. Returns whether it loaded.

        The keep-alive holds the model between conversations, but a restart always starts
        cold, and that first reply pays ~4.3s before a single character appears. Doing it
        here moves the wait to startup, where nobody is watching a "Thinking..." message.

        A plain preload: it never changes which model is active. It used to double as the
        startup check, so /model - which re-runs it - could have a fresh choice reverted by
        a load that merely failed. Failures are logged and swallowed: Ollama being slow or
        absent must not stop the bot, and the first chat loads the model itself if need be.
        """
        target = name or self.current
        started = time.monotonic()
        try:
            await asyncio.get_running_loop().run_in_executor(None, self.preload, target)
        except Exception as e:
            log.warning(f"Could not warm '{target}', the first reply will be slower: {e}")
            return False
        log.info(f"Model '{target}' warmed in {time.monotonic() - started:.1f}s "
                 f"(stays loaded for {self.keep_alive})")
        return True

    async def verify_restored(self) -> None:
        """
        Startup only: warm the model restored from settings, and fall back if it's gone.

        A model saved by /model can be uninstalled between runs, and every reply would then
        fail. But the fallback is deliberately narrow (see llm.startup_model_choice) and lives
        in memory only - the saved setting is left alone, so reinstalling the model, or simply
        starting once Ollama is up, brings the admin's choice back.
        """
        saved = self.current
        loaded = await self.warm(saved)
        if loaded or saved == self.default:
            return

        try:
            listing = await asyncio.get_running_loop().run_in_executor(None, ollama.list)
            installed: Optional[List[str]] = extract_model_names(listing)
        except Exception:
            installed = None                        # can't tell - so don't conclude anything

        chosen = startup_model_choice(saved, self.default, loaded, installed)
        if chosen == saved:
            reason = "Ollama isn't reachable yet" if installed is None else "it's installed"
            log.warning(f"Couldn't load saved model '{saved}' right now; keeping it "
                        f"because {reason}. The first reply will load it.")
            return
        if self.current != saved:
            return                                  # an admin ran /model meanwhile - theirs wins
        log.warning(f"Saved model '{saved}' is no longer installed; using '{chosen}' for this "
                    f"run. The saved choice is kept, so reinstalling '{saved}' brings it back.")
        self.current = chosen
        await self.warm(chosen)

    async def paraphrase(self, system: str, text: str, timeout: float,
                         max_tokens: int = PARAPHRASE_MAX_TOKENS) -> Optional[str]:
        """
        Ask the active model to rewrite `text` (e.g. for speaking aloud). Returns its content,
        or None if it took longer than `timeout`, failed, or said nothing. Never raises.

        Uses the model's own reasoning mode and the normal keep-alive, so it's the same warm
        model the chat uses - no second model loads.
        """
        name = self.current

        def ask() -> str:
            reply = ollama.chat(model=name,
                                messages=[{"role": "system", "content": system},
                                          {"role": "user", "content": text}],
                                think=think_value(self.think_mode_for(name)),
                                keep_alive=self.keep_alive,
                                options={"num_predict": max_tokens})
            return (reply["message"].get("content") or "").strip()

        try:
            content = await asyncio.wait_for(
                asyncio.get_running_loop().run_in_executor(None, ask), timeout=timeout)
        except asyncio.TimeoutError:
            log.info(f"Paraphrase took longer than {timeout}s; using the fallback")
            return None
        except Exception as e:
            log.warning(f"Paraphrase failed, using the fallback: {e}")
            return None
        return content or None

    def start_startup_check(self) -> None:
        """Background verify_restored, once - on_ready can fire again after a failed RESUME."""
        if self.warmup_task is None or self.warmup_task.done():
            self.warmup_task = asyncio.create_task(self.verify_restored())
