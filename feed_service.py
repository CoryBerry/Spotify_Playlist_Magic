"""Feed Radar — the "watch a blog, harvest Artist - Title lines" layer.

Thin-slice prototype. Two responsibilities, kept apart on purpose:

  1. Fetch a page's text via the Firecrawl CLI (side-effecting, needs network + auth).
  2. Extract candidate "Artist - Title" lines from that text (pure, unit-tested).

Keeping the extractor pure means the interesting logic is testable without Firecrawl
auth or a network round-trip (see test_feed_service.py). The fetch layer shells out to
the globally-installed `firecrawl` CLI so we reuse whatever login the user already did
(`firecrawl login`) instead of threading an API key through the Flask app.

Downstream, app.py hands the extracted lines to spotify_service.resolve_tracks() — the
exact same resolver Text Import uses — so a blog line becomes a Spotify URI through one
code path, not two.
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess

# Hardcoded sources for the prototype. A real version would store these in a table and
# let the user add/remove them. Each declares an extraction `mode`:
#   "anchors" — cheap regex over markdown link anchors (1 Firecrawl credit, ~fast).
#               Works when a blog titles posts as `[Artist – Title](url)` (e.g. GvB).
#   "llm"     — Firecrawl structured extraction with a schema (~5 credits, ~2 min).
#               Layout-agnostic: reads artist/title even from prose captions, so it
#               handles the blogs whose markup the regex can't (Pitchfork, Stereogum...).
FEED_SOURCES = [
    {"id": "gvb",  "name": "Gorilla vs. Bear",             "url": "https://www.gorillavsbear.net/",              "mode": "anchors"},
    {"id": "p4k",  "name": "Pitchfork — Best New Tracks",  "url": "https://pitchfork.com/reviews/tracks/",       "mode": "llm"},
    {"id": "lobf", "name": "Line of Best Fit — New Music", "url": "https://www.thelineofbestfit.com/new-music",  "mode": "llm"},
]


def get_source(source_id: str) -> dict | None:
    return next((s for s in FEED_SOURCES if s["id"] == source_id), None)


def item_hash(source_id: str, raw_line: str) -> str:
    """Stable id for a harvested line so the same post is never queued twice."""
    norm = " ".join(raw_line.lower().split())
    return hashlib.sha1(f"{source_id}\n{norm}".encode("utf-8")).hexdigest()


# --- Fetch (side-effecting) -------------------------------------------------

class FeedFetchError(RuntimeError):
    """Firecrawl scrape failed (not installed, not authenticated, or network/HTTP error)."""


# Schema for the "llm" extraction mode: a flat list of {artist, title}. Passed to
# Firecrawl's structured extraction so its model fills it from the page, however the
# page is laid out. Inline JSON string — subprocess gets it as one argv element, so
# there is no shell-quoting to worry about.
_TRACK_SCHEMA = (
    '{"type":"object","properties":{"tracks":{"type":"array","items":{"type":"object",'
    '"properties":{"artist":{"type":"string"},"title":{"type":"string"}},'
    '"required":["artist","title"]}}},"required":["tracks"]}'
)

# The CLI prints this login prompt to stdout (exit 0) when run non-interactively without
# auth, so we sniff for it and report "not authenticated" instead of parsing it as content.
_AUTH_PROMPT = re.compile(r"authenticate|Login with browser|Enter choice|FIRECRAWL_API_KEY")


def _run_firecrawl(args: list[str], timeout: int) -> str:
    """Run the Firecrawl CLI and return stdout, or raise FeedFetchError with a clear reason."""
    # shutil.which honors PATHEXT, so it resolves the npm `firecrawl.cmd` shim on Windows
    # where a bare "firecrawl" arg to subprocess would not. encoding="utf-8" is load-bearing:
    # the default on Windows is cp1252, which mangles every en-dash/accent in scraped text.
    exe = shutil.which("firecrawl")
    if not exe:
        raise FeedFetchError("Firecrawl CLI not found on PATH. Install it, then `firecrawl login`.")
    try:
        proc = subprocess.run(
            [exe, *args], capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise FeedFetchError(f"Firecrawl timed out after {timeout}s.")

    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        if any(k in err.lower() for k in ("auth", "login", "api key")):
            raise FeedFetchError("Firecrawl is not authenticated. Run `firecrawl login` once, then retry.")
        raise FeedFetchError(f"Firecrawl failed: {err[:300] or 'unknown error'}")

    out = proc.stdout.strip()
    if not out:
        raise FeedFetchError("Firecrawl returned empty output.")
    if _AUTH_PROMPT.search(out):
        raise FeedFetchError("Firecrawl is not authenticated. Run `firecrawl login` once, then retry.")
    return out


def fetch_markdown(url: str, timeout: int = 90) -> str:
    """Return a page's main-content markdown via the Firecrawl CLI."""
    return _run_firecrawl(["scrape", url, "--format", "markdown", "--only-main-content"], timeout)


