"""
Import the bot once, in its own process, with a simulated .env, and report what it resolved.

Settings are read when the bot is imported, so each scenario needs a fresh interpreter.
test_config.py runs this script in a subprocess. The scenario arrives as JSON in the
AMY_PROBE_SCENARIO environment variable:

    {"dotenv": {...name: value...}}   - what a .env file would contain

Anything else (a pre-set OLLAMA_KEEP_ALIVE, say) is simply passed in the subprocess's
environment, which is exactly where a system-wide setting would come from.
"""
import importlib.util
import io
import json
import os
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJ)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(PROJ)

from _fakes import isolate_db

isolate_db()
scenario = json.loads(os.environ.get("AMY_PROBE_SCENARIO", "{}"))
file_values = dict(scenario.get("dotenv", {}))
file_values.setdefault("DISCORD_TOKEN", "dummy-not-a-real-token")   # import never connects

import dotenv


def _fake_load_dotenv(*a, **kw):
    # python-dotenv's real rule: a variable already in the environment is NOT overridden
    for k, v in file_values.items():
        os.environ.setdefault(k, v)
    return True


dotenv.load_dotenv = _fake_load_dotenv
dotenv.dotenv_values = lambda *a, **kw: dict(file_values)

spec = importlib.util.spec_from_file_location("amy", os.path.join(PROJ, "Amy_chatbot_V2.py"))
amy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(amy)

print("PROBE_RESULT " + json.dumps({
    "HISTORY_LIMIT": amy.HISTORY_LIMIT,
    "db_max_messages": amy.db.max_messages,
    "DB_PRUNE_DAYS": amy.DB_PRUNE_DAYS,
    "WEB_SEARCH": amy.WEB_SEARCH,
    "OLLAMA_THINK": amy.OLLAMA_THINK,
    "OLLAMA_KEEP_ALIVE": amy.OLLAMA_KEEP_ALIVE,
}))
