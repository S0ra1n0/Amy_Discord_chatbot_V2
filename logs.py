"""
Logging: the console as it always looked, plus a rotating log file.

Everything used to go through print(), so an overnight problem left no trace once the console
window closed. Modules now log with `logging.getLogger("amy.<area>")` and this sets up where
it goes:

- Console - "[LEVEL] message", the format the bot always printed, down to DEBUG.
- File    - timestamped, INFO and up, rotated at MAX_BYTES with BACKUPS old files kept.
            DEBUG stays off disk on purpose: those lines carry the full text of every chat
            message, which belongs in a live console, not a file that accumulates for weeks.

No Discord or Ollama imports; testable on its own (tests/test_logs.py).
"""
import logging
import logging.handlers
import sys
from typing import Optional

CONSOLE_FORMAT = "[%(levelname)s] %(message)s"
FILE_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
MAX_BYTES = 1_000_000      # ~10k lines; rotates to amy.log.1 ... amy.log.5
BACKUPS = 5
FILE_LEVEL = logging.INFO

# Libraries that are chatty below these levels. discord.py at DEBUG logs raw gateway payloads;
# httpx at INFO logs every request Amy's web search makes; asyncio at DEBUG announces its
# event loop on every start.
QUIET = {"discord": logging.INFO, "httpx": logging.WARNING, "httpcore": logging.WARNING,
         "asyncio": logging.INFO}

_MARK = "_amy_handler"     # tags our handlers so a second setup replaces rather than doubles


class _SafeConsoleHandler(logging.StreamHandler):
    """
    A console handler that survives characters the console can't encode.

    The Windows console is often cp1252; a song title or reply with emoji would otherwise
    raise UnicodeEncodeError and logging would print a "--- Logging error ---" block instead
    of the line. This writes UTF-8 bytes in that case, as the old safe_print did.
    """
    def emit(self, record: logging.LogRecord) -> None:
        try:
            text = self.format(record) + self.terminator
            try:
                self.stream.write(text)
            except UnicodeEncodeError:
                buffer = getattr(self.stream, "buffer", None)
                if buffer is None:
                    raise
                buffer.write(text.encode("utf-8", errors="replace"))
            self.flush()
        except Exception:
            self.handleError(record)


def setup_logging(log_file: Optional[str]) -> Optional[str]:
    """
    Route all logging to the console and, if `log_file` is set, a rotating file.

    Safe to call again: it replaces the handlers it installed before. Returns a problem
    message if the file couldn't be opened (logging then continues to the console only), or
    None. Never raises - failing to log must not stop the bot.
    """
    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, _MARK, False):
            root.removeHandler(handler)
            handler.close()
    root.setLevel(logging.DEBUG)

    console = _SafeConsoleHandler(sys.stdout)
    console.setFormatter(logging.Formatter(CONSOLE_FORMAT))
    setattr(console, _MARK, True)
    root.addHandler(console)

    for name, level in QUIET.items():
        logging.getLogger(name).setLevel(level)

    if not log_file:
        return None
    try:
        handler = logging.handlers.RotatingFileHandler(
            log_file, maxBytes=MAX_BYTES, backupCount=BACKUPS, encoding="utf-8")
    except OSError as e:
        # Plain quotes, not !r: repr doubles every backslash in a Windows path.
        return (f"Couldn't open the log file '{log_file}' ({e.strerror or e}); "
                f"logging to the console only.")
    handler.setLevel(FILE_LEVEL)
    handler.setFormatter(logging.Formatter(FILE_FORMAT))
    setattr(handler, _MARK, True)
    root.addHandler(handler)
    return None
