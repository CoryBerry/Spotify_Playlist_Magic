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
              it to the app's DB (CreatedPlaylist + TrackHistory) like a real build

Auth reuses the same Spotipy `.cache` token the Flask app writes, so no browser
login is needed as long as a valid token exists. Reads are always safe; `create`
is the only writing verb and must be invoked explicitly.

Examples:
    python mix_helper.py sources --search chill
    python mix_helper.py sources --tag selects
    python mix_helper.py tracks "Cory's Chilled Playlist" 43M34ZEoIBEMbe9SDA1atB --exclude-cooldown
    python mix_helper.py create --name "Sunday Slow Burn" --desc "..." --uris-file picks.txt --record --cooldown
"""
import argparse
import json
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
DB_PATH = os.path.join(REPO_ROOT, "instance", "spotify_tools.db")
CACHE_PATH = os.path.join(REPO_ROOT, ".cache")
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


def _fetch_tracks(sp, pid):
    """All (uri, name, artist) for a playlist, following pagination."""
    out = []
    res = sp.playlist_items(pid, additional_types=["track"], limit=100)
    while res:
        for it in res["items"]:
            t = it.get("track")
            if t and t.get("id"):
                out.append((t["uri"], t["name"], ", ".join(a["name"] for a in t["artists"])))
        res = sp.next(res) if res.get("next") else None
    return out


def _fetch_tracks_rich(sp, pid):
    """Like _fetch_tracks but also carries album + popularity for band selection.

    playlist_items returns full track objects, so popularity and album ride along
    with no extra API calls.
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

    seen = set()
    for src in args.sources:
        name, pid = _resolve(src, by_id, by_name)
        for uri, tname, artist in _fetch_tracks(sp, pid):
            if uri in seen:
                continue
            if uri in frozen:
                continue
            seen.add(uri)
            # Tab-separated so the skill can parse uri / name / artist cleanly.
            print(f"{uri}\t{tname}\t{artist}")
    if args.exclude_cooldown:
        print(f"# excluded {len(frozen)} tracks in cooldown", file=sys.stderr)


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
        for t in _fetch_tracks_rich(sp, pid):
            if t["album_id"] not in albums:
                albums[t["album_id"]] = []
                order.append(t["album_id"])
            albums[t["album_id"]].append(t)
        for aid in order:
            ranked = sorted(albums[aid], key=lambda t: -t["pop"])
            for t in _band_select(ranked, args.skip_top, args.per_album, args.top):
                if t["uri"] in seen:
                    continue
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

    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return
    for t in rows:
        print(f"{t['pop']:>3}  {t['ice']:>5}  {t['uri']}  {t['name']} — {t['artist']}  ({t['album']})")
    print(f"# {len(rows)} candidates from {len(args.sources)} source(s); "
          f"mode={'top' if args.top else 'band'}, cooldown={cooldown_days}d", file=sys.stderr)


def cmd_create(args):
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
        db.commit()
        extra = " + cooldown" if args.cooldown else ""
        print(f"recorded to created_playlist{extra}")


def cmd_replace(args):
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
        db.commit()
        extra = " + cooldown" if args.cooldown else ""
        print(f"updated created_playlist{extra}")


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
    ro.add_argument("--sample", type=int, default=0, help="randomly keep N of the candidates (0 = all)")
    ro.add_argument("--seed", type=int, default=None, help="seed for --sample (reproducible)")
    ro.add_argument("--json", action="store_true")
    ro.set_defaults(func=cmd_roster)

    c = sub.add_parser("create", help="create a private playlist from URIs")
    c.add_argument("--name", required=True)
    c.add_argument("--desc", default="")
    c.add_argument("--uris-file", help="file of URIs (else read stdin)")
    c.add_argument("--public", action="store_true", help="make public (default private)")
    c.add_argument("--record", action="store_true",
                   help="log to created_playlist (shows in Recently Created)")
    c.add_argument("--cooldown", action="store_true",
                   help="with --record, also write tracks to track_history")
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
    r.set_defaults(func=cmd_replace)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
