"""
Song lyrics from LRCLIB (https://lrclib.net): free, no API key.

Pure helpers (track title -> search terms, choosing among results, stripping timestamps,
splitting into Discord-sized pages) plus the one network call, `search_lrclib`. Lyrics are
only ever shown in a text embed; Amy never speaks them.

The API (docs: https://lrclib.net/docs): `GET /api/search?track_name=&artist_name=` or
`?q=` returns a list of records with `trackName`, `artistName`, `duration` (seconds),
`instrumental`, `plainLyrics` and `syncedLyrics`. It asks clients for a descriptive
User-Agent.
"""
import asyncio
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import httpx

API_URL = "https://lrclib.net/api/search"
USER_AGENT = "Amy-Discord-Bot/1.0 (personal Discord music bot; lyrics lookup)"
TIMEOUT = 8.0                  # seconds PER SEARCH (find_lyrics may make two; httpx's own limit is per read)
DURATION_TOLERANCE = 5         # seconds: a result this close to the track's length is "the same song"
PAGE_CHARS = 4000              # under the 4096 embed limit, leaving room for the footer note
MAX_PAGES = 3                  # ~12k characters; the rest is left to lrclib.net
MAX_TERMS_CHARS = 200
CONTINUES_NOTE = "...lyrics continue on lrclib.net"


class LyricsError(Exception):
    """The lookup itself failed (network, timeout, a reply that isn't JSON). Not 'no match'."""


@dataclass(frozen=True)
class Terms:
    """What to search for: an artist when the title had one, and the song title."""
    artist: Optional[str]
    title: str

    @property
    def q(self) -> str:
        return "%s %s" % (self.artist, self.title) if self.artist else self.title

    @property
    def shown(self) -> str:
        return "%s - %s" % (self.artist, self.title) if self.artist else self.title


@dataclass(frozen=True)
class Found:
    """The chosen record. `text` is None for an instrumental."""
    artist: str
    title: str
    text: Optional[str]
    instrumental: bool = False

    @property
    def heading(self) -> str:
        return "%s - %s" % (self.artist, self.title) if self.artist else self.title


# ---- Title -> search terms ----------------------------------------------------------------------
# Brackets of every kind YouTube titles use, including the Japanese ones: "【Teto SV2】".
_BRACKETED = re.compile(r"\s*(?:\([^)]*\)|\[[^\]]*\]|\{[^}]*\}|【[^】]*】|〔[^〕]*〕|（[^）]*）)")
_QUOTES = re.compile(r"[「」『』\"“”]")            # keep what is inside: 「Song」 is the title
_SPLIT_BAR = re.compile(r"\s+(?:\||//)\s+")
_DASH = re.compile(r"\s+[-–—]\s+")
_FEAT = re.compile(r"\s+(?:feat|ft|featuring)\b\.?.*$", re.I)
_LOOSE_NOISE = re.compile(
    r"\b(?:official\s+(?:music\s+)?(?:video|audio|mv|lyric\s+video)|lyric\s+video|"
    r"lyrics?\s+video|music\s+video|official\s+visuali[sz]er|visuali[sz]er|"
    r"full\s+version|hd|4k)\b", re.I)


def _tidy(text: str) -> str:
    text = _QUOTES.sub(" ", _FEAT.sub("", _LOOSE_NOISE.sub("", text)))
    return re.sub(r"\s+", " ", text).strip(" -–—|:,~・")


_EXT = re.compile(r"\.[A-Za-z0-9]{1,5}$")
_TRACK_NO = re.compile(r"^\d{1,2}\s*[-.]\s*")      # 1-2 digits: "311 - Amber" is an artist


def local_title(filename: str) -> str:
    """
    A local file's name ("01 - Artist - Song.flac") as a title for `search_terms`: the
    extension and a leading track number dropped. Pure. Falls back to the name as given.
    """
    name = _EXT.sub("", filename.strip())
    return _TRACK_NO.sub("", name).strip() or filename.strip()


