"""Headless Spotify playlist service — the prompt-free create path.

This module is the seam between "a list of tracks" and "a real Spotify playlist". It is
deliberately decoupled from the Flask app: it imports no Flask, no SQLAlchemy, and reads no
Flask session. That lets ``cli.py`` (and anything else) build playlists without spinning up
the web app, its DB, or ``SECRET_KEY``.

The resolver (``_name_sim`` / ``_search_line``) lives here now and is imported back into
``app.py`` so there is a single source of truth for search matching.

Auth model: the web app logs in via OAuth and spotipy caches a refresh token in ``.cache``.
``get_headless_client()`` reuses that cache and refreshes silently — no browser, no prompt.
If there is no cached token, it raises rather than trying to open a browser, because this
path is meant to run unattended.
"""
from __future__ import annotations

import json
import os
import time
from typing import Any, Optional

import spotipy
from spotipy.oauth2 import SpotifyOAuth

# Same scope the web app requests (kept in sync with app.get_spotify_oauth).
SPOTIFY_SCOPE = "playlist-read-private playlist-modify-private playlist-modify-public"

# Default locations, anchored to this file so cwd doesn't matter.
_HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_TOKEN_CACHE = os.path.join(_HERE, ".cache")            # spotipy's OAuth token cache
DEFAULT_RESOLVE_CACHE = os.path.join(_HERE, ".track_cache.json")  # line -> resolved track


class SpotifyAuthError(RuntimeError):
    """Raised when no usable cached token exists (CLI can't prompt for a browser login)."""


# ---------------------------------------------------------------------------
# Search / matching  (moved from app.py — app.py now imports these back)
# ---------------------------------------------------------------------------

def _name_sim(query: str, result: str) -> float:
    """Word-overlap similarity between a query title and a Spotify result name."""
    q = set(query.lower().split())
    r = set(result.lower().split())
    return len(q & r) / len(q) if q else 0.0


def _search_line(sp, line: str) -> list[dict]:
    """Search Spotify for a line, returning up to 4 scored candidates sorted by name similarity."""
    parts = line.split(" - ", 1)
    artist = parts[0].strip() if len(parts) == 2 else ""
    title = parts[1].strip() if len(parts) == 2 else line.strip()
    q = (f"artist:{artist} " if artist else "") + title

    candidates: list[dict] = []
    try:
        for t in (sp.search(q=q, type="track", limit=5).get("tracks") or {}).get("items") or []:
            if t:
                candidates.append({
                    "uri": t["uri"],
                    "type": "track",
                    "name": t["name"],
                    "artist": t["artists"][0]["name"] if t.get("artists") else "",
                    "score": _name_sim(title, t["name"]),
                })
    except Exception:
        pass
    try:
        for a in (sp.search(q=q, type="album", limit=3).get("albums") or {}).get("items") or []:
            if a:
                candidates.append({
                    "uri": a["uri"],
                    "type": "album",
                    "name": a["name"],
                    "artist": a["artists"][0]["name"] if a.get("artists") else "",
                    "score": _name_sim(title, a["name"]),
                })
    except Exception:
        pass
    candidates.sort(key=lambda x: -x["score"])
    return candidates[:4]


# ---------------------------------------------------------------------------
# Headless auth
# ---------------------------------------------------------------------------

def get_headless_client(token_cache: Optional[str] = None, scope: str = SPOTIFY_SCOPE):
    """Return an authenticated spotipy client using the cached refresh token — no prompts.

    Raises SpotifyAuthError if there is no cached token to refresh from (i.e. you have never
    completed the web OAuth login). The returned client auto-refreshes on every request via
    its auth_manager, so a stale access token is not a problem.
    """
    token_cache = token_cache or DEFAULT_TOKEN_CACHE
    client_id = os.environ.get("SPOTIFY_CLIENT_ID")
    client_secret = os.environ.get("SPOTIFY_CLIENT_SECRET")
    redirect_uri = os.environ.get("SPOTIFY_REDIRECT_URI")
    if not (client_id and client_secret and redirect_uri):
        raise SpotifyAuthError(
            "Missing SPOTIFY_CLIENT_ID / SPOTIFY_CLIENT_SECRET / SPOTIFY_REDIRECT_URI. "
            "Check your .env."
        )

    auth = SpotifyOAuth(
        client_id=client_id,
        client_secret=client_secret,
        redirect_uri=redirect_uri,
        scope=scope,
        cache_path=token_cache,
        open_browser=False,
    )
    cached = auth.cache_handler.get_cached_token()
    if not cached:
        raise SpotifyAuthError(
            f"No cached Spotify token at {token_cache}. Log in once via the web app "
            "(run app.py, click Login with Spotify), then re-run this command."
        )
    # auth_manager makes every request validate/refresh the token as needed.
    return spotipy.Spotify(auth_manager=auth)


