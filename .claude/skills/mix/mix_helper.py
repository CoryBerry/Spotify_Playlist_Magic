#!/usr/bin/env python
"""
mix_helper.py — plumbing for the `mix` skill.

Encapsulates the three steps of designing a playlist "mix" from Cory's existing
Spotify library, so the skill never has to re-derive the auth / fetch / create
dance by hand:

    sources   list candidate playlists (name, id, track_count, tags, use_count)
    tracks    dump real tracks (uri / name / artist) from one or more sources,
              optionally excluding tracks still inside the app's cooldown window
    create    create a private playlist from a list of URIs, optionally recording
              it to the app's DB (CreatedPlaylist + TrackHistory + PlaylistUsage for
              each --source) like a real build

    refresh-cache  re-read the Spotify library into the app's playlist_cache, so
              source names/ids resolve without opening the web app first
    find-artists  sweep the already-pulled pools for a roster of artists —
              fully offline, and the only way to ask "what does the library
              NOT have?" (pool names say nothing about their contents)

Auth reuses the same Spotipy `.cache` token the Flask app writes, so no browser
login is needed as long as a valid token exists. Reads of *Spotify* are always
safe; `create` and `replace` are the only verbs that write to Spotify and must be
invoked explicitly. `create`, `replace` and `refresh-cache` also write the app's
regenerable `playlist_cache` blob so the skill can see its own playlists.

The `roster` verb can optionally annotate each candidate with its lead artist's
Last.fm tags (`--tags`, opt-in — needs LASTFM_API_KEY; plain roster stays offline
and Last.fm-free). Tags give mood/genre context for curation now that Spotify's
/audio-features is unavailable.

Examples:
    python mix_helper.py sources --search chill
    python mix_helper.py sources --tag selects
    python mix_helper.py tracks "Cory's Chilled Playlist" 43M34ZEoIBEMbe9SDA1atB --exclude-cooldown
    python mix_helper.py roster "90s Albums" --tags
    python mix_helper.py create --name "Sunday Slow Burn" --desc "..." --uris-file picks.txt --record --cooldown --source "Chill Albums" --source "90s Albums"
"""
import argparse
import glob
import hashlib
import json
import math
import os
import random
import re
import sqlite3
import sys
import unicodedata
from datetime import datetime, timedelta

from dotenv import load_dotenv
import spotipy
from spotipy.oauth2 import SpotifyOAuth

# Paths are resolved relative to the repo root (two levels up from this file:
# .claude/skills/mix/mix_helper.py -> repo root), so the skill works regardless
# of the current working directory.
REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
# Make the repo-root modules (e.g. lastfm_service) importable regardless of cwd.
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)
DB_PATH = os.path.join(REPO_ROOT, "instance", "spotify_tools.db")
CACHE_PATH = os.path.join(REPO_ROOT, ".cache")
# Disk cache of per-playlist track pulls, keyed by Spotify's snapshot_id so an
# unchanged source is served with zero playlist_items calls (issue #3). One JSON
# file per playlist; created lazily; git-ignored. See _fetch_tracks_rich_cached.
MIX_CACHE_DIR = os.path.join(REPO_ROOT, ".mix_cache")
# Schema marker for the per-track dicts inside those files. Bump it whenever
# _fetch_tracks_rich gains or renames a field: a blob written by an older version
# is treated as a miss and re-pulled, so a stale-schema entry can never surface
# rows missing the new keys. v2 added duration_ms / year / explicit (issue #12).
MIX_CACHE_VERSION = 2
SCOPE = "playlist-read-private playlist-modify-private playlist-modify-public"
# Name tag prepended to every mix we ship, so generated playlists group together
# in the library (Spotify's API can't file into folders). Idempotent; opt out
# with `create --no-prefix`.
MIX_PREFIX = "[Mix]"


# ---------------------------------------------------------------- infra

def _db():
    return sqlite3.connect(DB_PATH)


def _client():
    """Build a Spotipy client from the app's cached token. Refreshes if expired."""
    load_dotenv(os.path.join(REPO_ROOT, ".env"))
    oauth = SpotifyOAuth(
        client_id=os.environ["SPOTIFY_CLIENT_ID"],
        client_secret=os.environ["SPOTIFY_CLIENT_SECRET"],
        redirect_uri=os.environ["SPOTIFY_REDIRECT_URI"],
        scope=SCOPE,
        cache_path=CACHE_PATH,
        open_browser=False,
    )
    tok = oauth.get_cached_token()
    if not tok:
        sys.exit(
            "No cached Spotify token at .cache. Log in through the Flask app once "
            "(it writes .cache), then retry."
        )
    if oauth.is_token_expired(tok):
        tok = oauth.refresh_access_token(tok["refresh_token"])
    return spotipy.Spotify(auth=tok["access_token"])


# ------------------------------------------------------- playlist_cache (shared)
# The app's single-row `playlist_cache` blob is how every source token gets
# resolved to an id. The skill used to only read it, which meant a playlist the
# skill created was invisible to the skill until someone opened the web app
# (issue #13) — so `create` now appends to it, and `refresh-cache` can rewrite it
# outright. Safe to write: the blob is regenerable by design (cli.py backup skips
# it), and the app's own TTL tiers compare its length against Spotify's playlist
# total, so appending a real new playlist keeps that check consistent rather than
# breaking it.

def _read_playlist_cache():
    """The blob as a list, or None when the row is absent/unreadable. Non-fatal."""
    row = _db().execute("SELECT data FROM playlist_cache LIMIT 1").fetchone()
    if not row:
        return None
    try:
        return json.loads(row[0])
    except ValueError:
        return None


def _write_playlist_cache(playlists, user_id):
    """Replace the blob and bump updated_at, keeping it to the one row the app expects.

    Prefers the row already keyed to `user_id`; failing that it rewrites whichever
    single row exists (re-keying it) so a user_id mismatch can't leave two rows for
    `_read_playlist_cache`'s LIMIT 1 to choose between. Single-user app by design.
    """
    db = _db()
    now = datetime.now().isoformat(sep=" ")
    data = json.dumps(playlists, ensure_ascii=False)
    row = (db.execute("SELECT id FROM playlist_cache WHERE user_id=?", (user_id,)).fetchone()
           or db.execute("SELECT id FROM playlist_cache LIMIT 1").fetchone())
    if row:
        db.execute("UPDATE playlist_cache SET user_id=?, data=?, updated_at=? WHERE id=?",
                   (user_id, data, now, row[0]))
    else:
        db.execute("INSERT INTO playlist_cache (user_id, data, updated_at) VALUES (?,?,?)",
                   (user_id, data, now))
    db.commit()


def _cache_upsert_playlist(pl, user_id, track_total=None):
    """Put a just-created/just-rewritten playlist into the blob. Returns the new length.

    The create/replace response already carries the playlist object, so this closes
    the loop with no extra API call. `track_total` overrides the object's own count,
    which reads 0 straight out of `user_playlist_create` (tracks are added after).
    Newest goes first, matching how Spotify paginates a library.
    """
    pls = _read_playlist_cache() or []
    entry = dict(pl)
    if track_total is not None:
        entry["tracks"] = dict(entry.get("tracks") or {}, total=track_total)
    pls = [entry] + [p for p in pls if p.get("id") != entry.get("id")]
    _write_playlist_cache(pls, user_id)
    return len(pls)


def _cached_playlists():
    """The app's 1-row playlist_cache blob: every playlist with name + track total."""
    pls = _read_playlist_cache()
    if pls is None:
        sys.exit("playlist_cache is empty or unreadable. Run "
                 "`mix_helper.py refresh-cache` to rebuild it from Spotify "
                 "(or open the app's Manage page once).")
    return pls


def _cooldown_days():
    row = _db().execute("SELECT cooldown_days FROM app_settings LIMIT 1").fetchone()
    return row[0] if row else 7


# ---------------------------------------------------------------- ice box
# A manual, long-lived exclusion list ("never-list" / timed freeze) shared with
# the Flask app via the same `track_ice` table. Distinct from the 7-day cooldown:
# ice is a HARD exclusion (never yields to a small pool) and can last months or
# forever. thaw_at IS NULL => never-list; thaw_at in the future => timed ice.

def _add_months(dt, n):
    """Calendar-correct month add — '6-month ice' thaws the same day, 6 months on."""
    m = dt.month - 1 + n
    y = dt.year + m // 12
    m = m % 12 + 1
    leap = y % 4 == 0 and (y % 100 != 0 or y % 400 == 0)
    dim = [31, 29 if leap else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][m - 1]
    return dt.replace(year=y, month=m, day=min(dt.day, dim))


def _ensure_ice_table(db):
    """Create track_ice if the app hasn't yet (matches the SQLAlchemy schema)."""
    db.execute(
        "CREATE TABLE IF NOT EXISTS track_ice ("
        " id INTEGER PRIMARY KEY,"
        " track_id VARCHAR(200) NOT NULL,"
        " provider VARCHAR(20) NOT NULL,"
        " name VARCHAR(300), artist VARCHAR(300),"
        " frozen_at DATETIME, thaw_at DATETIME, reason VARCHAR(300),"
        " UNIQUE(track_id, provider))"
    )
    db.commit()