def search_terms(title: str) -> Terms:
    """
    Turn a track title ("Artist - Song (Official Video)") into search terms. Pure.

    Brackets (ASCII and 【】-style) and "feat." credits are dropped, anything after " | " or
    " // " too. A " - " splits artist from song; with no dash there is only a title. When
    cleaning leaves nothing, the raw title is used so a search still has something to go on.
    """
    raw = re.sub(r"\s+", " ", title).strip()
    t = _SPLIT_BAR.split(raw, maxsplit=1)[0]
    t = _BRACKETED.sub("", t)
    parts = _DASH.split(t, maxsplit=1)
    artist: Optional[str] = None
    if len(parts) == 2:
        artist, song = _tidy(parts[0]) or None, _tidy(parts[1])
    else:
        song = _tidy(parts[0])
    if not song:
        # "Artist - (Official Video)" or a title that was all brackets
        song, artist = (artist or _tidy(raw) or raw), None
    return Terms(artist[:MAX_TERMS_CHARS] if artist else None, song[:MAX_TERMS_CHARS])


# ---- Choosing among results ---------------------------------------------------------------------
# Spaces and tabs only: `\s` would eat the newlines and lose stanza breaks.
_STAMPS = re.compile(r"^[ \t]*(?:\[\d+:\d+(?:[.:]\d+)?\][ \t]*)+|<\d+:\d+(?:[.:]\d+)?>[ \t]*", re.M)
_TAG_LINE = re.compile(r"^[ \t]*\[[A-Za-z]+:[^\]]*\][ \t]*$", re.M)     # [ar:Artist] [length:3:20]


def strip_timestamps(synced: str) -> str:
    """Synced lyrics ("[01:23.45] words") as plain lines; stanza breaks are kept. Pure."""
    text = _STAMPS.sub("", _TAG_LINE.sub("", synced.replace("\r\n", "\n").replace("\r", "\n")))
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _text_of(rec: Dict[str, Any]) -> str:
    plain = rec.get("plainLyrics")
    if isinstance(plain, str) and plain.strip():
        return plain.strip()
    synced = rec.get("syncedLyrics")
    if isinstance(synced, str) and synced.strip():
        return strip_timestamps(synced)
    return ""


def _usable(rec: Any) -> bool:
    return isinstance(rec, dict) and (rec.get("instrumental") is True or bool(_text_of(rec)))


def names_match(rec: Dict[str, Any], terms: Terms) -> bool:
    """
    Does a record loosely match what was searched? Pure. Casefolded, and either side may
    contain the other. With an artist in the terms, the ARTIST must match (the record's artist
    or, for a title ordered "Song - Artist", its track name), so a same-titled cover by someone
    else is out; with no artist, the title (or the record's artist) may match.
    """
    ours = [s.casefold().strip() for s in ((terms.artist,) if terms.artist else (terms.title,))
            if s and s.strip()]
    theirs = [str(rec.get(k) or "").casefold().strip() for k in ("artistName", "trackName")]
    return any(a and b and (a in b or b in a) for a in ours for b in theirs)


def choose_result(results: List[Dict[str, Any]], duration: Optional[float] = None,
                  strict: bool = False) -> Optional[Found]:
    """
    Pick the record to show, or None when nothing usable came back. Pure.

    A usable record has lyrics or is flagged instrumental. With a known duration, the one
    closest to it within DURATION_TOLERANCE seconds wins (the same song, not a 10-hour loop
    or a live cut); otherwise, or when none is that close, the first usable record. With
    `strict`, only a record within the tolerance counts (None otherwise).
    """
    usable = [r for r in results if _usable(r)]
    if not usable:
        return None
    pick = usable[0]
    if strict and not duration:
        return None
    if duration:
        near = [(abs(float(r["duration"]) - duration), i) for i, r in enumerate(usable)
                if isinstance(r.get("duration"), (int, float))
                and abs(float(r["duration"]) - duration) <= DURATION_TOLERANCE]
        if near:
            pick = usable[min(near)[1]]
        elif strict:
            return None
    text = _text_of(pick)
    return Found(artist=str(pick.get("artistName") or "").strip(),
                 title=str(pick.get("trackName") or "").strip() or "Unknown title",
                 text=text or None,
                 instrumental=pick.get("instrumental") is True and not text)


