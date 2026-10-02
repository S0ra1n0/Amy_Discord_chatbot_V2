"""
logs.py on its own: where log lines go, what reaches the file, and rotation.

Everything used to go through print(), so an overnight problem left nothing behind once the
console closed. These pin the promises the file makes - above all that DEBUG lines, which
carry the full text of chat messages, never reach disk.
"""
import io
import logging
import os
import sys
import tempfile

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJ)

import logs
from _check import check, finish

assert "discord" not in sys.modules and "ollama" not in sys.modules, \
    "logs.py must stay importable without Discord or Ollama"

tmp = tempfile.TemporaryDirectory()
log_path = os.path.join(tmp.name, "amy.log")


def capture_console():
    """Point the console handler at a buffer we can read."""
    buf = io.StringIO()
    for h in logging.getLogger().handlers:
        if isinstance(h, logs._SafeConsoleHandler):
            h.setStream(buf)
    return buf


def file_text(path=log_path):
    for h in logging.getLogger().handlers:
        h.flush()
    with open(path, encoding="utf-8") as f:
        return f.read()


def close_ours():
    root = logging.getLogger()
    for h in list(root.handlers):
        if getattr(h, logs._MARK, False):
            root.removeHandler(h)
            h.close()


print("=== console keeps the old format; the file adds time and source ===")
check("setup reports no problem", logs.setup_logging(log_path) is None)
console = capture_console()
log = logging.getLogger("amy.test")
log.info("hello there")
log.warning("careful")
check("console line is '[INFO] message'", "[INFO] hello there\n" in console.getvalue(),
      console.getvalue())
check("console shows warnings as '[WARNING] ...'", "[WARNING] careful" in console.getvalue())
text = file_text()
check("the file has the line", "hello there" in text, text)
check("the file names the logger", "amy.test" in text, text)
check("the file is timestamped", text[:4].isdigit(), text[:30])

print("=== DEBUG reaches the console but never the file ===")
log.debug("Message received from someone: my secret plans")
check("console shows DEBUG", "my secret plans" in console.getvalue())
check("the file does not", "my secret plans" not in file_text(), file_text()[-200:])

print("=== exceptions keep their traceback in the file ===")
try:
    raise ValueError("boom")
except ValueError:
    log.exception("it broke")
text = file_text()
check("traceback recorded", "Traceback" in text and "ValueError: boom" in text, text[-300:])

print("=== setting up again replaces, never doubles ===")
logs.setup_logging(log_path)
console = capture_console()
log.info("once only")
check("one console line per record", console.getvalue().count("once only") == 1,
      console.getvalue())
check("one file line per record", file_text().count("once only") == 1)
ours = [h for h in logging.getLogger().handlers if getattr(h, logs._MARK, False)]
check("exactly two handlers installed (console + file)", len(ours) == 2, ours)

print("=== the file rotates instead of growing forever ===")
logs.setup_logging(log_path)
capture_console()
line = "x" * 200
for _ in range(int(logs.MAX_BYTES / 200) + 50):
    log.info(line)
check("a rotated file exists", os.path.exists(log_path + ".1"))
check("the live file stays under the cap", os.path.getsize(log_path) <= logs.MAX_BYTES + 400,
      os.path.getsize(log_path))
check("at most BACKUPS old files are kept",
      not os.path.exists(log_path + ".%d" % (logs.BACKUPS + 1)))

print("=== no file, or an unusable one, still logs to the console ===")
check("blank path means console only", logs.setup_logging("") is None)
check("no file handler then", not any(
    isinstance(h, logging.FileHandler) for h in logging.getLogger().handlers))
bad = os.path.join(tmp.name, "no", "such", "dir", "amy.log")
problem = logs.setup_logging(bad)
check("an unopenable path is reported, not raised", problem is not None and bad in problem, problem)
console = capture_console()
log.info("still here")
check("and the console still works", "still here" in console.getvalue())

print("=== a console that can't encode a character doesn't lose the line ===")
logs.setup_logging("")
raw = io.BytesIO()
cp1252 = io.TextIOWrapper(raw, encoding="cp1252")   # what a Windows console often is
for h in logging.getLogger().handlers:
    if isinstance(h, logs._SafeConsoleHandler):
        h.setStream(cp1252)
log.info("Now playing: \U0001F3B5 日本")
cp1252.flush()
out = raw.getvalue().decode("utf-8", errors="replace")
check("the line is written (as UTF-8) instead of a logging error", "Now playing" in out, out)

print("=== chatty libraries are quietened ===")
check("discord.py below INFO is dropped", logging.getLogger("discord").level == logging.INFO)
check("httpx request lines are dropped", logging.getLogger("httpx").level == logging.WARNING)
check("asyncio's loop announcements are dropped", logging.getLogger("asyncio").level == logging.INFO)

close_ours()
finish("ALL LOGGING TESTS PASSED")
