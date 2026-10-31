"""
Discord markdown escaping. Pure, stdlib only, so every module (music, ui, lyrics callers) can
use it without importing the others.

Anything a stranger controls (a YouTube title, a community-submitted lyrics record) must be
escaped before it goes into an embed description or message, or "[Free Nitro](https://x)"
renders as a masked link and "||x||" as a spoiler.
"""
import re

_MD_CHARS = re.compile(r"([\\*_~`|\[\]<>])")
_MD_LINE_START = re.compile(r"^(\s*)([#>-])", re.M)


def escape_markdown(text: str) -> str:
    """
    Make community-submitted text render literally in an embed: no masked links
    ("[x](https://...)"), spoilers, emphasis, code, headings or list markers. Pure.
    """
    escaped = _MD_CHARS.sub(lambda m: "\\" + m.group(1), text)
    return _MD_LINE_START.sub(lambda m: m.group(1) + "\\" + m.group(2), escaped)
