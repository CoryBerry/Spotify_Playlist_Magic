"""Headless Last.fm enrichment service — read-only, key-only, no user auth.

This module is the seam between "an artist/track name" and "what Last.fm knows about it"
(tags, similar artists, stats). Like ``spotify_service.py`` it is deliberately decoupled from
the Flask app: it imports no Flask, no SQLAlchemy, and reads no session. That lets ``cli.py``,
the mix tooling, and anything else pull enrichment without spinning up the web app.

Auth model: Last.fm's read methods (``artist.getInfo``, ``artist.getSimilar``,
``artist.getTopTags``, ``track.getInfo`` …) only need the public API *key* — no callback, no
signed session. So there is nothing to cache and nothing to prompt for; if ``LASTFM_API_KEY``
is set we can call. (The secret is only needed for signed/authenticated write methods like
scrobbling, which this module deliberately does not do.)

Caching: responses are stable, so we memoize to a small JSON file anchored to this file. That
keeps repeated enrichment (e.g. fanning a mix out over a playlist's artists) from hammering the
API. Pass ``use_cache=False`` to bypass.
"""
from __future__ import annotations

import atexit
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Optional

API_ROOT = "https://ws.audioscrobbler.com/2.0/"

# Anchored to this file so cwd doesn't matter (mirrors spotify_service.py).
_HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CACHE = os.path.join(_HERE, ".lastfm_cache.json")

# Be a good citizen; Last.fm asks for a descriptive User-Agent.
_USER_AGENT = "SpotifyPlaylistMagic/1.0 (+https://github.com/CoryBerry/Spotify_Playlist_Magic)"


class LastfmError(RuntimeError):
    """Raised on missing key or a Last.fm API error response."""


# ---------------------------------------------------------------------------
# Low-level call + cache
# ---------------------------------------------------------------------------

def get_api_key() -> str:
    key = os.environ.get("LASTFM_API_KEY")
    if not key:
        raise LastfmError("Missing LASTFM_API_KEY. Check your .env.")
    return key


def get_user() -> str:
    """The scrobbling username the user-scoped reads run against.

    Separate from ``get_api_key`` because it's a separate failure: a valid key with no
    ``LASTFM_USER`` can still answer every artist/track read, just nothing personal.
    """
    user = os.environ.get("LASTFM_USER")
    if not user:
        raise LastfmError("Missing LASTFM_USER. Check your .env.")
    return user


# The cache is parsed at most once per process and kept in memory, keyed by path. It used to be
# re-read on *every* call and fully rewritten on every miss — fine at a few KB, ruinous at
# 9 MB: fanning `roster --heat` over ~400 artists spent minutes re-parsing an already-warm
# file. Misses now just mark the path dirty; it's flushed at most every _FLUSH_EVERY seconds
# and once more at exit, so a cold fan-out costs a handful of writes instead of one per artist.
_MEMO: dict[str, dict] = {}
_DIRTY: set[str] = set()
_LAST_FLUSH: dict[str, float] = {}
_FLUSH_EVERY = 5.0  # seconds