# ---- Pages --------------------------------------------------------------------------------------
def split_pages(text: str, limit: int = PAGE_CHARS) -> List[str]:
    """
    Cut text into pieces of at most `limit` characters, on line boundaries. Pure.

    A single line longer than the limit is cut at its last space (or hard, with none), never
    right after a lone backslash (that would separate it from the character it escapes). The
    rest of a cut line starts a new page, so a leading `#`, `>` or `-` there gets a backslash
    (it was not at a line start when the text was escaped). Pieces are stripped; no text is
    lost apart from that edge whitespace. `limit` must be at least 2 (a continuation
    backslash plus one character would never fit in 1).
    """
    if limit < 2:
        raise ValueError("limit must be at least 2")
    pages: List[str] = []
    current = ""
    for line in text.strip().split("\n"):
        while len(line) > limit:
            cut = line.rfind(" ", 0, limit + 1)
            cut = cut if cut > 0 else limit
            head = line[:cut]
            if (len(head) - len(head.rstrip("\\"))) % 2 == 1 and cut > 1:
                cut -= 1
            if current:
                pages.append(current)
                current = ""
            pages.append(line[:cut].strip())
            line = line[cut:].strip()
            if line[:1] in ("#", ">", "-"):
                line = "\\" + line
        if current and len(current) + 1 + len(line) > limit:
            pages.append(current)
            current = line
        else:
            current = current + "\n" + line if current else line
    if current:
        pages.append(current)
    return [p.strip() for p in pages if p.strip()]


_MD_CHARS = re.compile(r"([\\*_~`|\[\]<>])")
_MD_LINE_START = re.compile(r"^(\s*)([#>-])", re.M)


def escape_markdown(text: str) -> str:
    """
    Make community-submitted text render literally in an embed: no masked links
    ("[x](https://...)"), spoilers, emphasis, code, headings or list markers. Pure.
    """
    escaped = _MD_CHARS.sub(lambda m: "\\" + m.group(1), text)
    return _MD_LINE_START.sub(lambda m: m.group(1) + "\\" + m.group(2), escaped)


def lyric_pages(text: str, limit: int = PAGE_CHARS,
                max_pages: int = MAX_PAGES) -> Tuple[List[str], bool]:
    """
    The pages to show and whether more were left out. Pure. The text is escaped BEFORE it is
    split, because escaping adds characters and every page must still fit `limit`.
    """
    pages = split_pages(escape_markdown(text), limit)
    return pages[:max_pages], len(pages) > max_pages


# ---- The network --------------------------------------------------------------------------------
async def search_lrclib(params: Dict[str, str],
                        client: Optional[httpx.AsyncClient] = None) -> List[Dict[str, Any]]:
    """
    One LRCLIB search. Raises LyricsError for anything but a clean answer (an empty list is a
    clean answer). The whole call is bounded by TIMEOUT. `client` is for tests.
    """
    async def go(c: httpx.AsyncClient) -> List[Dict[str, Any]]:
        resp = await c.get(API_URL, params=params, headers={"User-Agent": USER_AGENT})
        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, list):
            raise ValueError("unexpected reply shape")
        return [r for r in data if isinstance(r, dict)]

    try:
        if client is not None:
            return await asyncio.wait_for(go(client), TIMEOUT)
        async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=False) as c:
            return await asyncio.wait_for(go(c), TIMEOUT)
    except (httpx.HTTPError, asyncio.TimeoutError, ValueError) as e:
        raise LyricsError("%s: %s" % (type(e).__name__, e)) from e


async def find_lyrics(terms: Terms, duration: Optional[float] = None) -> Optional[Found]:
    """
    Look a song up. Returns None for 'no match'; raises LyricsError when LRCLIB can't be
    reached. With a known duration a record within DURATION_TOLERANCE from either search beats the
    first search's out-of-tolerance guess (from the free-text search only if its artist or title
    loosely matches the terms). Each search has its own TIMEOUT. With an artist it asks for that exact pair first, then falls back to a free-text
    search of both words (a title ordered "Song - Artist" matches that way).
    """
    attempts: List[Dict[str, str]] = []
    if terms.artist:
        attempts.append({"track_name": terms.title, "artist_name": terms.artist})
    attempts.append({"q": terms.q})
    fallback: Optional[Found] = None
    for params in attempts:
        results = await search_lrclib(params)
        if not duration:
            found = choose_result(results)
            if found is not None:
                return found
            continue
        # Duration known: a record of the right length from ANY attempt beats the first
        # attempt's best guess (a live cut), so keep looking until one matches.
        # ...but in the free-text round only one whose names loosely match what was asked
        # (a cover by someone else of the right length is not the song).
        candidates = results if "q" not in params else [r for r in results if names_match(r, terms)]
        found = choose_result(candidates, duration, strict=True)
        if found is not None:
            return found
        if fallback is None:
            # The duration preference only ever applies to name-matched records
            fallback = choose_result(candidates, duration) or choose_result(results)
    return fallback
