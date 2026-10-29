"""
Import the bot with a simulated .env and report what it resolved - one scenario at a time,
all in ONE worker process.

Settings are read when the bot is imported, so every scenario needs a fresh import of
Amy_chatbot_V2.py. A fresh *interpreter* per scenario cost ~1.2 s each (almost all of it
importing discord, httpx and ollama), so test_config.py now keeps one worker alive instead:

    from _config_probe import probe
    code, result, output = probe({"HISTORY_LIMIT": "4"}, extra_env={"OLLAMA_KEEP_ALIVE": "5m"})

`probe` sends the scenario to the worker (started on first use) as one JSON line and reads one
JSON line back. The worker, for each scenario:

- replaces os.environ wholesale with exactly the environment the parent built (so a setting
  the scenario doesn't name is genuinely absent, as in a fresh process),
- fakes python-dotenv so the user's real .env is never read: `file_values` plays the .env,
  with the same "an existing variable is not overridden" rule,
- executes Amy_chatbot_V2.py as a brand-new module (the project's other modules read no
  environment at import, so they stay cached - that is the saving),
- captures stdout and stderr for that scenario only and reports the exit code (a bot that
  calls exit(1) raises SystemExit here; an unexpected exception is reported as code 1 with its
  traceback in the output),
- then closes the log handlers and database the import opened and restores everything, so
  nothing leaks into the next scenario.

Run directly (`python _config_probe.py`) it is the worker; imported, it is the client.
"""
import io
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PROJ = os.path.dirname(HERE)

# Settings a scenario must not inherit from the machine it runs on.
SCRUBBED = ("HISTORY_LIMIT", "DB_PRUNE_DAYS", "WEB_SEARCH", "OLLAMA_THINK", "OLLAMA_KEEP_ALIVE",
            "DISCORD_TOKEN", "TTS", "AMY_VOICE", "TTS_SPEED", "TTS_THREADS", "DUCK_LEVEL",
            "VOICE_LEVEL", "TTS_ENGINE", "PIPER_VOICE", "PIPER_LENGTH", "PIPER_PITCH",
            "PIPER_SOFT")
REPLY = "PROBE_REPLY "
_worker = None


# ---- client (used by test_config.py) ----------------------------------------------------
def _stop_worker():
    global _worker
    if _worker is not None:
        try:
            _worker.stdin.close()
            _worker.wait(timeout=10)
        except Exception:
            _worker.kill()
        _worker = None