def fetch_tracks_llm(url: str, timeout: int = 280) -> list[str]:
    """Return "Artist - Title" lines via Firecrawl structured extraction (the "llm" mode).

    Slower and pricier than a plain scrape, but layout-agnostic — it reads artist/title
    even when they only appear in a prose headline, which the anchor regex can't. Output
    is normalized to the same "Artist - Title" shape the resolver expects.
    """
    out = _run_firecrawl(["scrape", url, "--format", "json", "--schema", _TRACK_SCHEMA], timeout)
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        raise FeedFetchError("Firecrawl returned output that was not valid JSON.")
    tracks = ((data.get("json") or {}).get("tracks")) or []
    lines, seen = [], set()
    for t in tracks:
        artist = (t.get("artist") or "").strip()
        title = (t.get("title") or "").strip()
        if not (artist and title):
            continue
        norm = f"{artist} - {title}"
        if norm.lower() not in seen:
            seen.add(norm.lower())
            lines.append(norm)
    return lines


def harvest(source: dict) -> list[str]:
    """Return candidate "Artist - Title" lines for a source, per its extraction mode."""
    if source.get("mode") == "llm":
        return fetch_tracks_llm(source["url"])
    return extract_lines(fetch_markdown(source["url"]))


# --- Extract (pure) ---------------------------------------------------------

# "Artist – Title" where the dash is hyphen, en-dash, or em-dash, optionally with the
# title in straight or curly quotes. Music blogs overwhelmingly write new-track posts
# this way ("Artist – 'Song'"), which makes a boring regex a surprisingly good v1.
_QUOTED = re.compile(
    r'^(?P<artist>.{2,60}?)\s+[-–—]\s+["“](?P<title>.{1,80}?)["”]\s*$'
)
_BARE = re.compile(
    r'^(?P<artist>.{2,60}?)\s+[-–—]\s+(?P<title>.{2,80}?)\s*$'
)

# Markdown link anchor text containing a dash: `[Artist – Title](url)`. Music blogs
# (GvB, Stereogum, Pitchfork...) title posts this way, so the anchor text is the single
# richest signal on the page. `[^\[\]]` keeps it to one bracket pair, which also pulls the
# alt text out of image links `[![Artist – Title](img)](post)` without tripping on nesting.
_LINK_ANCHOR = re.compile(r'\[([^\[\]]*?[-–—][^\[\]]*?)\]')

# Emphasis / leftover markdown to strip from an anchor or line before classifying.
_MD_NOISE = re.compile(r'(\*\*|__|[*_`>#!])')

# Candidates that are obviously navigation/boilerplate, not tracks.
_JUNK = re.compile(
    r'https?://|@|\b(menu|search|subscribe|newsletter|tags?|share|comments?|'
    r'privacy|terms|spotify|apple music|bandcamp|soundcloud|playlist)\b', re.I)


def _clean(text: str) -> str:
    return " ".join(_MD_NOISE.sub("", text).split()).strip()


def _classify(text: str) -> tuple[str, str] | None:
    """Return ("quoted"|"bare", "Artist - Title") if text looks like a track, else None."""
    if not text or len(text) > 90 or _JUNK.search(text):
        return None
    m = _QUOTED.match(text)
    if m:
        return "quoted", f"{m.group('artist').strip()} - {m.group('title').strip()}"
    m = _BARE.match(text)
    if m:
        title = m.group("title").strip()
        # Bare "X - Y" is the false-positive-prone path, so demand a title-looking title:
        # must start with a letter. Kills "Posted 12 - 15 in the archive" and numeric
        # ranges, at the cost of bare numeric titles like "1979" (still caught if quoted).
        if title[:1].isalpha():
            return "bare", f"{m.group('artist').strip()} - {title}"
    return None


def extract_lines(markdown: str, limit: int = 40) -> list[str]:
    """Pull candidate "Artist - Title" strings from page markdown.

    Two passes, both funneled through _classify: (1) markdown link anchor text — the
    strongest signal, since blogs title posts as `[Artist – Title](url)`; (2) whole
    lines — catches plain-text blogs with no links. Returns normalized "Artist - Title"
    strings (single hyphen, so they drop straight into spotify_service._search_line,
    which splits on " - "). Deduped, order-preserving, capped at `limit`. Quoted-title
    matches lead bare ones, since a bare "X - Y" is likelier a false positive.
    """
    seen: set[str] = set()
    quoted: list[str] = []
    bare: list[str] = []

    def _take(text: str) -> None:
        got = _classify(text)
        if not got:
            return
        kind, norm = got
        key = norm.lower()
        if key not in seen:
            seen.add(key)
            (quoted if kind == "quoted" else bare).append(norm)

    for anchor in _LINK_ANCHOR.findall(markdown):
        _take(_clean(anchor))
    for raw in markdown.splitlines():
        # Pass 1 already mined every link anchor; a line with a link here would only
        # re-add it (often as concatenated image-alt + text). Pass 2 is purely for
        # plain-text blogs, so skip any line that contains a markdown link.
        if "](" in raw:
            continue
        _take(_clean(raw))

    return (quoted + bare)[:limit]