def _read_cache_file(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError:
        # Keep the bad file for inspection instead of silently overwriting it on next flush.
        try:
            os.replace(path, path + ".corrupt")
        except OSError:
            pass
        return {}
    return data if isinstance(data, dict) else {}


def _load_cache(path: str) -> dict:
    """The in-memory cache for ``path``, reading the file only on first use this process."""
    if path not in _MEMO:
        _MEMO[path] = _read_cache_file(path)
        _LAST_FLUSH[path] = time.monotonic()
    return _MEMO[path]


def _save_cache(cache: dict, path: str) -> None:
    """Write atomically (temp file + rename) so a killed process can't truncate the cache.

    Compact JSON on purpose: indentation alone was ~20% of a multi-MB file nobody hand-reads.
    """
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(cache, fh, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# Response slimming — cache only the fields the parsers below actually read
# ---------------------------------------------------------------------------
# Raw bodies are mostly image arrays, URLs, mbids and streamable flags: a 1000-track
# user.gettoptracks page is ~1.3 MB raw and ~60 KB slimmed. Each slimmer keeps the response
# *shape* (same nesting, same keys) so the parsers can't tell a slim body from a raw one.
# Methods without a slimmer are cached raw — they're low-volume.

def _slim_track(t: Any) -> Any:
    if not isinstance(t, dict):
        return t
    out = {k: t[k] for k in ("name", "playcount") if k in t}
    if isinstance(t.get("artist"), dict):
        out["artist"] = {"name": t["artist"].get("name")}
    return out


def _slim_paged(root: str, node: str):
    def slim(body: dict) -> dict:
        container = body.get(root)
        if not isinstance(container, dict):
            return body
        out: dict = {node: [_slim_track(i) for i in _as_list(container.get(node))]}
        attr = container.get("@attr")
        if isinstance(attr, dict) and "totalPages" in attr:
            out["@attr"] = {"totalPages": attr["totalPages"]}
        return {root: out}
    return slim


def _slim_toptags(body: dict) -> dict:
    container = body.get("toptags")
    if not isinstance(container, dict):
        return body
    tags = [{k: t[k] for k in ("name", "count") if k in t}
            for t in _as_list(container.get("tag")) if isinstance(t, dict)]
    return {"toptags": {"tag": tags}}


_SLIMMERS = {
    "artist.gettoptags": _slim_toptags,
    "user.gettoptracks": _slim_paged("toptracks", "track"),
    "user.getlovedtracks": _slim_paged("lovedtracks", "track"),
    "user.gettopartists": _slim_paged("topartists", "artist"),
}


def _slim(method: str, body: Any) -> Any:
    fn = _SLIMMERS.get(method)
    return fn(body) if fn and isinstance(body, dict) else body


def compact_cache(path: Optional[str] = None) -> tuple[int, int]:
    """Slim every entry already on disk (no refetch). Returns (bytes_before, bytes_after)."""
    path = path or DEFAULT_CACHE
    before = os.path.getsize(path) if os.path.exists(path) else 0
    cache = _load_cache(path)
    for ck, hit in cache.items():
        method = ck.split("?", 1)[0]
        if isinstance(hit, dict) and "_ts" in hit and "_body" in hit:
            hit["_body"] = _slim(method, hit["_body"])
        else:
            cache[ck] = _slim(method, hit)
    _save_cache(cache, path)
    _DIRTY.discard(path)
    return before, os.path.getsize(path)


def _mark_dirty(path: str) -> None:
    _DIRTY.add(path)
    if time.monotonic() - _LAST_FLUSH.get(path, 0.0) >= _FLUSH_EVERY:
        _flush(path)


def _flush(path: str) -> None:
    if path in _DIRTY and path in _MEMO:
        _save_cache(_MEMO[path], path)
        _DIRTY.discard(path)
    _LAST_FLUSH[path] = time.monotonic()


def flush_cache() -> None:
    """Write any pending cache entries to disk. Runs automatically at interpreter exit."""
    for path in list(_DIRTY):
        try:
            _flush(path)
        except OSError:
            pass


atexit.register(flush_cache)


def _call(
    method: str,
    params: dict[str, Any],
    *,
    use_cache: bool = True,
    cache_path: Optional[str] = None,
    timeout: int = 15,
    ttl: Optional[int] = None,
) -> dict:
    """Call a Last.fm read method, returning the parsed JSON body.

    Raises LastfmError on a Last.fm-level error (``{"error": N, "message": ...}``) or a
    network/HTTP failure. Successful responses are memoized by (method, sorted params).

    ``ttl`` (seconds) marks a response as *perishable*. Artist/track facts are stable and
    cache forever (``ttl=None``, stored bare). A user's own scrobble counts change daily, so
    the user-scoped reads pass a TTL and are stored timestamp-wrapped; an expired entry is a
    miss. Both shapes coexist in one file — legacy bare entries simply never satisfy a
    TTL'd read.
    """
    cache_path = cache_path or DEFAULT_CACHE
    # Cache key excludes the API key so the file is shareable and stable.
    ck = method + "?" + urllib.parse.urlencode(sorted(params.items()))

    cache = _load_cache(cache_path) if use_cache else {}
    if use_cache and ck in cache:
        hit = cache[ck]
        wrapped = isinstance(hit, dict) and "_ts" in hit and "_body" in hit
        if ttl is None:
            if not wrapped:
                return hit
        elif wrapped and (time.time() - hit["_ts"]) < ttl:
            return hit["_body"]

    query = dict(params)
    query.update(method=method, api_key=get_api_key(), format="json")
    url = API_ROOT + "?" + urllib.parse.urlencode(query)
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.load(resp)
    except urllib.error.URLError as exc:
        raise LastfmError(f"Last.fm request failed: {exc}") from exc

    if isinstance(body, dict) and "error" in body:
        raise LastfmError(f"Last.fm error {body['error']}: {body.get('message', '')}")

    # Slim before returning too, so a miss and a later hit hand callers the identical body.
    body = _slim(method, body)
    if use_cache:
        cache[ck] = body if ttl is None else {"_ts": time.time(), "_body": body}
        _mark_dirty(cache_path)
    return body


def _as_list(value: Any) -> list:
    """Last.fm collapses single-element lists to a bare dict — normalize to a list."""
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


# ---------------------------------------------------------------------------
# Enrichment (the useful surface)
# ---------------------------------------------------------------------------

def artist_info(name: str, *, autocorrect: bool = True, use_cache: bool = True) -> Optional[dict]:
    """Return a flattened summary for an artist, or None if Last.fm doesn't know it.

    {name, listeners, playcount, tags: [str], similar: [str], bio: str}

    ``autocorrect`` fixes typos but can also redirect a correct name to a different act
    (Ratboys → Ratboy); pass False when the name already came from Last.fm or Spotify.
    """
    try:
        body = _call("artist.getinfo", {"artist": name, "autocorrect": "1" if autocorrect else "0"},
                     use_cache=use_cache)
    except LastfmError:
        return None
    a = body.get("artist")
    if not a:
        return None
    stats = a.get("stats") or {}
    return {
        "name": a.get("name", name),
        "listeners": int(stats.get("listeners") or 0),
        "playcount": int(stats.get("playcount") or 0),
        "tags": [t["name"] for t in _as_list((a.get("tags") or {}).get("tag")) if t.get("name")],
        "similar": [s["name"] for s in _as_list((a.get("similar") or {}).get("artist")) if s.get("name")],
        "bio": ((a.get("bio") or {}).get("summary") or "").strip(),
    }


def similar_artists(name: str, limit: int = 20, *, use_cache: bool = True) -> list[dict]:
    """Artists similar to ``name``, most-similar first: [{name, match}] (match is 0..1)."""
    limit = max(1, min(int(limit), 100))
    try:
        body = _call(
            "artist.getsimilar",
            {"artist": name, "autocorrect": "1", "limit": str(limit)},
            use_cache=use_cache,
        )
    except LastfmError:
        return []
    items = _as_list((body.get("similarartists") or {}).get("artist"))
    out = []
    for s in items:
        if s.get("name"):
            try:
                match = float(s.get("match") or 0.0)
            except (TypeError, ValueError):
                match = 0.0
            out.append({"name": s["name"], "match": round(match, 4)})
    return out


def artist_top_tags(name: str, limit: int = 10, *, use_cache: bool = True) -> list[dict]:
    """Top tags for an artist, most-weighted first: [{name, count}]."""
    limit = max(1, min(int(limit), 100))
    try:
        body = _call("artist.gettoptags", {"artist": name, "autocorrect": "1"}, use_cache=use_cache)
    except LastfmError:
        return []
    items = _as_list((body.get("toptags") or {}).get("tag"))
    out = [{"name": t["name"], "count": int(t.get("count") or 0)} for t in items if t.get("name")]
    return out[:limit]


def track_info(
    artist: str,
    title: str,
    *,
    username: Optional[str] = None,
    use_cache: bool = True,
) -> Optional[dict]:
    """Return {name, artist, listeners, playcount, tags: [str]} for a track, or None.

    Pass ``username`` to additionally get that user's own relationship to the track as
    ``user_playcount`` and ``loved``. This is one call *per track*, so it's the expensive way
    to read a personal signal — prefer the bulk ``user_top_tracks`` / ``user_loved_tracks``
    reads and fall back here only for specific misses.
    """
    params = {"artist": artist, "track": title, "autocorrect": "1"}
    if username:
        params["username"] = username
    try:
        body = _call(
            "track.getinfo",
            params,
            use_cache=use_cache,
            ttl=_USER_TTL if username else None,
        )
    except LastfmError:
        return None
    t = body.get("track")
    if not t:
        return None
    out = {
        "name": t.get("name", title),
        "artist": (t.get("artist") or {}).get("name", artist),
        "listeners": int(t.get("listeners") or 0),
        "playcount": int(t.get("playcount") or 0),
        "tags": [g["name"] for g in _as_list((t.get("toptags") or {}).get("tag")) if g.get("name")],
    }
    if username:
        out["user_playcount"] = int(t.get("userplaycount") or 0)
        out["loved"] = str(t.get("userloved") or "0") == "1"
    return out


# ---------------------------------------------------------------------------
# User-scoped reads (LASTFM_USER) — the personal signal
# ---------------------------------------------------------------------------
# Still key-only auth: a user's scrobbles and loves are public reads, so these need no signed
# session. What they *do* need is a TTL — unlike an artist's tags, these change every time
# Cory plays something.

_USER_TTL = 12 * 3600  # 12h — scrobble counts drift daily, not hourly
_PAGE_LIMIT = 1000     # Last.fm's per-page ceiling for these methods


def _paged(
    method: str,
    root: str,
    node: str,
    params: dict[str, Any],
    *,
    max_pages: int,
    use_cache: bool,
) -> list[dict]:
    """Walk a paged Last.fm list method, returning the concatenated item dicts.

    Stops at ``max_pages``, at the reported ``totalPages``, or at the first short/empty page —
    whichever comes first. A failure mid-walk returns what was collected rather than raising,
    so a flaky page degrades to a partial signal instead of killing the caller.
    """
    items: list[dict] = []
    page = 1
    while page <= max_pages:
        try:
            body = _call(
                method,
                dict(params, limit=str(_PAGE_LIMIT), page=str(page)),
                use_cache=use_cache,
                ttl=_USER_TTL,
            )
        except LastfmError:
            break
        container = body.get(root) or {}
        batch = _as_list(container.get(node))
        items.extend(b for b in batch if isinstance(b, dict))
        try:
            total_pages = int((container.get("@attr") or {}).get("totalPages") or 1)
        except (TypeError, ValueError):
            total_pages = 1
        if len(batch) < _PAGE_LIMIT or page >= total_pages:
            break
        page += 1
    return items


def user_top_tracks(
    user: Optional[str] = None,
    *,
    period: str = "overall",
    max_pages: int = 5,
    use_cache: bool = True,
) -> list[dict]:
    """The user's most-scrobbled tracks: [{artist, title, playcount}], most-played first.

    ``period`` is Last.fm's window vocabulary (``overall`` / ``7day`` / ``1month`` /
    ``3month`` / ``6month`` / ``12month``). Defaults to ``overall`` — durable taste.
    """
    user = user or get_user()
    rows = _paged(
        "user.gettoptracks",
        "toptracks",
        "track",
        {"user": user, "period": period},
        max_pages=max_pages,
        use_cache=use_cache,
    )
    out = []
    for t in rows:
        name = t.get("name")
        artist = (t.get("artist") or {}).get("name")
        if not name or not artist:
            continue
        try:
            plays = int(t.get("playcount") or 0)
        except (TypeError, ValueError):
            plays = 0
        out.append({"artist": artist, "title": name, "playcount": plays})
    return out


def user_loved_tracks(
    user: Optional[str] = None,
    *,
    max_pages: int = 5,
    use_cache: bool = True,
) -> list[dict]:
    """The user's explicitly loved tracks: [{artist, title}]."""
    user = user or get_user()
    rows = _paged(
        "user.getlovedtracks",
        "lovedtracks",
        "track",
        {"user": user},
        max_pages=max_pages,
        use_cache=use_cache,
    )
    out = []
    for t in rows:
        name = t.get("name")
        artist = (t.get("artist") or {}).get("name")
        if name and artist:
            out.append({"artist": artist, "title": name})
    return out


def user_top_artists(
    user: Optional[str] = None,
    *,
    period: str = "overall",
    max_pages: int = 3,
    use_cache: bool = True,
) -> list[dict]:
    """The user's most-scrobbled artists: [{artist, playcount}], most-played first.

    Same ``period`` vocabulary as ``user_top_tracks``. Three pages (3,000 artists) covers a
    year of listening comfortably while keeping a cold read to a handful of calls.
    """
    user = user or get_user()
    rows = _paged(
        "user.gettopartists",
        "topartists",
        "artist",
        {"user": user, "period": period},
        max_pages=max_pages,
        use_cache=use_cache,
    )
    out = []
    for a in rows:
        if not a.get("name"):
            continue
        try:
            plays = int(a.get("playcount") or 0)
        except (TypeError, ValueError):
            plays = 0
        out.append({"artist": a["name"], "playcount": plays})
    return out


def user_weekly_artist_charts(
    user: Optional[str] = None,
    *,
    weeks: int = 13,
    use_cache: bool = True,
) -> list[dict]:
    """The user's per-week artist play counts for the last ``weeks`` chart weeks.

    Returns ``[{from, to, artists: {name: playcount}}]``, oldest first. This is how a caller
    can tell *sustained* listening (played in 7 of 13 weeks) from one intense session, which a
    windowed top-artists total can't. A finished week never changes, so those charts cache
    forever; only the chart list and the in-progress week use the perishable TTL. A week that
    fails to load is skipped rather than raising — callers should read ``len(result)``.
    """
    user = user or get_user()
    weeks = max(1, min(int(weeks), 52))
    body = _call("user.getweeklychartlist", {"user": user}, use_cache=use_cache, ttl=_USER_TTL)
    charts = _as_list((body.get("weeklychartlist") or {}).get("chart"))[-weeks:]
    now = time.time()
    out = []
    for c in charts:
        frm, to = c.get("from"), c.get("to")
        if not frm or not to:
            continue
        finished = float(to) < now
        try:
            wk = _call(
                "user.getweeklyartistchart",
                {"user": user, "from": frm, "to": to},
                use_cache=use_cache,
                ttl=None if finished else _USER_TTL,
            )
        except LastfmError:
            continue
        artists = {}
        for a in _as_list((wk.get("weeklyartistchart") or {}).get("artist")):
            if a.get("name"):
                try:
                    artists[a["name"]] = int(a.get("playcount") or 0)
                except (TypeError, ValueError):
                    pass
        out.append({"from": int(frm), "to": int(to), "artists": artists})
    return out


if __name__ == "__main__":
    import sys

    if sys.argv[1:] == ["--compact-cache"]:
        b, a = compact_cache()
        print(f"compacted {DEFAULT_CACHE}: {b / 1e6:.1f} MB -> {a / 1e6:.2f} MB")
    else:
        sys.exit("usage: python lastfm_service.py --compact-cache")
