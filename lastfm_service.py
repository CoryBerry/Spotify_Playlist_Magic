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


def _load_cache(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_cache(cache: dict, path: str) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(cache, fh, indent=2, ensure_ascii=False)


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

    if use_cache:
        cache[ck] = body if ttl is None else {"_ts": time.time(), "_body": body}
        _save_cache(cache, cache_path)
    return body


def _as_list(value: Any) -> list:
    """Last.fm collapses single-element lists to a bare dict — normalize to a list."""
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


# ---------------------------------------------------------------------------
# Enrichment (the useful surface)
# ---------------------------------------------------------------------------

def artist_info(name: str, *, use_cache: bool = True) -> Optional[dict]:
    """Return a flattened summary for an artist, or None if Last.fm doesn't know it.

    {name, listeners, playcount, tags: [str], similar: [str], bio: str}
    """
    try:
        body = _call("artist.getinfo", {"artist": name, "autocorrect": "1"}, use_cache=use_cache)
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