# ---------------------------------------------------------------------------
# Resolution cache  (line -> resolved track), so re-runs are cheap + deterministic
# ---------------------------------------------------------------------------

def _norm(line: str) -> str:
    return " ".join(line.lower().split())


def load_resolve_cache(path: Optional[str] = None) -> dict:
    path = path or DEFAULT_RESOLVE_CACHE
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_resolve_cache(cache: dict, path: Optional[str] = None) -> None:
    path = path or DEFAULT_RESOLVE_CACHE
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(cache, fh, indent=2, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Resolve + create
# ---------------------------------------------------------------------------

def resolve_tracks(
    sp,
    lines: list[str],
    threshold: float = 0.6,
    cache: Optional[dict] = None,
) -> tuple[list[dict], list[dict]]:
    """Resolve free-text "Artist - Title" lines to Spotify track URIs — no prompts.

    Auto-pick policy: search each line, keep only *track* candidates (never expand an album
    into a mood playlist), take the top-scored one, and accept it iff its score >= threshold.
    Anything below threshold (or with no track match) becomes a *reported miss*, never a
    question and never a silently-wrong pick.

    Returns (resolved, misses):
      resolved: [{line, uri, name, artist, score, source}]  source is "cache" or "search"
      misses:   [{line, reason, best}]  reason is "low_confidence" | "no_match"
    """
    cache = cache if cache is not None else {}
    resolved: list[dict] = []
    misses: list[dict] = []

    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue

        key = _norm(line)
        if key in cache:
            hit = dict(cache[key])
            hit.update(line=line, source="cache")
            resolved.append(hit)
            continue

        candidates = _search_line(sp, line)
        tracks = [c for c in candidates if c["type"] == "track"]
        best = tracks[0] if tracks else None

        if best and best["score"] >= threshold:
            hit = {
                "line": line,
                "uri": best["uri"],
                "name": best["name"],
                "artist": best["artist"],
                "score": round(best["score"], 2),
                "source": "search",
            }
            resolved.append(hit)
            cache[key] = {k: hit[k] for k in ("uri", "name", "artist", "score")}
        else:
            misses.append({
                "line": line,
                "reason": "low_confidence" if best else "no_match",
                "best": (
                    {"name": best["name"], "artist": best["artist"], "score": round(best["score"], 2)}
                    if best else None
                ),
            })

    return resolved, misses


def create_playlist_from_lines(
    sp,
    name: str,
    lines: list[str],
    description: str = "",
    public: bool = False,
    threshold: float = 0.6,
    cache: Optional[dict] = None,
    dedupe: bool = True,
) -> dict:
    """Resolve lines and create a Spotify playlist from the hits. Fully non-interactive.

    Returns a summary dict (JSON-serializable). On ``created: False`` nothing was created
    (no line resolved); ``missed`` still lists what failed so the caller can report it.
    """
    t0 = time.time()
    resolved, misses = resolve_tracks(sp, lines, threshold=threshold, cache=cache)

    uris = [r["uri"] for r in resolved]
    if dedupe:
        seen: set[str] = set()
        uris = [u for u in uris if not (u in seen or seen.add(u))]

    if not uris:
        return {
            "created": False,
            "reason": "no_tracks_resolved",
            "resolved": resolved,
            "missed": misses,
            "gen_seconds": round(time.time() - t0, 1),
        }

    user_id = sp.me()["id"]
    playlist = sp.user_playlist_create(
        user_id, name, public=public, description=(description or "")[:300]
    )
    for i in range(0, len(uris), 100):  # Spotify's per-request cap
        sp.playlist_add_items(playlist["id"], uris[i:i + 100])

    return {
        "created": True,
        "playlist_id": playlist["id"],
        "playlist_url": playlist["external_urls"]["spotify"],
        "name": playlist["name"],
        "track_count": len(uris),
        "resolved": resolved,
        "missed": misses,
        "gen_seconds": round(time.time() - t0, 1),
    }