def probe(dotenv_values, extra_env=None):
    """Import the bot as if .env held `dotenv_values`. Returns (exit code, result or None, output)."""
    global _worker
    env = {k: v for k, v in os.environ.items() if k not in SCRUBBED}
    env.update(extra_env or {})
    env["PYTHONIOENCODING"] = "utf-8"
    if _worker is None or _worker.poll() is not None:
        _worker = subprocess.Popen(
            [sys.executable, os.path.join(HERE, "_config_probe.py")], env=env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace")
        import atexit
        atexit.register(_stop_worker)
    assert _worker.stdin is not None and _worker.stdout is not None
    _worker.stdin.write(json.dumps({"dotenv": dotenv_values, "env": env}) + "\n")
    _worker.stdin.flush()
    noise = []
    while True:
        line = _worker.stdout.readline()
        if not line:   # the worker died without answering
            _worker = None
            return -1, None, "probe worker exited\n" + "".join(noise)[-1500:]
        if line.startswith(REPLY):
            reply = json.loads(line[len(REPLY):])
            return reply["code"], reply["result"], reply["output"]
        noise.append(line)


# ---- worker -----------------------------------------------------------------------------
def _report(amy):
    return {
        "HISTORY_LIMIT": amy.HISTORY_LIMIT,
        "db_max_messages": amy.db.max_messages,
        "DB_PRUNE_DAYS": amy.DB_PRUNE_DAYS,
        "WEB_SEARCH": amy.WEB_SEARCH,
        "OLLAMA_THINK": amy.OLLAMA_THINK,
        "OLLAMA_KEEP_ALIVE": amy.OLLAMA_KEEP_ALIVE,
        "TTS": amy.TTS,
        "speaker": amy.speaker is not None,
        "AMY_VOICE": amy.AMY_VOICE,
        "TTS_SPEED": amy.TTS_SPEED,
        "TTS_THREADS": amy.TTS_THREADS,
        "DUCK_LEVEL": amy.DUCK_LEVEL,
        "VOICE_LEVEL": amy.VOICE_LEVEL,
        "TTS_ENGINE": amy.TTS_ENGINE,
        "PIPER_VOICE": amy.PIPER_VOICE,
        "PIPER_LENGTH": amy.PIPER_LENGTH,
        "PIPER_PITCH": amy.PIPER_PITCH,
        "PIPER_SOFT": amy.PIPER_SOFT,
        "engine": amy.speaker.engine.name if amy.speaker is not None else None,
        "engine_settings": ({"length": amy.speaker.engine.length, "pitch": amy.speaker.engine.pitch,
                             "soft": amy.speaker.engine.soft, "threads": amy.speaker.engine.threads,
                             "model_path": amy.speaker.engine.model_path,
                             "ffmpeg_given": bool(amy.speaker.engine.ffmpeg)}
                            if amy.speaker is not None and amy.speaker.engine.name == "Piper" else None),
        "voice_description": amy.describe_voice(),
        "voice_enabled": amy.voice_enabled,
        "speaker_level": amy.speaker.level if amy.speaker is not None else None,
        # Loading is deferred to on_ready; importing the bot must never pull PyTorch in.
        "torch_imported": any(m in sys.modules for m in ("torch", "kokoro", "piper", "onnxruntime")),
    }


def _run_scenario(scenario, real_stdout):
    import importlib.util
    import logging
    import traceback

    import dotenv
    from _fakes import isolate_db

    file_values = dict(scenario.get("dotenv", {}))
    file_values.setdefault("DISCORD_TOKEN", "dummy-not-a-real-token")   # import never connects

    def fake_load_dotenv(*a, **kw):
        # python-dotenv's real rule: a variable already in the environment is NOT overridden
        for k, v in file_values.items():
            os.environ.setdefault(k, v)
        return True

    saved_env = dict(os.environ)
    # exit(1) in the bot is site's Quitter, which CLOSES sys.stdin before raising SystemExit -
    # our command channel. So the bot gets a stand-in to close instead.
    saved = (dotenv.load_dotenv, dotenv.dotenv_values, sys.stdout, sys.stderr, sys.stdin)
    out, err = io.StringIO(), io.StringIO()
    code, result, amy = 0, None, None
    try:
        # exactly the parent's environment: unnamed settings are absent, not inherited from us
        os.environ.clear()
        os.environ.update(scenario["env"])
        isolate_db()
        dotenv.load_dotenv = fake_load_dotenv
        dotenv.dotenv_values = lambda *a, **kw: dict(file_values)
        sys.stdout, sys.stderr, sys.stdin = out, err, io.StringIO()
        spec = importlib.util.spec_from_file_location("amy", os.path.join(PROJ, "Amy_chatbot_V2.py"))
        assert spec is not None and spec.loader is not None
        amy = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(amy)
        result = _report(amy)
    except SystemExit as e:
        code = e.code if isinstance(e.code, int) else 1
    except BaseException:
        code = 1
        traceback.print_exc(file=err)
    finally:
        sys.stdout, sys.stderr, sys.stdin = saved[2], saved[3], saved[4]
        dotenv.load_dotenv, dotenv.dotenv_values = saved[0], saved[1]
        # Release what the import opened, so the file is free for the parent to read/delete
        # and the next scenario starts with only the handlers it installs itself.
        root = logging.getLogger()
        for handler in list(root.handlers):
            if getattr(handler, "_amy_handler", False):
                root.removeHandler(handler)
                handler.close()
        conn = getattr(getattr(amy, "db", None), "conn", None)
        if conn is not None:
            conn.close()
        os.environ.clear()
        os.environ.update(saved_env)
    return {"code": code, "result": result, "output": out.getvalue() + err.getvalue()}


def _worker_main():
    real_stdout = sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    sys.path.insert(0, PROJ)
    sys.path.insert(0, HERE)
    os.chdir(PROJ)
    for line in sys.stdin:
        if line.strip():
            reply = _run_scenario(json.loads(line), real_stdout)
            real_stdout.write(REPLY + json.dumps(reply) + "\n")
            real_stdout.flush()


if __name__ == "__main__":
    _worker_main()