def _iced_rows(db, provider="spotify"):
    """Rows on ice *right now*: never-list (thaw_at NULL) or thaw_at still in the future.

    thaw_at is stored in ISO 'YYYY-MM-DD HH:MM:SS.ffffff' form by both writers, so a
    lexical string compare against `now` is a correct chronological compare.
    """
    _ensure_ice_table(db)
    now = datetime.now().isoformat(sep=" ")
    return db.execute(
        "SELECT track_id, name, artist, thaw_at, reason FROM track_ice "
        "WHERE provider=? AND (thaw_at IS NULL OR thaw_at > ?) ORDER BY frozen_at",
        (provider, now),
    ).fetchall()


def _ice_label(thaw_at):
    """Column tag for an iced track: 🧊NVR (never) or 🧊<days>d until it thaws."""
    if thaw_at is None:
        return "🧊NVR"
    days = (_parse_ts(thaw_at) - datetime.now()).days
    return f"🧊{days}d"


def _resolve(source, by_id, by_name):
    """Resolve a source token (exact id, else case-insensitive name substring)."""
    if source in by_id:
        return by_id[source], source  # (name, id) — match the name-branch order
    hits = [(n, i) for n, i in by_name.items() if source.lower() in n.lower()]
    if not hits:
        sys.exit(f"No playlist matches {source!r}. If it's new or was renamed, run "
                 f"`mix_helper.py refresh-cache` to re-read your library.")
    if len(hits) > 1:
        exact = [(n, i) for n, i in hits if n.lower() == source.lower()]
        if len(exact) == 1:
            return exact[0][0], exact[0][1]
        names = ", ".join(n for n, _ in hits[:8])
        sys.exit(f"{source!r} is ambiguous — matches: {names}. Use a fuller name or the id.")
    return hits[0]


def _release_year(release_date):
    """Year as an int from Spotify's `release_date`, or None if absent/odd.

    The field's precision varies ('1994', '1994-09', '1994-09-27'), but the year
    is always the leading four characters when it's there at all.
    """
    if not release_date:
        return None
    head = str(release_date)[:4]
    return int(head) if head.isdigit() else None


def _mmss(ms):
    """A track's runtime as m:ss."""
    secs = round((ms or 0) / 1000)
    return f"{secs // 60}:{secs % 60:02d}"


def _hhmm(ms):
    """A total runtime as 1h23m (or 23m under the hour) — for the roster footer."""
    mins = round((ms or 0) / 1000) // 60
    h, m = divmod(mins, 60)
    return f"{h}h{m:02d}m" if h else f"{m}m"


def _fetch_tracks_rich(sp, pid):
    """Every track for a playlist as a rich dict — see MIX_CACHE_VERSION for the shape.

    playlist_items returns full track objects, so popularity, album, runtime,
    release date and the explicit flag all ride along with no extra API calls.
    Caching them (issue #12) is what keeps mix sizing ("make it ~3 hours") and
    era/explicit filtering offline instead of costing an `sp.tracks()` pass per
    mix. This is the single source of truth for a track pull; the
    (uri, name, artist) tuple form is derived from it, so both the `tracks` and
    `roster` commands share one cache file per playlist.
    """
    out = []
    res = sp.playlist_items(pid, additional_types=["track"], limit=100)
    while res:
        for it in res["items"]:
            t = it.get("track")
            if t and t.get("id"):
                alb = t.get("album") or {}
                out.append({
                    "uri": t["uri"],
                    "name": t["name"],
                    "artist": ", ".join(a["name"] for a in t["artists"]),
                    "album_id": alb.get("id") or "single",
                    "album": alb.get("name") or "",
                    "pop": t.get("popularity", 0) or 0,
                    "duration_ms": t.get("duration_ms") or 0,
                    "year": _release_year(alb.get("release_date")),
                    "explicit": bool(t.get("explicit")),
                })
        res = sp.next(res) if res.get("next") else None
    return out


# ---------------------------------------------------------------- track cache
# Source playlists change infrequently, so re-fetching every track on every run
# is wasteful. We memoize the rich track list per playlist on disk, keyed by
# Spotify's snapshot_id: unchanged playlist => same snapshot => reuse the file;
# the moment it's edited the snapshot flips and we re-pull automatically. No TTL.
#
# Trade-off (issue #3, Option A): snapshot_id does NOT change when Spotify quietly
# recomputes a track's `popularity` over time, so a long-unchanged source serves
# frozen popularity — which `roster` band-selection ranks on. In practice band
# selection is coarse (skip the top hits), so a few points of drift rarely
# reorders anything; `--no-cache` is the manual refresh when it matters.

def _cache_file(pid):
    return os.path.join(MIX_CACHE_DIR, f"{pid}.json")


