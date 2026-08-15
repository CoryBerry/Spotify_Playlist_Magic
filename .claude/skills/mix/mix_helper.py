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

Auth reuses the same Spotipy `.cache` token the Flask app writes, so no browser
login is needed as long as a valid token exists. Reads are always safe; `create`
is the only writing verb and must be invoked explicitly.

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
import json
import math
import os
import random
import sqlite3
import sys
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
SCOPE = "playlist-read-private playlist-modify-private playlist-modify-public"


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


def _cached_playlists():
    """The app's 1-row playlist_cache blob: every playlist with name + track total."""
    row = _db().execute("SELECT data FROM playlist_cache LIMIT 1").fetchone()
    if not row:
        sys.exit("playlist_cache is empty. Open the app's Manage page once to populate it.")
    return json.loads(row[0])


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
        sys.exit(f"No playlist matches {source!r}.")
    if len(hits) > 1:
        exact = [(n, i) for n, i in hits if n.lower() == source.lower()]
        if len(exact) == 1:
            return exact[0][0], exact[0][1]
        names = ", ".join(n for n, _ in hits[:8])
        sys.exit(f"{source!r} is ambiguous — matches: {names}. Use a fuller name or the id.")
    return hits[0]


def _fetch_tracks_rich(sp, pid):
    """Every track for a playlist as a rich dict (uri/name/artist/album/pop).

    playlist_items returns full track objects, so popularity and album ride along
    with no extra API calls. This is the single source of truth for a track pull;
    the (uri, name, artist) tuple form is derived from it, so both the `tracks`
    and `roster` commands share one cache file per playlist.
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
    """
    live = _snapshot_id(sp, pid)
    if use_cache and live is not None:
        cached = _read_cache(pid)
        if cached and cached.get("snapshot_id") == live:
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


