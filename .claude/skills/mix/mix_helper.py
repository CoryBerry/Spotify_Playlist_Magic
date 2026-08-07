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
        return source, by_id[source]
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
