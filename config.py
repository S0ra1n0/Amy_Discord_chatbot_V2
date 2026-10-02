# config.py
"""
Reading Amy's settings from the environment / .env, without crashing or misreading.

Pure functions only - no I/O - so every rule here is tested offline in tests/test_config.py.
Each parser returns (value, problem): `problem` is a ready-to-log sentence when the raw
value couldn't be used, else None. The caller logs it; nothing here prints.

Why this exists: a typo in .env used to either crash the bot at import with a bare
ValueError traceback (HISTORY_LIMIT=abc) or be misread silently - WEB_SEARCH=ture turned
web search OFF, because anything that wasn't an exact "true" word counted as false.
"""

from typing import Iterable, List, Mapping, Optional, Tuple

TRUE_WORDS = ("1", "true", "yes", "on")
FALSE_WORDS = ("0", "false", "no", "off")


def parse_bool(name: str, raw: Optional[str], default: bool) -> Tuple[bool, Optional[str]]:
    """
    Read a true/false setting. Blank or unset means `default`.

    An unrecognised word keeps the default and says so - it is NOT read as false. The old
    parser did exactly that, so WEB_SEARCH=ture quietly switched search off.
    """
    if raw is None or not raw.strip():
        return default, None
    word = raw.strip().lower()
    if word in TRUE_WORDS:
        return True, None
    if word in FALSE_WORDS:
        return False, None
    fallback = "true" if default else "false"
    return default, (f"{name}={raw.strip()!r} isn't true or false (use true/false, yes/no "
                     f"or on/off); using {fallback}.")


def parse_int(name: str, raw: Optional[str], default: int,
              minimum: int) -> Tuple[int, Optional[str]]:
    """
    Read a whole-number setting. Blank or unset means `default`.

    A non-number keeps the default; a number below `minimum` is raised to it. Either way
    the problem is reported rather than crashing the bot at startup.
    """
    if raw is None or not raw.strip():
        return default, None
    try:
        value = int(raw.strip())
    except ValueError:
        return default, f"{name}={raw.strip()!r} isn't a whole number; using {default}."
    if value < minimum:
        return minimum, f"{name}={value} is below the minimum of {minimum}; using {minimum}."
    return value, None


def shadowed_settings(env_before_dotenv: Mapping[str, str],
                      dotenv_file: Mapping[str, Optional[str]]) -> List[str]:
    """
    Settings defined in .env that are ALSO set, differently, in the system environment.

    python-dotenv never overrides a variable that already exists, so for these the .env
    line is silently ignored. OLLAMA_KEEP_ALIVE is the realistic case - Ollama reads the
    same name, and its install guide has you set it system-wide. Returns names only: the
    values may be secrets (DISCORD_TOKEN), so callers must never log them.
    """
    shadowed: Iterable[str] = (
        name for name, file_value in dotenv_file.items()
        if file_value is not None and name in env_before_dotenv
        and env_before_dotenv[name] != file_value
    )
    return sorted(shadowed)
