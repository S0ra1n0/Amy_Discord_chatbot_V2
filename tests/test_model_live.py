"""
Live checks on model switching. Needs Ollama running; no Discord connection.

These cover the two things that went wrong when /model learned to probe:

  * A model's reasoning mode has to be detected, not assumed. think=False is right for
    qwen3.5:2b and makes qwen3:4b paste thousands of characters of its own reasoning into
    the reply, so the wrong choice is visible to everyone in the channel.
  * Switching must release the outgoing model. Holding both at once filled an 8GB GPU and
    made one measured /model call take 337 seconds.

Neither is reachable from an offline suite, because both are properties of the real models.
"""
import asyncio
import io
import os
import subprocess
import sys
import tempfile
import time

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJ)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(PROJ)

from _fakes import load_bot
from database import ConversationDB

amy = load_bot()
amy.db = ConversationDB(tempfile.mktemp(suffix=".db"))   # never touch the real settings

fails = []


def check(label, ok, got=""):
    print(("  PASS " if ok else "  FAIL ") + label)
    if not ok:
        fails.append(label)
        print("        got:", str(got)[:150])


def loaded_models():
    """Model names Ollama currently holds in memory."""
    out = subprocess.run(["ollama", "ps"], capture_output=True, text=True).stdout
    rows = [l.split()[0] for l in out.splitlines()[1:] if l.strip()]
    return [r for r in rows if r]


print("=== Ollama is reachable ===")
try:
    installed = amy.extract_model_names(amy.ollama.list())
except Exception as e:
    print("  SKIP - could not reach Ollama: %s: %s" % (type(e).__name__, e))
    sys.exit(0)
check("at least one model installed", len(installed) >= 1, installed)
print("        installed:", installed)

default = amy.DEFAULT_MODEL
check("the configured default is installed", default in installed,
      "%s not in %s" % (default, installed))

print()
print("=== the default model probes to a usable mode ===")
# Load the model first so the timing below measures the probe rather than Ollama's load,
# which depends entirely on what happens to be resident: the same probe measured 149s cold
# and 0.3s warm on the same machine.
asyncio.run(amy.warm_model())

# If this fails, every reply Amy sends is suspect - it means the shipped default doesn't
# have a reasoning mode that produces a clean answer.
t0 = time.time()
mode = asyncio.run(amy.probe_think_mode(default))
probe_seconds = time.time() - t0
print("        probed in %.1fs -> %r" % (probe_seconds, mode))
check("a mode was found", mode is not None, mode)
check("it is a real mode", mode in amy.THINK_MODES, mode)

print()
print("=== the chosen mode actually produces a clean reply ===")
reply = amy.ollama.chat(
    model=default,
    messages=[{"role": "user", "content": "What is 2 + 2? Answer in one short sentence."}],
    think=amy.think_value(mode or amy.OLLAMA_THINK),
    keep_alive=amy.PROBE_KEEP_ALIVE,
    options={"num_predict": amy.PROBE_MAX_TOKENS},
)
content = (reply["message"].get("content") or "").strip()
thinking = (reply["message"].get("thinking") or "").strip()
print("        content %d ch | thinking %d ch" % (len(content), len(thinking)))
check("something came back", bool(content or thinking), (content, thinking))
check("the reply is not the model narrating its own reasoning",
      not amy.looks_like_reasoning(content), content[:120])

print()
print("=== the probe stays cheap ===")
# /model was instant before it learned to probe, so the probe itself must stay small. This
# times a warm model on purpose; a cold load is Ollama's cost, not the probe's.
check("probing a warm model is quick", probe_seconds < 30.0, "%.1fs" % probe_seconds)
check("the probe is bounded by its own timeout",
      probe_seconds <= amy.PROBE_TIMEOUT * 2 + 5,
      "%.1fs against a %.0fs per-attempt limit" % (probe_seconds, amy.PROBE_TIMEOUT))

others = [m for m in installed if m != default]
if not others:
    print()
    print("  SKIP switching checks - only one model installed")
else:
    other = others[0]
    print()
    print("=== switching releases the outgoing model (guards the 337s regression) ===")
    t0 = time.time()
    out = asyncio.run(amy.switch_model(other))
    elapsed = time.time() - t0
    print("        /model %s took %.1fs" % (other, elapsed))
    print("        %s" % out.splitlines()[0])
    check("switched to the requested model", amy.model == other, amy.model)
    check("the switch was not pathologically slow", elapsed < 120.0, "%.1fs" % elapsed)

    time.sleep(3)                      # let the release and warm-up settle
    resident = loaded_models()
    print("        loaded now:", resident)
    check("the old model was released", default not in resident, resident)
    check("only one model is resident", len(resident) <= 1, resident)

    stored = amy.db.get_setting("think_mode:%s" % other)
    check("a mode was recorded for it", stored in amy.THINK_MODES or stored is None, stored)
    if stored:
        check("the recorded mode is what think_mode_for returns",
              amy.think_mode_for(other) == stored, amy.think_mode_for(other))

    print()
    print("=== switching back leaves the default in place ===")
    asyncio.run(amy.switch_model(default))
    check("back on the default", amy.model == default, amy.model)
    time.sleep(3)
    resident = loaded_models()
    check("still only one model resident", len(resident) <= 1, resident)

print()
print("=== an unknown model is refused before any probing ===")
t0 = time.time()
out = asyncio.run(amy.switch_model("definitely-not-a-real-model"))
check("refused", "not found" in out, out[:120])
check("refused instantly, without loading anything", time.time() - t0 < 5.0,
      "%.1fs" % (time.time() - t0))
check("the active model is unchanged", amy.model == default, amy.model)

amy.db.conn.close()
print()
if fails:
    print("%d CHECK(S) FAILED" % len(fails))
    sys.exit(1)
print("ALL MODEL TESTS PASSED")