def _read_cache(pid):
    """Return the cached blob for a playlist, or None if absent/unreadable."""
    try:
        with open(_cache_file(pid), encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _write_cache(pid, snapshot_id, tracks):
    """Write the track pull atomically (tmp + replace) so a crash can't corrupt it."""
    os.makedirs(MIX_CACHE_DIR, exist_ok=True)
    blob = {
        "version": MIX_CACHE_VERSION,
        "snapshot_id": snapshot_id,
        "fetched_at": datetime.now().isoformat(sep=" "),
        "tracks": tracks,
    }
    tmp = _cache_file(pid) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(blob, fh, ensure_ascii=False)
    os.replace(tmp, _cache_file(pid))


def _snapshot_id(sp, pid):
    """The playlist's current snapshot_id — one cheap call, no track items."""
    return (sp.playlist(pid, fields="snapshot_id") or {}).get("snapshot_id")


def _fetch_tracks_rich_cached(sp, pid, use_cache=True):
    """_fetch_tracks_rich, memoized on disk by snapshot_id.

    Cache hit costs one lightweight `playlist` snapshot call and zero
    `playlist_items` calls. use_cache=False (the `--no-cache` escape hatch) skips
    the disk read and forces a live pull, but still rewrites the cache so the next
    normal run is fresh.

    A hit also requires the blob's schema `version` to match MIX_CACHE_VERSION, so
    entries written before a field was added re-pull silently instead of handing
    back rows missing it.
    """
    live = _snapshot_id(sp, pid)
    if use_cache and live is not None:
        cached = _read_cache(pid)
        if (cached and cached.get("snapshot_id") == live
                and cached.get("version") == MIX_CACHE_VERSION):
            return cached["tracks"]
    tracks = _fetch_tracks_rich(sp, pid)
    if live is not None:
        _write_cache(pid, live, tracks)
    return tracks


def _fetch_tracks_cached(sp, pid, use_cache=True):
    """(uri, name, artist) tuples, derived from the shared rich cache."""
    return [(t["uri"], t["name"], t["artist"])
            for t in _fetch_tracks_rich_cached(sp, pid, use_cache)]


def _parse_ts(s):
    """track_history.used_at is stored as 'YYYY-MM-DD HH:MM:SS[.ffffff]'."""
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return datetime.strptime(s.split(".")[0], "%Y-%m-%d %H:%M:%S")


def _last_used_map():
    """uri -> most-recent used_at datetime, across all Spotify track_history rows."""
    latest = {}
    for uri, used in _db().execute(
        "SELECT track_id, used_at FROM track_history WHERE provider='spotify'"
    ):
        cur = latest.get(uri)
        if cur is None or used > cur:
            latest[uri] = used
    return {u: _parse_ts(s) for u, s in latest.items()}


def _band_select(album_tracks, skip_top, per_album, top_mode):
    """Pick tracks from one album's list (already sorted by popularity, desc).

    Default (band): skip the top `skip_top` hits, then take `per_album` — Cory's
    "3rd-5th of 10" sweet spot (album favorites that aren't the obvious single).
    `--top`: just take the highest-popularity `per_album` (the bangers / most-played).
    Short albums/EPs shrink the skip so something always comes through.
    """
    n = len(album_tracks)
    if top_mode:
        return album_tracks[:per_album]
    if n <= per_album:
        return album_tracks[:]
    skip = max(0, min(skip_top, n - per_album))
    return album_tracks[skip:skip + per_album]


def _jitter_skip(album_id, base_skip, steps, hi):
    """Random-walk the band's `skip` so a much-used album drifts off its sweet spot.

    Cory's ask: don't wear out the one deep cut by always picking it. Each prior
    use of the album (`steps`) is one pure-50/50 ±1 step from the band default
    (`base_skip`), so a fresh album sits at the sweet spot and a heavily-replayed
    one has wandered across the tracklist. The coin is a deterministic hash of
    (album_id, step index), so the same history reproduces the same pick and it
    only *moves* when the album is genuinely used again. `skip` is allowed all the
    way up to 0 (the #1 track — "picking top is fine") and the walk reflects at
    both ends [0, hi] so it keeps roaming instead of parking on an edge.
    """
    if hi <= 0 or steps <= 0:
        return base_skip
    pos = base_skip
    for i in range(steps):
        bit = int(hashlib.sha1(f"{album_id}:{i}".encode()).hexdigest(), 16) & 1
        pos += 1 if bit else -1
        if pos < 0:            # reflect off the top edge (rank 0)
            pos = -pos
        elif pos > hi:         # reflect off the deep edge
            pos = 2 * hi - pos
    return max(0, min(pos, hi))


def _validate_filters(args):
    """Fail fast on an impossible filter window instead of returning an empty roster.

    A silently-empty result reads as "the library has nothing like that", which is a
    much more expensive wrong conclusion than an error message.
    """
    for flag, val in (("--pop-min", args.pop_min), ("--pop-max", args.pop_max)):
        if val is not None and not 0 <= val <= 100:
            sys.exit(f"{flag} must be 0-100 (Spotify's popularity scale); got {val}.")
    if (args.pop_min is not None and args.pop_max is not None
            and args.pop_min > args.pop_max):
        sys.exit(f"--pop-min {args.pop_min} is above --pop-max {args.pop_max} — nothing can match.")
    if args.year_min and args.year_max and args.year_min > args.year_max:
        sys.exit(f"--year-min {args.year_min} is above --year-max {args.year_max} — "
                 "nothing can match.")


# Filter labels, in the order _filter_reason tests them — also the stderr report order.
_FILTER_REASONS = ("era", "explicit", "pop")


def _filter_reason(t, args):
    """Why this candidate is excluded ("era"/"explicit"/"pop"), or None to keep it.

    All three read straight off the cached fields (issues #12, #14) — no API calls,
    and applied *after* band/`--top` selection so they narrow the chosen cuts rather
    than changing which cuts an album offers. A track with no known release year is
    dropped by either year bound rather than assumed in range: an era brief wants
    certainty, not guesses.
    """
    yr = t.get("year")
    if args.year_min and (yr is None or yr < args.year_min):
        return "era"
    if args.year_max and (yr is None or yr > args.year_max):
        return "era"
    if args.no_explicit and t.get("explicit"):
        return "explicit"
    pop = t.get("pop") or 0
    if args.pop_min is not None and pop < args.pop_min:
        return "pop"
    if args.pop_max is not None and pop > args.pop_max:
        return "pop"
    return None


def _annotate_tags(rows, lookup):
    """Attach each row's lead-artist tags in place, deduping lookups per run.

    `lookup(artist) -> list[str]` is called at most once per distinct lead artist
    (case-insensitive) — the caller wires it to Last.fm; tests inject a fake. Only
    the lead artist is used (the comma-joined `artist` field's first name). Returns
    `(resolved, total, unknown)`: distinct artists with tags, total distinct, and
    those Last.fm didn't know (empty tags).
    """
    cache = {}
    for t in rows:
        lead = t["artist"].split(",")[0].strip()
        key = lead.lower()
        if key not in cache:
            cache[key] = lookup(lead)
        t["tags"] = cache[key]
    total = len(cache)
    unknown = sum(1 for tags in cache.values() if not tags)
    return total - unknown, total, unknown


# ------------------------------------------------------------- personal signal
# Last.fm scrobbles are `(artist, title)` strings typed by whatever client scrobbled
# them; Spotify rows are catalog objects. The two agree on the song and disagree on
# everything else — casing, accents, "(Remastered 2011)", "- 2019 Remix", featured
# artists. `_norm_track_key` beats both sides into one comparable key so an exact
# dict lookup does the matching; anything that still misses simply gets no marker.

_MINE_PAREN = re.compile(r"[\(\[][^\)\]]*[\)\]]")           # (Remastered), [Live]
_MINE_DASH_SUFFIX = re.compile(r"\s-\s.*$")                  # " - 2019 Remaster"
_MINE_FEAT = re.compile(r"\b(feat|ft|featuring|with)\b.*$")  # "feat. Someone"
# Two classes of punctuation, normalized differently on purpose: marks that sit *inside*
# a word vanish ("T.N.T."/"TNT", "Don't"/"Dont"), separators become spaces so a joined
# and a spaced spelling agree ("AC/DC"/"AC DC", "rock&roll"/"rock & roll").
_MINE_INTRAWORD = re.compile(r"[.'‘’´`]")
_MINE_PUNCT = re.compile(r"[^\w\s]")


def _norm_track_key(artist, title):
    """Normalize `(artist, title)` to a comparable key: `"artist\\ttitle"`.

    Lowercases, strips accents, drops parenthetical/bracketed and trailing-dash
    qualifiers, drops `feat.`-style credits, removes punctuation, collapses
    whitespace. Deliberately lossy — over-normalizing costs a rare false match,
    under-normalizing costs most of the real ones.
    """
    def clean(s, strip_quals):
        s = unicodedata.normalize("NFKD", s or "")
        s = "".join(c for c in s if not unicodedata.combining(c)).lower()
        if strip_quals:
            s = _MINE_PAREN.sub(" ", s)
            s = _MINE_DASH_SUFFIX.sub(" ", s)
        s = _MINE_FEAT.sub(" ", s)
        s = _MINE_INTRAWORD.sub("", s)
        s = _MINE_PUNCT.sub(" ", s)
        return " ".join(s.split())

    # Only the lead artist — Spotify comma-joins every credited performer, Last.fm
    # scrobbles usually carry just the primary.
    lead = (artist or "").split(",")[0]
    return f"{clean(lead, False)}\t{clean(title, True)}"


def _annotate_mine(rows, top_tracks, loved_tracks):
    """Attach each row's personal Last.fm signal in place. Returns `(matched, total)`.

    `top_tracks` is `[{artist, title, playcount}]` and `loved_tracks` is
    `[{artist, title}]` — both already fetched in bulk by the caller (tests inject
    plain lists). Every row gets a `mine` dict so downstream code never has to guard:
    `{"loved": bool, "playcount": int|None}`, with `playcount` None when the track is
    known only as loved. A row matching neither list gets `{"loved": False,
    "playcount": None}` and renders as `·`.

    `matched` counts rows carrying *any* personal signal — the honest denominator for
    "did the name matching work", since an unmatched row and a genuinely never-played
    row are indistinguishable from here.
    """
    plays = {}
    for t in top_tracks:
        plays.setdefault(_norm_track_key(t["artist"], t["title"]), t.get("playcount") or 0)
    loved = {_norm_track_key(t["artist"], t["title"]) for t in loved_tracks}

    matched = 0
    for t in rows:
        key = _norm_track_key(t.get("artist", ""), t.get("name", ""))
        signal = {"loved": key in loved, "playcount": plays.get(key)}
        t["mine"] = signal
        if signal["loved"] or signal["playcount"]:
            matched += 1
    return matched, len(rows)


def _mine_label(mine):
    """Render the `mine` signal as the roster's left-hand column: `♥42`, `17`, or `·`."""
    if not mine:
        return "·"
    plays = mine.get("playcount")
    if mine.get("loved"):
        return f"♥{plays}" if plays else "♥"
    return str(plays) if plays else "·"


# ---------------------------------------------------------------- arc sequencing
# Turn a flat pool of already-curated tracks into an intentional "ride ups and
# downs" running order. The taste part — how energetic each track is — stays with
# the caller (Spotify killed /audio-features, so it can't be derived): each track
# carries an integer `energy` level (e.g. 1=mellow, 2=mid, 3=banger). This code
# only owns the *arc math*: an oscillating energy curve with an eased opening and
# a firm soft-landing tail, mapped onto the real per-level supply so the counts
# always work out, then filled avoiding adjacent same-artist clumps.
#
# It's rank-based: positions are sorted by the curve and the lowest-energy tracks
# go to the lowest-curve positions, so the curve's absolute scale is irrelevant —
# only its *shape* matters, and any set of energy levels maps on exactly.

_SPARK_BLOCKS = "▁▂▃▄▅▆▇█"


def _spark(values):
    """Compact unicode sparkline of a numeric sequence, for a stderr eyeball."""
    if not values:
        return ""
    lo, hi = min(values), max(values)
    if hi == lo:
        return _SPARK_BLOCKS[0] * len(values)
    span = len(_SPARK_BLOCKS) - 1
    return "".join(_SPARK_BLOCKS[int((v - lo) / (hi - lo) * span)] for v in values)


def _arc_curve(n, waves, open_frac, land_frac):
    """Per-position energy-shape values for an n-track arc.

    `waves` full oscillations across the run; the first `open_frac` is capped so
    the mix eases in rather than opening on a peak; the last `land_frac` is forced
    into a strictly-descending tail (below the oscillation's trough) so those
    positions reliably rank lowest and become the soft landing.
    """
    if n <= 1:
        return [0.0] * n
    land_start = 1.0 - land_frac
    out = []
    for i in range(n):
        x = i / (n - 1)
        o = math.sin(2 * math.pi * waves * x)
        if x < open_frac:
            o = min(o, 0.0)  # keep the opening off the peak
        if x > land_start:
            t = (x - land_start) / land_frac  # 0..1 across the tail
            o = -1.0 - t                      # strictly descending -> lowest ranks
        out.append(o)
    return out


def _break_runs(assign, max_run, body_end):
    """De-clump runs longer than `max_run` in assign[:body_end] via local swaps.

    Best-effort, not a hard cap: it only swaps with a *nearby* differently-tiered
    slot, so it thins the shoulders where energies mingle but deliberately leaves
    a genuine peak or trough intact — those broad plateaus are the arc's "ups and
    downs," and breaking them would need to steal from far-away extrema and flatten
    the whole shape. A pure reorder of the assignment, so per-level counts (and the
    overall curve) are untouched. Bounded passes — never loops indefinitely. The
    landing tail (assign[body_end:]) is exempt: a long mellow run there is the
    intended wind-down.
    """
    if max_run <= 0:
        return
    for _ in range(3):
        i = 0
        while i < body_end:
            j = i
            while j + 1 < body_end and assign[j + 1] == assign[i]:
                j += 1
            if j - i + 1 > max_run:
                target = None
                for k in range(j + 1, min(body_end, j + 12)):
                    if assign[k] != assign[i]:
                        target = k
                        break
                if target is None:
                    for k in range(i - 1, max(-1, i - 12), -1):
                        if assign[k] != assign[i]:
                            target = k
                            break
                if target is not None:
                    assign[j], assign[target] = assign[target], assign[j]
            i = j + 1


def _lane(t):
    """A track's lane, normalized. Missing/blank means "unlabeled"."""
    return (t.get("lane") or "").strip().lower()


def _longest_lane_run(seq):
    """Longest run of consecutive identical lanes.

    Unlabeled tracks break a run rather than extending one — the same rule the
    artist check uses, so a partly-labeled input degrades instead of misreporting.
    """
    best, run, prev = 0, 0, None
    for t in seq:
        lane = _lane(t)
        run = run + 1 if (lane and lane == prev) else (1 if lane else 0)
        prev = lane or None
        best = max(best, run)
    return best


def _lane_run_allowance(lane, run_len, lane_left, slots_left, max_lane_run):
    """The longest run this lane may take *right now* — normally the requested cap.

    A lane with more tracks left than the rest of the set can separate has to run
    long somewhere, and the honest place to put that is evenly: `left + run_len`
    tracks over the `others + 1` groups still available is `ceil(...)` apiece.

    Recomputing it at every slot, rather than raising the cap once up front, is
    what keeps the tail from starving. An eager greedy spends every separator as
    early as it can and banks the whole excess into one wall at the end — 40
    dance-punk of 50 came out as ten tidy pairs followed by twenty in a row, which
    is the complaint this is here to fix. As separators are spent the allowance
    re-tightens on its own, so the mix closes on the same texture it opened with.
    """
    left = lane_left.get(lane, 0)
    groups = (slots_left - left) + 1  # one more group than there are separators
    return max(max_lane_run, -(-(left + run_len) // groups))


def _lane_excess(seq, max_lane_run):
    """How many tracks sit beyond the allowed lane run — the repair pass's cost,
    and the honest shortfall to report when a lane is simply too big."""
    if max_lane_run <= 0:
        return 0
    bad, run, prev = 0, 0, None
    for t in seq:
        lane = _lane(t)
        run = run + 1 if (lane and lane == prev) else (1 if lane else 0)
        prev = lane or None
        if run > max_lane_run:
            bad += 1
    return bad


def _artist_clashes(seq):
    """Adjacent same-artist pairs — guarded so a lane repair can't trade one flaw
    for the other."""
    prev, n = None, 0
    for t in seq:
        a = (t.get("artist") or "").lower()
        if a and a == prev:
            n += 1
        prev = a or None
    return n


def _pick_index(pool, recent, run_lane, run_len, run_allowance, lane_left):
    """Index of the best next track in `pool` for the slot being filled.

    Two soft constraints, ranked: an artist not among the last few picks (the older,
    stricter invariant) and a lane that would not push the running lane past
    `run_allowance` (0 = lanes off). Candidates satisfying both win, then
    artist-only, then lane-only, then whatever is left — so a corner never fails, it
    just spends the cheaper constraint.

    Ties break toward the lane with the **most tracks still unplaced**, so a big
    lane is placed as often as its allowance permits rather than hoarded for the
    end. With no lanes in play every candidate scores alike and this degrades to
    the old first-fit exactly.
    """
    best = None
    for idx, cand in enumerate(pool):
        a = (cand.get("artist") or "").lower()
        lane = _lane(cand)
        artist_ok = not a or a not in recent  # empty artist never counts as a clash
        lane_ok = (not lane or not run_allowance or lane != run_lane
                   or run_len < run_allowance)
        tier = 0 if (artist_ok and lane_ok) else 1 if artist_ok else 2 if lane_ok else 3
        key = (tier, -lane_left.get(lane, 0), idx)
        if best is None or key < best:
            best = key
    return best[2]


def _repair_lane_runs(seq, max_lane_run, window=48, passes=2):
    """Swap tracks of the *same energy* to break lane runs the fill couldn't avoid.

    A same-energy swap leaves the arc bit-for-bit intact — only which track sits in
    a given energy slot changes — so this can improve lane spacing but never bend
    the shape. A swap is kept solely if it lowers (lane excess, artist clashes) as
    a pair, so it cannot buy lane spacing with a same-artist adjacency. Bounded: a
    couple of passes over a local `window`, since the useful partner is a nearby
    separator, not a track fifty slots away.
    """
    if max_lane_run <= 0 or not seq:
        return
    n = len(seq)
    for _ in range(passes):
        cost = (_lane_excess(seq, max_lane_run), _artist_clashes(seq))
        if not cost[0]:
            return
        for i in range(n):
            if not cost[0]:
                return
            for j in range(max(0, i - window), min(n, i + window + 1)):
                if j == i or seq[j]["energy"] != seq[i]["energy"]:
                    continue
                seq[i], seq[j] = seq[j], seq[i]
                trial = (_lane_excess(seq, max_lane_run), _artist_clashes(seq))
                if trial < cost:
                    cost = trial
                    break
                seq[i], seq[j] = seq[j], seq[i]  # no gain — put it back


def _sequence_arc(tracks, waves=3.0, open_frac=0.07, land_frac=0.14,
                  max_run=3, avoid_window=2, max_lane_run=2, seed=0):
    """Order `tracks` (dicts with int `energy`, optional `artist`/`lane`) into an arc.

    Returns a new list, same items, reordered: energy oscillates `waves` times,
    eases in, and lands soft; runs longer than `max_run` are de-clumped at the
    transition shoulders (genuine peaks/troughs may still sustain — see
    `_break_runs`); adjacent same-artist avoided within the last `avoid_window`
    picks where supply allows; no more than `max_lane_run` consecutive tracks share
    a lane — and where one lane is too big for that to be possible, its unavoidable
    long runs are spread evenly rather than banked into one wall at the end (see
    `_lane_run_allowance`). Deterministic for a given `seed`.

    The two axes are separable by construction: the curve fixes *which energy* each
    position gets, and lane only decides *which track of that energy* fills it — so
    de-clumping genre can never bend the energy arc.
    """
    n = len(tracks)
    if n == 0:
        return []
    levels = sorted({t["energy"] for t in tracks})
    supply = {lvl: sum(1 for t in tracks if t["energy"] == lvl) for lvl in levels}

    curve = _arc_curve(n, waves, open_frac, land_frac)
    # rank positions low->high by curve; hand the lowest-energy level the
    # lowest-curve positions, on up — an exact fit against real supply.
    order = sorted(range(n), key=lambda i: curve[i])
    assign = [None] * n
    idx = 0
    for lvl in levels:
        for _ in range(supply[lvl]):
            assign[order[idx]] = lvl
            idx += 1

    land_start = 1.0 - land_frac
    body_end = next((i for i in range(n) if i / (n - 1) > land_start), n) if n > 1 else n
    _break_runs(assign, max_run, body_end)

    rng = random.Random(seed)
    pools = {lvl: [t for t in tracks if t["energy"] == lvl] for lvl in levels}
    for lvl in pools:
        rng.shuffle(pools[lvl])

    lane_left = {}
    for t in tracks:
        lane = _lane(t)
        if lane:
            lane_left[lane] = lane_left.get(lane, 0) + 1

    out, recent = [], []
    run_lane, run_len = None, 0
    for i in range(n):
        pool = pools[assign[i]]
        allowance = (_lane_run_allowance(run_lane, run_len, lane_left, n - i, max_lane_run)
                     if run_lane and max_lane_run > 0 else 0)
        pick = pool.pop(_pick_index(pool, recent, run_lane, run_len,
                                    allowance, lane_left))
        out.append(pick)

        a = (pick.get("artist") or "").lower()
        if avoid_window and a:
            recent.append(a)
            recent = recent[-avoid_window:]

        lane = _lane(pick)
        if lane:
            lane_left[lane] -= 1
            run_len = run_len + 1 if lane == run_lane else 1
            run_lane = lane
        else:
            run_lane, run_len = None, 0  # an unlabeled track separates two runs

    _repair_lane_runs(out, max_lane_run)
    return out


# ---------------------------------------------------------------- commands

def cmd_sources(args):
    pls = _cached_playlists()
    db = _db()
    # `folder:` tags are the web Manage page's local folders, not real tags — keep
    # them out of the tag column and surface the folder on its own.
    tags, folders = {}, {}
    for pid, tag in db.execute("SELECT playlist_id, tag FROM playlist_tag"):
        if tag.startswith("folder:"):
            folders[pid] = tag[len("folder:"):]
        else:
            tags.setdefault(pid, []).append(tag)
    usage = {pid: (uc, lu) for pid, uc, lu in
             db.execute("SELECT playlist_id, use_count, last_used FROM playlist_usage")}

    rows = []
    for p in pls:
        pid = p["id"]
        ptags = tags.get(pid, [])
        if args.tag and args.tag not in ptags:
            continue
        if args.search and args.search.lower() not in p["name"].lower():
            continue
        uc = usage.get(pid, (0, None))[0]
        rows.append({
            "id": pid,
            "name": p["name"],
            "tracks": p.get("tracks", {}).get("total"),
            "tags": ptags,
            "folder": folders.get(pid, ""),
            "use_count": uc,
        })
    rows.sort(key=lambda r: (-(r["use_count"] or 0), r["name"].lower()))

    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return
    for r in rows:
        tagstr = f"  [{', '.join(r['tags'])}]" if r["tags"] else ""
        print(f"{r['use_count']:>3}x  {r['tracks'] or '?':>5} trk  {r['name']}{tagstr}  ({r['id']})")


def cmd_tracks(args):
    pls = _cached_playlists()
    by_id = {p["id"]: p["name"] for p in pls}
    by_name = {p["name"]: p["id"] for p in pls}
    sp = _client()

    frozen = set()
    if args.exclude_cooldown:
        cutoff = datetime.now() - timedelta(days=_cooldown_days())
        frozen = {u for (u,) in _db().execute(
            "SELECT track_id FROM track_history WHERE provider='spotify' AND used_at >= ?",
            (cutoff.isoformat(sep=" "),),
        )}

    iced = set() if args.show_iced else {r[0] for r in _iced_rows(_db())}

    seen      = set()
    n_iced    = 0
    for src in args.sources:
        name, pid = _resolve(src, by_id, by_name)
        for uri, tname, artist in _fetch_tracks_cached(sp, pid, use_cache=not args.no_cache):
            if uri in seen or uri in frozen:
                continue
            if uri in iced:
                n_iced += 1
                continue
            seen.add(uri)
            # Tab-separated so the skill can parse uri / name / artist cleanly.
            print(f"{uri}\t{tname}\t{artist}")
    if args.exclude_cooldown:
        print(f"# excluded {len(frozen)} tracks in cooldown", file=sys.stderr)
    if n_iced:
        print(f"# excluded {n_iced} iced track(s) — see `ice list`", file=sys.stderr)


def cmd_roster(args):
    """Compact deep-cut candidate pool: per-album band selection, cooldown-aware.

    This is the curation aid — instead of dumping every track, it surfaces a
    bigger *roster* of the good-but-not-obvious cuts (default) so a mix has
    surprise, or the bangers (`--top`) for albums you want to lead with hits.
    """
    _validate_filters(args)
    pls = _cached_playlists()
    by_id = {p["id"]: p["name"] for p in pls}
    by_name = {p["name"]: p["id"] for p in pls}
    sp = _client()

    cooldown_days = _cooldown_days()
    last_used = _last_used_map()
    now = datetime.now()

    # Manual ice box: track_id -> thaw_at. Hard exclusion by default; --show-iced
    # reveals them labelled with the 🧊 column so you can review before thawing.
    iced_map = {r[0]: r[3] for r in _iced_rows(_db())}

    # --jitter: how many times each track has been used (track_history rows),
    # summed per album below to drive the band's random walk.
    hist_counts = {}
    if args.jitter:
        for (tid,) in _db().execute(
            "SELECT track_id FROM track_history WHERE provider='spotify'"
        ):
            hist_counts[tid] = hist_counts.get(tid, 0) + 1

    def ice_of(uri):
        """(label, frozen, days) — days since last play, or None if never played."""
        lu = last_used.get(uri)
        if lu is None:
            return "·", False, None
        days = (now - lu).days
        if days < cooldown_days:
            return f"❄{days}d", True, days
        return f"~{days}d", False, days

    seen = set()
    rows = []
    excluded = dict.fromkeys(_FILTER_REASONS, 0)
    for src in args.sources:
        name, pid = _resolve(src, by_id, by_name)
        # bucket this source's tracks by album, in first-seen order
        albums, order = {}, []
        for t in _fetch_tracks_rich_cached(sp, pid, use_cache=not args.no_cache):
            if t["album_id"] not in albums:
                albums[t["album_id"]] = []
                order.append(t["album_id"])
            albums[t["album_id"]].append(t)
        for aid in order:
            ranked = sorted(albums[aid], key=lambda t: -t["pop"])
            if args.jitter and not args.top and len(ranked) > args.per_album:
                hi = len(ranked) - args.per_album
                base_skip = max(0, min(args.skip_top, hi))
                steps = sum(hist_counts.get(t["uri"], 0) for t in ranked)
                skip = _jitter_skip(aid, base_skip, steps, hi)
                selection = ranked[skip:skip + args.per_album]
            else:
                selection = _band_select(ranked, args.skip_top, args.per_album, args.top)
            for t in selection:
                if t["uri"] in seen:
                    continue
                if t["uri"] in iced_map:
                    if not args.show_iced:
                        continue  # hard exclusion — iced tracks aren't build candidates
                    label, frozen, days = _ice_label(iced_map[t["uri"]]), True, None
                else:
                    label, frozen, days = ice_of(t["uri"])
                # --fresh: drop anything still on ice. --thawed: only played-but-thawed.
                if args.fresh and frozen:
                    continue
                if args.thawed and (days is None or frozen):
                    continue
                reason = _filter_reason(t, args)
                if reason:
                    excluded[reason] += 1
                    continue
                seen.add(t["uri"])
                t = dict(t, ice=label, frozen=frozen, days=days, source=name)
                rows.append(t)

    # optional per-artist cap to stop one artist clumping the roster
    if args.per_artist:
        capped, counts = [], {}
        for t in sorted(rows, key=lambda r: -r["pop"]):  # keep each artist's stronger cuts
            key = t["artist"].lower()
            if counts.get(key, 0) >= args.per_artist:
                continue
            counts[key] = counts.get(key, 0) + 1
            capped.append(t)
        rows = capped

    if args.sample and len(rows) > args.sample:
        rng = random.Random(args.seed)
        rows = rng.sample(rows, args.sample)

    # --tags: annotate the final roster with each lead artist's Last.fm tags.
    # Opt-in and lazy so plain roster stays offline and Last.fm-free. Hard-fail
    # if the key is missing (a silent tagless roster would look like a bug); a
    # per-artist blip degrades to empty tags via lastfm_service, not a crash.
    if args.tags:
        import lastfm_service
        try:
            lastfm_service.get_api_key()
        except lastfm_service.LastfmError as exc:
            sys.exit(f"--tags needs a Last.fm key: {exc}")

        def _lookup(artist):
            return [d["name"] for d in lastfm_service.artist_top_tags(artist, limit=5)]

        resolved, total, unknown = _annotate_tags(rows, _lookup)
        print(f"# tags: {resolved}/{total} artists resolved ({unknown} unknown to Last.fm)",
              file=sys.stderr)

    # --mine: annotate with Cory's own scrobble signal (loved + playcount). Runs last,
    # so it only ever sees rows that already survived ice, cooldown and every filter —
    # an excluded track can't pick up a marker. Two bulk reads for the whole roster
    # (not one call per track), then local name matching.
    if args.mine and rows:
        import lastfm_service
        try:
            lastfm_service.get_api_key()
            user = lastfm_service.get_user()
        except lastfm_service.LastfmError as exc:
            sys.exit(f"--mine needs Last.fm credentials: {exc}")

        top = lastfm_service.user_top_tracks(user, period="overall",
                                            use_cache=not args.no_cache)
        loved = lastfm_service.user_loved_tracks(user, use_cache=not args.no_cache)
        matched, total = _annotate_mine(rows, top, loved)
        print(f"# mine: {matched}/{total} candidates carry a personal signal "
              f"({len(top)} scrobbled, {len(loved)} loved for {user})", file=sys.stderr)

    total_ms = sum(t.get("duration_ms") or 0 for t in rows)
    # Filter notes go to stderr in both modes (like --tags) — a --json caller wants
    # to know the era filter ate half the pool just as much as a text one does.
    notes = {
        "era": "outside the year range (unknown year counts as out)",
        "explicit": "flagged explicit",
        "pop": "outside the popularity band",
    }
    for reason in _FILTER_REASONS:
        if excluded[reason]:
            print(f"# excluded {excluded[reason]} track(s) {notes[reason]}", file=sys.stderr)

    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return
    for t in rows:
        tagstr = f"  [{', '.join(t['tags'])}]" if t.get("tags") else ""
        year = f" · {t['year']}" if t.get("year") else ""
        exp = "  [E]" if t.get("explicit") else ""
        # --mine rides in the left gutter beside pop/ice so it scans as a column and
        # stays clear of --tags' trailing bracket.
        mine = f"  {_mine_label(t.get('mine')):>5}" if args.mine else ""
        print(f"{t['pop']:>3}  {t['ice']:>5}{mine}  {_mmss(t.get('duration_ms')):>5}  {t['uri']}  "
              f"{t['name']} — {t['artist']}  ({t['album']}{year}){exp}{tagstr}")
    mode = "top" if args.top else ("band+jitter" if args.jitter else "band")
    print(f"# {len(rows)} candidates from {len(args.sources)} source(s); "
          f"runtime {_hhmm(total_ms)}; "
          f"mode={mode}, cooldown={cooldown_days}d", file=sys.stderr)


def cmd_sequence(args):
    """Reorder a curated pool into an energy arc — pure local logic, no Spotify.

    Input lines: `uri<TAB>energy[<TAB>artist[<TAB>name[<TAB>lane]]]` (blank / '#'
    lines ignored). `energy` is your own integer level (e.g. 1=mellow, 2=mid,
    3=banger) and `lane` your own genre bucket (e.g. dance-punk, disco) — the two
    bits taste has to supply. Later columns are optional but positional: to give a
    lane without an artist/name, leave those columns empty. Prints the same lines
    reordered into the arc (trailing empty columns trimmed, so it round-trips),
    plus an energy sparkline and the achieved lane run on stderr.
    """
    raw = (open(args.tracks, encoding="utf-8").read() if args.tracks
           else sys.stdin.read()).splitlines()
    tracks = []
    for line in raw:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        parts = line.split("\t")
        uri = parts[0].strip()
        if not uri.startswith("spotify:track:"):
            continue
        if len(parts) < 2:
            sys.exit(f"line missing an energy column: {line!r}")
        try:
            energy = int(float(parts[1]))
        except ValueError:
            sys.exit(f"bad energy {parts[1]!r} on line: {line!r}")
        tracks.append({
            "uri": uri,
            "energy": energy,
            "artist": parts[2].strip() if len(parts) > 2 else "",
            "name": parts[3].strip() if len(parts) > 3 else "",
            "lane": parts[4].strip() if len(parts) > 4 else "",
        })
    if not tracks:
        sys.exit("No 'spotify:track:<TAB>energy' lines found on stdin or in --tracks.")

    seq = _sequence_arc(tracks, waves=args.waves, land_frac=args.landing,
                        max_run=args.max_run, max_lane_run=args.max_lane_run,
                        seed=args.seed)

    for t in seq:
        if args.uris_only:
            print(t["uri"])
            continue
        cols = [t["uri"], str(t["energy"]), t["artist"], t["name"], t["lane"]]
        while len(cols) > 2 and not cols[-1]:
            cols.pop()  # trim trailing empties: a 2/3/4-column input comes back as one
        print("\t".join(cols))

    levels = sorted({t["energy"] for t in tracks})
    print(f"# {len(seq)} tracks · waves={args.waves:g} · landing={args.landing:g} · "
          f"levels={levels}", file=sys.stderr)
    print("# energy " + _spark([t["energy"] for t in seq]), file=sys.stderr)

    lane_counts = {}
    for t in tracks:
        lane = _lane(t)
        if lane:
            lane_counts[lane] = lane_counts.get(lane, 0) + 1
    if lane_counts:
        spread = ", ".join(f"{lane}×{c}" for lane, c in
                           sorted(lane_counts.items(), key=lambda kv: (-kv[1], kv[0])))
        run = _longest_lane_run(seq)
        over = _lane_excess(seq, args.max_lane_run)
        flag = "" if not over else f" — {over} forced over (supply, not a bug)"
        print(f"# lanes  {spread}", file=sys.stderr)
        print(f"# longest lane run {run} (--max-lane-run {args.max_lane_run}){flag}",
              file=sys.stderr)


def _resolve_sources(source_tokens):
    """Resolve --source tokens (id or name substring) to playlist ids, order-preserving.

    Resolved up front (before any Spotify write) so a typo aborts cleanly instead of
    leaving a created playlist with no usage recorded.
    """
    pls = _cached_playlists()
    by_id = {p["id"]: p["name"] for p in pls}
    by_name = {p["name"]: p["id"] for p in pls}
    ids = [_resolve(s, by_id, by_name)[1] for s in source_tokens]
    return list(dict.fromkeys(ids))


def _record_source_usage(db, source_ids, provider="spotify"):
    """Bump playlist_usage for each source playlist — identical semantics to the web
    app's _record_usage: an existing row's use_count += 1, a new row starts at 1.

    This makes a mix build count toward a source's "most used" rank exactly like a
    Block Mix / Album Blast build does — the very signal `sources`/`roster` rank on.
    Without it the skill was blind to its own builds (a source used by 5 mixes still
    read 1×). Upsert leans on the table's UNIQUE(playlist_id, provider) constraint.
    """
    now = datetime.now().isoformat(sep=" ")
    for pid in source_ids:
        db.execute(
            "INSERT INTO playlist_usage (playlist_id, provider, use_count, last_used) "
            "VALUES (?, ?, 1, ?) "
            "ON CONFLICT(playlist_id, provider) DO UPDATE SET "
            "use_count = use_count + 1, last_used = excluded.last_used",
            (pid, provider, now),
        )
    db.commit()


def cmd_create(args):
    source_ids = _resolve_sources(args.source) if args.source else []
    if args.uris_file:
        with open(args.uris_file, encoding="utf-8") as fh:
            raw = fh.read().split()
    else:
        raw = sys.stdin.read().split()
    # Accept full lines (uri<TAB>name<TAB>artist) or bare URIs; take the first field.
    uris = []
    for tok in raw:
        tok = tok.strip()
        if tok.startswith("spotify:track:"):
            uris.append(tok)
    # de-dupe, preserve order
    uris = list(dict.fromkeys(uris))
    if not uris:
        sys.exit("No spotify:track: URIs found on stdin or in --uris-file.")

    name = args.name
    if not args.no_prefix and not name.startswith(MIX_PREFIX):
        name = f"{MIX_PREFIX} {name}"

    sp = _client()
    uid = sp.me()["id"]
    pl = sp.user_playlist_create(
        uid, name, public=args.public, description=args.desc or ""
    )
    for i in range(0, len(uris), 100):
        sp.playlist_add_items(pl["id"], uris[i:i + 100])

    url = pl["external_urls"]["spotify"]
    print(f"CREATED  {name}  ({len(uris)} tracks)")
    print(url)

    if args.record:
        db = _db()
        now = datetime.now().isoformat(sep=" ")
        db.execute(
            "INSERT INTO created_playlist (playlist_id, name, tool, provider, url, "
            "created_at, alive, track_count) VALUES (?,?,?,?,?,?,1,?)",
            (pl["id"], name, "Mix", "spotify", url, now, len(uris)),
        )
        if args.cooldown:
            db.executemany(
                "INSERT INTO track_history (track_id, provider, used_at) VALUES (?, 'spotify', ?)",
                [(u, now) for u in uris],
            )
        if source_ids:
            _record_source_usage(db, source_ids)
        db.commit()
        extra = " + cooldown" if args.cooldown else ""
        usage = f" + usage×{len(source_ids)}" if source_ids else ""
        print(f"recorded to created_playlist{extra}{usage}")

    # Make it resolvable by roster/tracks/--source right away (issue #13) — without
    # this the skill can create a playlist it then can't use as a source until the
    # web app next refreshes the blob. Last, so a cache-write hiccup can't cost the
    # --record bookkeeping above, which matters more.
    n_cached = _cache_upsert_playlist(pl, uid, track_total=len(uris))
    print(f"playlist_cache updated ({n_cached} playlists) — usable as a source now")


def cmd_replace(args):
    source_ids = _resolve_sources(args.source) if args.source else []
    """Replace all tracks in an existing playlist in place (keeps the same URL)."""
    if args.uris_file:
        with open(args.uris_file, encoding="utf-8") as fh:
            raw = fh.read().split()
    else:
        raw = sys.stdin.read().split()
    uris = list(dict.fromkeys(t.strip() for t in raw if t.strip().startswith("spotify:track:")))
    if not uris:
        sys.exit("No spotify:track: URIs found on stdin or in --uris-file.")

    sp = _client()
    pid = args.playlist.split(":")[-1].split("/")[-1]  # accept id, uri, or url
    # First 100 replace the contents; the rest are appended in order.
    sp.playlist_replace_items(pid, uris[:100])
    for i in range(100, len(uris), 100):
        sp.playlist_add_items(pid, uris[i:i + 100])
    if args.name or args.desc:
        sp.playlist_change_details(
            pid, **({"name": args.name} if args.name else {}),
            **({"description": args.desc} if args.desc else {}),
        )

    pl = sp.playlist(pid, fields="id,external_urls,name")
    url = pl["external_urls"]["spotify"]
    print(f"REPLACED  {pl['name']}  ({len(uris)} tracks)")
    print(url)

    if args.record:
        db = _db()
        now = datetime.now().isoformat(sep=" ")
        # Update the existing history row if we have one; else insert a fresh record.
        cur = db.execute(
            "UPDATE created_playlist SET name=?, track_count=?, url=?, created_at=?, alive=1 "
            "WHERE playlist_id=?",
            (args.name or pl["name"], len(uris), url, now, pid),
        )
        if cur.rowcount == 0:
            db.execute(
                "INSERT INTO created_playlist (playlist_id, name, tool, provider, url, "
                "created_at, alive, track_count) VALUES (?,?,?,?,?,?,1,?)",
                (pid, args.name or pl["name"], "Mix", "spotify", url, now, len(uris)),
            )
        if args.cooldown:
            db.executemany(
                "INSERT INTO track_history (track_id, provider, used_at) VALUES (?, 'spotify', ?)",
                [(u, now) for u in uris],
            )
        if source_ids:
            _record_source_usage(db, source_ids)
        db.commit()
        extra = " + cooldown" if args.cooldown else ""
        usage = f" + usage×{len(source_ids)}" if source_ids else ""
        print(f"updated created_playlist{extra}{usage}")

    # Keep the blob honest about the new name/count — `replace` takes a raw id, so
    # the playlist may not be in the cache at all yet (issue #13). Last, for the
    # same reason as in cmd_create.
    _cache_upsert_playlist(dict(pl, id=pl.get("id") or pid), sp.me()["id"],
                           track_total=len(uris))


def _cached_pool_tracks():
    """Every track in every `.mix_cache` file: ((playlist_id, track) pairs, n_stale).

    Purely local — the point of `find-artists` is that it costs no Spotify calls,
    so this deliberately does NOT re-pull stale-schema files the way
    `_fetch_tracks_rich_cached` would; a pre-v2 blob is read as-is and its tracks
    simply lack duration/year. `n_stale` counts those pools so the caller can say
    so rather than rendering a missing runtime as a plausible-looking `0:00`.
    """
    out, stale = [], 0
    for path in sorted(glob.glob(os.path.join(MIX_CACHE_DIR, "*.json"))):
        pid = os.path.splitext(os.path.basename(path))[0]
        try:
            with open(path, encoding="utf-8") as fh:
                blob = json.load(fh)
        except (OSError, ValueError):
            continue
        if blob.get("version") != MIX_CACHE_VERSION:
            stale += 1
        for t in blob.get("tracks") or []:
            out.append((pid, t))
    return out, stale


def cmd_find_artists(args):
    """Sweep the cached pools for a roster of artists — offline, no Spotify calls.

    Answers "does the library contain any of these N artists?", which name-based
    `sources --search` can't: pools are named things like `Broken Metric Stars`,
    which says nothing about its contents. `--missing` is the genuinely useful
    half — it tells you whether a themed mix is buildable at all.

    Matching is deliberately substring-on-the-cached-`artist`-field: punctuation and
    acronym names ('!!!', 'CSS') resolve badly through Spotify's artist search, and
    this sidesteps it entirely.
    """
    names = list(args.names or [])
    if args.file:
        with open(args.file, encoding="utf-8") as fh:
            names += [ln.strip() for ln in fh
                      if ln.strip() and not ln.lstrip().startswith("#")]
    names = list(dict.fromkeys(names))  # de-dupe, preserve order
    if not names:
        sys.exit("Give at least one artist name, or --file with one name per line.")

    pool_names = {p["id"]: p["name"] for p in (_read_playlist_cache() or [])}
    pairs, n_stale = _cached_pool_tracks()
    pools_seen = {pid for pid, _ in pairs}

    # One pass over the cache per run, not per name: bucket every match by the query
    # that found it, deduping a track that appears in several pools while keeping the
    # list of pools it came from.
    found = {n: {} for n in names}
    lowered = [(n, n.lower()) for n in names]
    for pid, t in pairs:
        artist = (t.get("artist") or "").lower()
        if not artist:
            continue
        for name, needle in lowered:
            if needle in artist:
                hit = found[name].setdefault(t["uri"], dict(t, pools=[]))
                pool = pool_names.get(pid, pid)
                if pool not in hit["pools"]:
                    hit["pools"].append(pool)

    missing = [n for n in names if not found[n]]

    if args.missing:
        if args.json:
            print(json.dumps(missing, ensure_ascii=False, indent=2))
        else:
            for n in missing:
                print(n)
        print(f"# {len(missing)}/{len(names)} name(s) absent from {len(pools_seen)} cached pool(s)",
              file=sys.stderr)
        return

    results = []
    for name in names:
        hits = sorted(found[name].values(), key=lambda r: -(r.get("pop") or 0))
        results.append({"name": name, "hits": len(hits),
                        "tracks": hits[:args.top] if args.top else hits})

    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
    else:
        for r in results:
            if not r["hits"]:
                print(f"{r['name']} — no hits")
                continue
            pools = {p for t in r["tracks"] for p in t["pools"]}
            print(f"{r['name']} — {r['hits']} track(s), {len(pools)} pool(s)")
            for t in r["tracks"]:
                year = f" · {t['year']}" if t.get("year") else ""
                # A pre-v2 blob has no duration at all — show that, don't render it
                # as 0:00, which reads as a real zero-length track.
                dur = _mmss(t["duration_ms"]) if "duration_ms" in t else "—"
                where = t["pools"][0] + (f" +{len(t['pools']) - 1}" if len(t["pools"]) > 1 else "")
                print(f"  {t.get('pop') or 0:>3}  {dur:>5}  {t['uri']}  "
                      f"{t['name']} — {t['artist']}  ({t.get('album', '')}{year})  [{where}]")

    hit_names = len(names) - len(missing)
    print(f"# {hit_names}/{len(names)} name(s) found across {len(pools_seen)} cached pool(s); "
          f"uncached pools are invisible — a nil result means \"not in any pool pulled so far\", "
          f"not \"not in the library\"", file=sys.stderr)
    if n_stale:
        print(f"# {n_stale} pool(s) still on an older cache schema — their rows show no "
              f"runtime/year until a roster re-pulls them", file=sys.stderr)
    if missing:
        print(f"# missing: {', '.join(missing)}", file=sys.stderr)


def cmd_refresh_cache(args):
    """Rebuild the app's playlist_cache from Spotify (issue #13).

    The Flask app is otherwise the only writer, on a TTL — so after the app has
    been closed a while the blob goes stale and newly made playlists can't be
    resolved as sources. This is the skill's own way to unstick it.
    """
    sp = _client()
    uid = sp.me()["id"]
    pls = []
    res = sp.current_user_playlists(limit=50)
    while res:
        pls.extend(res["items"])
        res = sp.next(res) if res.get("next") else None
    if not pls:
        sys.exit("Spotify returned no playlists — leaving the existing cache alone.")
    _write_playlist_cache(pls, uid)
    print(f"playlist_cache refreshed — {len(pls)} playlists for {uid}")


def _extract_track_uri(tok):
    """Return a spotify:track: URI from a URI or open.spotify.com URL, else None."""
    if tok.startswith("spotify:track:"):
        return tok
    if "open.spotify.com/track/" in tok:
        return "spotify:track:" + tok.split("track/")[-1].split("?")[0].split("/")[0]
    return None


def cmd_ice(args):
    """Manage the shared ice box: add (never / timed), list, thaw. DB is the source
    of truth — the Flask app's builds read the same table, so a freeze here takes
    effect everywhere immediately."""
    db = _db()
    _ensure_ice_table(db)

    if args.action == "list":
        rows = _iced_rows(db)
        if not rows:
            print("Ice box is empty.")
            return
        for tid, name, artist, thaw_at, reason in rows:
            when = "never" if thaw_at is None else f"thaws {thaw_at.split('.')[0]}"
            why  = f"  — {reason}" if reason else ""
            print(f"{_ice_label(thaw_at):>7}  {name or '?'} — {artist or '?'}  ({when}){why}  [{tid}]")
        print(f"# {len(rows)} track(s) on ice", file=sys.stderr)
        return

    if not args.track:
        sys.exit(f"ice {args.action} needs a track (URI/URL/name).")

    if args.action == "thaw":
        rows = db.execute(
            "SELECT track_id, name, artist FROM track_ice WHERE provider='spotify'"
        ).fetchall()
        uri = _extract_track_uri(args.track)
        if uri:
            hits = [r for r in rows if r[0] == uri]
        else:
            t = args.track.lower()
            hits = [r for r in rows if t in (r[1] or "").lower()]
        if not hits:
            sys.exit(f"No iced track matches {args.track!r}.")
        if len(hits) > 1:
            names = "; ".join(f"{n} — {a}" for _, n, a in hits[:8])
            sys.exit(f"{args.track!r} is ambiguous — matches: {names}. Use the URI.")
        db.execute("DELETE FROM track_ice WHERE track_id=? AND provider='spotify'", (hits[0][0],))
        db.commit()
        print(f"THAWED  {hits[0][1]} — {hits[0][2]}  [{hits[0][0]}]")
        return

    # action == add — resolve URI directly, else search Spotify and take the top hit
    sp  = _client()
    uri = _extract_track_uri(args.track)
    if uri:
        t = sp.track(uri)
    else:
        items = sp.search(q=args.track, type="track", limit=5)["tracks"]["items"]
        if not items:
            sys.exit(f"No Spotify track found for {args.track!r}.")
        t, uri = items[0], items[0]["uri"]
    name   = t["name"]
    artist = ", ".join(a["name"] for a in t["artists"])

    if args.months:
        thaw = _add_months(datetime.now(), args.months).isoformat(sep=" ")
        dur  = f"{args.months}-month ice (thaws {thaw.split()[0]})"
    else:
        thaw, dur = None, "never-list"
    now = datetime.now().isoformat(sep=" ")
    db.execute(
        "INSERT INTO track_ice (track_id, provider, name, artist, frozen_at, thaw_at, reason) "
        "VALUES (?, 'spotify', ?, ?, ?, ?, ?) "
        "ON CONFLICT(track_id, provider) DO UPDATE SET "
        "name=excluded.name, artist=excluded.artist, frozen_at=excluded.frozen_at, "
        "thaw_at=excluded.thaw_at, reason=excluded.reason",
        (uri, name, artist, now, thaw, args.reason),
    )
    db.commit()
    why = f"  — {args.reason}" if args.reason else ""
    print(f"ICED  {name} — {artist}  ({dur}){why}  [{uri}]")


def main():
    ap = argparse.ArgumentParser(description="Plumbing for the `mix` skill.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("sources", help="list candidate source playlists")
    s.add_argument("--tag", help="only playlists carrying this tag")
    s.add_argument("--search", help="only playlists whose name contains this")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_sources)

    t = sub.add_parser("tracks", help="dump uri/name/artist from sources")
    t.add_argument("sources", nargs="+", help="playlist names (substring) or ids")
    t.add_argument("--exclude-cooldown", action="store_true",
                   help="drop tracks still inside the app's cooldown window")
    t.add_argument("--show-iced", action="store_true",
                   help="include ice-boxed tracks (excluded by default)")
    t.add_argument("--no-cache", action="store_true",
                   help="force a live pull, ignoring (and refreshing) the disk cache")
    t.set_defaults(func=cmd_tracks)

    ro = sub.add_parser("roster", help="deep-cut candidate pool (band select, cooldown-aware)")
    ro.add_argument("sources", nargs="+", help="playlist names (substring) or ids")
    ro.add_argument("--top", action="store_true",
                    help="take each album's HIGHEST-popularity tracks (bangers) instead of the deep-cut band")
    ro.add_argument("--per-album", type=int, default=3, help="tracks to take per album (default 3)")
    ro.add_argument("--skip-top", type=int, default=2,
                    help="band mode: skip this many top hits before selecting (default 2)")
    ro.add_argument("--per-artist", type=int, default=0,
                    help="cap tracks per artist across the roster (0 = no cap)")
    ro.add_argument("--jitter", action="store_true",
                    help="random-walk each album's band pick by its play history, so a "
                         "much-used album drifts off its sweet spot (spreads the wear; band mode only)")
    ro.add_argument("--fresh", action="store_true", help="drop tracks still on ice (within cooldown)")
    ro.add_argument("--thawed", action="store_true",
                    help="only tracks you've played before but are now off ice (throwbacks)")
    ro.add_argument("--show-iced", action="store_true",
                    help="reveal ice-boxed tracks (🧊 column) instead of hiding them")
    ro.add_argument("--year-min", type=int, default=0, metavar="YYYY",
                    help="only tracks whose album released in this year or later "
                         "(unknown release year is treated as out of range)")
    ro.add_argument("--year-max", type=int, default=0, metavar="YYYY",
                    help="only tracks whose album released in this year or earlier "
                         "(unknown release year is treated as out of range)")
    ro.add_argument("--no-explicit", action="store_true",
                    help="drop tracks Spotify flags explicit")
    ro.add_argument("--pop-min", type=int, default=None, metavar="N",
                    help="popularity floor, 0-100 (composes with --top / band mode)")
    ro.add_argument("--pop-max", type=int, default=None, metavar="N",
                    help="popularity ceiling, 0-100 — '--top --pop-max 70' is "
                         "\"each album's biggest track, minus the ubiquitous ones\"")
    ro.add_argument("--sample", type=int, default=0, help="randomly keep N of the candidates (0 = all)")
    ro.add_argument("--seed", type=int, default=None, help="seed for --sample (reproducible)")
    ro.add_argument("--no-cache", action="store_true",
                    help="force a live pull, ignoring (and refreshing) the disk cache")
    ro.add_argument("--mine", action="store_true",
                    help="annotate with your own Last.fm signal (loved + scrobble count); "
                         "needs LASTFM_API_KEY + LASTFM_USER")
    ro.add_argument("--tags", action="store_true",
                    help="annotate each row with the lead artist's top-5 Last.fm tags "
                         "(needs LASTFM_API_KEY)")
    ro.add_argument("--json", action="store_true")
    ro.set_defaults(func=cmd_roster)

    q = sub.add_parser("sequence", help="order a curated pool into an energy arc (offline)")
    q.add_argument("--tracks",
                   help="file of 'uri<TAB>energy[<TAB>artist[<TAB>name[<TAB>lane]]]' lines (else stdin)")
    q.add_argument("--waves", type=float, default=3.0, help="number of energy peaks across the mix (default 3)")
    q.add_argument("--landing", type=float, default=0.14,
                   help="fraction of the tail reserved for a soft wind-down (default 0.14)")
    q.add_argument("--max-run", type=int, default=3,
                   help="de-clump body runs longer than this at transition shoulders "
                        "(best-effort; true peaks/troughs may sustain; default 3)")
    q.add_argument("--max-lane-run", type=int, default=2,
                   help="max consecutive tracks sharing a lane, if the input has a "
                        "lane column (0 disables; default 2)")
    q.add_argument("--seed", type=int, default=0, help="seed for the within-level shuffle (reproducible)")
    q.add_argument("--uris-only", action="store_true", help="print only URIs (pipe straight to create)")
    q.set_defaults(func=cmd_sequence)

    c = sub.add_parser("create", help="create a private playlist from URIs")
    c.add_argument("--name", required=True)
    c.add_argument("--desc", default="")
    c.add_argument("--uris-file", help="file of URIs (else read stdin)")
    c.add_argument("--public", action="store_true", help="make public (default private)")
    c.add_argument("--no-prefix", action="store_true",
                   help=f"don't prepend the '{MIX_PREFIX}' name tag")
    c.add_argument("--record", action="store_true",
                   help="log to created_playlist (shows in Recently Created)")
    c.add_argument("--cooldown", action="store_true",
                   help="with --record, also write tracks to track_history")
    c.add_argument("--source", action="append", metavar="PLAYLIST",
                   help="a source playlist (id or name substring) this mix drew from; "
                        "with --record, bumps its use_count like a web build. Repeatable.")
    c.set_defaults(func=cmd_create)

    r = sub.add_parser("replace", help="replace all tracks in an existing playlist in place")
    r.add_argument("--playlist", required=True, help="playlist id, uri, or url to overwrite")
    r.add_argument("--name", help="optionally rename the playlist")
    r.add_argument("--desc", help="optionally reset the description")
    r.add_argument("--uris-file", help="file of URIs (else read stdin)")
    r.add_argument("--record", action="store_true",
                   help="update the created_playlist row (or insert if missing)")
    r.add_argument("--cooldown", action="store_true",
                   help="with --record, also write tracks to track_history")
    r.add_argument("--source", action="append", metavar="PLAYLIST",
                   help="a source playlist (id or name substring) this mix drew from; "
                        "with --record, bumps its use_count like a web build. Repeatable.")
    r.set_defaults(func=cmd_replace)

    rc = sub.add_parser("refresh-cache",
                        help="re-read your Spotify library into the app's playlist_cache")
    rc.set_defaults(func=cmd_refresh_cache)

    fa = sub.add_parser("find-artists",
                        help="sweep the cached pools for a roster of artists (offline)")
    fa.add_argument("names", nargs="*", help="artist names (case-insensitive substring)")
    fa.add_argument("--file", help="file of artist names, one per line ('#' comments ok)")
    fa.add_argument("--top", type=int, default=3,
                    help="tracks to show per matched artist, by popularity (0 = all)")
    fa.add_argument("--missing", action="store_true",
                    help="list ONLY the names with no hits — what the library lacks")
    fa.add_argument("--json", action="store_true")
    fa.set_defaults(func=cmd_find_artists)

    i = sub.add_parser("ice", help="manual ice box: never-list / timed freeze, shared with the app")
    i.add_argument("action", choices=["add", "list", "thaw"])
    i.add_argument("track", nargs="?",
                   help="add: URI/URL or search query; thaw: URI or name substring")
    i.add_argument("--months", type=int, default=0,
                   help="add: timed ice of N months (default: never-list)")
    i.add_argument("--never", action="store_true",
                   help="add: explicit never-list (the default when no --months)")
    i.add_argument("--reason", default=None, help="add: why (e.g. 'heard to death')")
    i.set_defaults(func=cmd_ice)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
