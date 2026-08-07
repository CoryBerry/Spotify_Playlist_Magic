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
) -> dict:
    """Call a Last.fm read method, returning the parsed JSON body.

    Raises LastfmError on a Last.fm-level error (``{"error": N, "message": ...}``) or a
    network/HTTP failure. Successful responses are memoized by (method, sorted params).
    """
    cache_path = cache_path or DEFAULT_CACHE
    # Cache key excludes the API key so the file is shareable and stable.
    ck = method + "?" + urllib.parse.urlencode(sorted(params.items()))

    cache = _load_cache(cache_path) if use_cache else {}
    if use_cache and ck in cache:
        return cache[ck]

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
        cache[ck] = body
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


def track_info(artist: str, title: str, *, use_cache: bool = True) -> Optional[dict]:
    """Return {name, artist, listeners, playcount, tags: [str]} for a track, or None."""
    try:
        body = _call(
            "track.getinfo",
            {"artist": artist, "track": title, "autocorrect": "1"},
            use_cache=use_cache,
        )
    except LastfmError:
        return None
    t = body.get("track")
    if not t:
        return None
    return {
        "name": t.get("name", title),
        "artist": (t.get("artist") or {}).get("name", artist),
        "listeners": int(t.get("listeners") or 0),
        "playcount": int(t.get("playcount") or 0),
        "tags": [g["name"] for g in _as_list((t.get("toptags") or {}).get("tag")) if g.get("name")],
    }