def _sequence_arc(tracks, waves=3.0, open_frac=0.07, land_frac=0.14,
                  max_run=3, avoid_window=2, seed=0):
    """Order `tracks` (dicts with int `energy`, optional `artist`) into an arc.

    Returns a new list, same items, reordered: energy oscillates `waves` times,
    eases in, and lands soft; runs longer than `max_run` are de-clumped at the
    transition shoulders (genuine peaks/troughs may still sustain — see
    `_break_runs`); adjacent same-artist avoided within the last `avoid_window`
    picks where supply allows. Deterministic for a given `seed`.
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

    out, recent = [], []
    for i in range(n):
        pool = pools[assign[i]]
        pick = None
        for k, cand in enumerate(pool):
            a = (cand.get("artist") or "").lower()
            if not a or a not in recent:  # empty artist never counts as a clash
                pick = pool.pop(k)
                break
        if pick is None:
            pick = pool.pop(0)  # forced same-artist only if unavoidable
        out.append(pick)
        a = (pick.get("artist") or "").lower()
        if avoid_window and a:
            recent.append(a)
            recent = recent[-avoid_window:]
    return out


# ---------------------------------------------------------------- commands

def cmd_sources(args):
    pls = _cached_playlists()
    db = _db()
    tags = {}
    for pid, tag in db.execute("SELECT playlist_id, tag FROM playlist_tag"):
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
            for t in _band_select(ranked, args.skip_top, args.per_album, args.top):
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

    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return
    for t in rows:
        tagstr = f"  [{', '.join(t['tags'])}]" if t.get("tags") else ""
        print(f"{t['pop']:>3}  {t['ice']:>5}  {t['uri']}  {t['name']} — {t['artist']}  ({t['album']}){tagstr}")
    print(f"# {len(rows)} candidates from {len(args.sources)} source(s); "
          f"mode={'top' if args.top else 'band'}, cooldown={cooldown_days}d", file=sys.stderr)


def cmd_sequence(args):
    """Reorder a curated pool into an energy arc — pure local logic, no Spotify.

    Input lines: `uri<TAB>energy[<TAB>artist[<TAB>name]]` (blank / '#' lines
    ignored). `energy` is your own integer level (e.g. 1=mellow, 2=mid, 3=banger)
    — the one bit taste has to supply. Prints the same lines reordered into the
    arc, plus an energy sparkline on stderr so you can eyeball the shape.
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
        })
    if not tracks:
        sys.exit("No 'spotify:track:<TAB>energy' lines found on stdin or in --tracks.")

    seq = _sequence_arc(tracks, waves=args.waves, land_frac=args.landing,
                        max_run=args.max_run, seed=args.seed)

    for t in seq:
        if args.uris_only:
            print(t["uri"])
            continue
        cols = [t["uri"], str(t["energy"])]
        if t["artist"] or t["name"]:
            cols.append(t["artist"])
        if t["name"]:
            cols.append(t["name"])
        print("\t".join(cols))

    levels = sorted({t["energy"] for t in tracks})
    print(f"# {len(seq)} tracks · waves={args.waves:g} · landing={args.landing:g} · "
          f"levels={levels}", file=sys.stderr)
    print("# energy " + _spark([t["energy"] for t in seq]), file=sys.stderr)


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

    sp = _client()
    uid = sp.me()["id"]
    pl = sp.user_playlist_create(
        uid, args.name, public=args.public, description=args.desc or ""
    )
    for i in range(0, len(uris), 100):
        sp.playlist_add_items(pl["id"], uris[i:i + 100])

    url = pl["external_urls"]["spotify"]
    print(f"CREATED  {args.name}  ({len(uris)} tracks)")
    print(url)

    if args.record:
        db = _db()
        now = datetime.now().isoformat(sep=" ")
        db.execute(
            "INSERT INTO created_playlist (playlist_id, name, tool, provider, url, "
            "created_at, alive, track_count) VALUES (?,?,?,?,?,?,1,?)",
            (pl["id"], args.name, "Mix", "spotify", url, now, len(uris)),
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

    pl = sp.playlist(pid, fields="external_urls,name")
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
    ro.add_argument("--fresh", action="store_true", help="drop tracks still on ice (within cooldown)")
    ro.add_argument("--thawed", action="store_true",
                    help="only tracks you've played before but are now off ice (throwbacks)")
    ro.add_argument("--show-iced", action="store_true",
                    help="reveal ice-boxed tracks (🧊 column) instead of hiding them")
    ro.add_argument("--sample", type=int, default=0, help="randomly keep N of the candidates (0 = all)")
    ro.add_argument("--seed", type=int, default=None, help="seed for --sample (reproducible)")
    ro.add_argument("--no-cache", action="store_true",
                    help="force a live pull, ignoring (and refreshing) the disk cache")
    ro.add_argument("--tags", action="store_true",
                    help="annotate each row with the lead artist's top-5 Last.fm tags "
                         "(needs LASTFM_API_KEY)")
    ro.add_argument("--json", action="store_true")
    ro.set_defaults(func=cmd_roster)

    q = sub.add_parser("sequence", help="order a curated pool into an energy arc (offline)")
    q.add_argument("--tracks", help="file of 'uri<TAB>energy[<TAB>artist[<TAB>name]]' lines (else stdin)")
    q.add_argument("--waves", type=float, default=3.0, help="number of energy peaks across the mix (default 3)")
    q.add_argument("--landing", type=float, default=0.14,
                   help="fraction of the tail reserved for a soft wind-down (default 0.14)")
    q.add_argument("--max-run", type=int, default=3,
                   help="de-clump body runs longer than this at transition shoulders "
                        "(best-effort; true peaks/troughs may sustain; default 3)")
    q.add_argument("--seed", type=int, default=0, help="seed for the within-level shuffle (reproducible)")
    q.add_argument("--uris-only", action="store_true", help="print only URIs (pipe straight to create)")
    q.set_defaults(func=cmd_sequence)

    c = sub.add_parser("create", help="create a private playlist from URIs")
    c.add_argument("--name", required=True)
    c.add_argument("--desc", default="")
    c.add_argument("--uris-file", help="file of URIs (else read stdin)")
    c.add_argument("--public", action="store_true", help="make public (default private)")
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
