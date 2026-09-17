#!/usr/bin/env python
"""
profile_helper.py — plumbing for the `profile` skill.

Builds a music-taste profile of another Spotify user (a friend, a family member)
from their public playlists, so the skill never re-derives the fetch/count/compare
dance by hand:

    pull           fetch a user's profile + every public playlist + all tracks,
                   recovering old `spotify:local:` entries, and cache to disk
    stats          artist counts, popularity distribution, era spread, and the
                   per-playlist `added_at` ranges that reveal a stale library
    overlap        shared artists vs Cory's own library, and the N-way
                   intersection across two or more profiled friends
    resolve-local  re-resolve unplayable local-file entries against the Spotify
                   catalogue so they can actually be used in a mix

Auth reuses `mix_helper._client()` (the Flask app's cached `.cache` token), so
there is one source of truth for login. Every command here is READ-ONLY against
Spotify — nothing in this file writes to a playlist or to the app's DB.

Examples:
    python profile_helper.py pull gregulate
    python profile_helper.py stats gregulate
    python profile_helper.py stats dukejansen --pool "2013 08 nostalgia"
    python profile_helper.py overlap dukejansen
    python profile_helper.py overlap dukejansen gregulate
    python profile_helper.py resolve-local dukejansen --limit 40
"""
import argparse
import collections
import json
import glob
import os
import statistics
import sys
import urllib.parse
from datetime import datetime

# The sibling `mix` skill owns the Spotify auth dance; import its helper rather
# than re-implementing _client(), so there is exactly one login path to maintain.
SKILL_DIR = os.path.dirname(os.path.abspath(__file__))
MIX_SKILL_DIR = os.path.join(os.path.dirname(SKILL_DIR), "mix")
if MIX_SKILL_DIR not in sys.path:
    sys.path.insert(0, MIX_SKILL_DIR)
import mix_helper as mh  # noqa: E402

REPO_ROOT = mh.REPO_ROOT
MIX_CACHE_DIR = mh.MIX_CACHE_DIR
# One JSON per profiled user; git-ignored, created lazily. A pull is dozens of
# playlist_items calls, so it is cached by default and refreshed with --no-cache.
PROFILE_CACHE_DIR = os.path.join(REPO_ROOT, ".profile_cache")

# Spotify restricts algorithmic/editorial playlists (Blend, Discover Weekly,
# Release Radar, Daily Mix) for apps created after late 2024: the API 404s and
# the web page is behind reCAPTCHA + a login gate. See SKILL.md for the fix.
ALGORITHMIC_PREFIX = "37i9dQZF1E"


# ---------------------------------------------------------------- infra

def _cache_file(user_id):
    return os.path.join(PROFILE_CACHE_DIR, f"{user_id}.json")


def _read_profile(user_id, required=True):
    try:
        with open(_cache_file(user_id), encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        if required:
            sys.exit(f"No cached profile for {user_id!r}. Run: profile_helper.py pull {user_id}")
        return None


def _write_profile(user_id, blob):
    """Write the pull atomically (tmp + replace) so a crash can't corrupt it."""
    os.makedirs(PROFILE_CACHE_DIR, exist_ok=True)
    tmp = _cache_file(user_id) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(blob, fh, ensure_ascii=False)
    os.replace(tmp, _cache_file(user_id))


def _decode_local(uri):
    """Recover (artist, album, title) from a `spotify:local:` URI.

    Old local-file playlists carry no track object — `playlist_items` returns a
    stub whose only real content is the URI itself, which encodes the metadata
    URL-quoted with '+' for spaces:

        spotify:local:The+Beatles:Abbey+Road:I+Want+You:467

    These are unplayable (they cannot be added to a playlist) but they are still
    perfectly good TASTE data, and on some users they are the bulk of the library
    — so they are recovered here rather than silently dropped as empty playlists.
    """
    parts = uri.split(":")
    dec = urllib.parse.unquote_plus
    artist = dec(parts[2]) if len(parts) > 2 else ""
    album = dec(parts[3]) if len(parts) > 3 else ""
    title = dec(parts[4]) if len(parts) > 4 else ""
    return artist, album, title


def _fetch_playlist_tracks(sp, pid):
    """Every track of a playlist, keeping local-file entries and `added_at`.

    Deliberately NOT reusing mix_helper._fetch_tracks_rich: that one drops
    anything without a track id (i.e. every local file) and doesn't carry
    added_at, both of which are load-bearing for profiling.
    """
    out = []
    res = sp.playlist_items(pid, additional_types=["track"], limit=100)
    while res:
        for it in res["items"]:
            t = it.get("track") or {}
            uri = t.get("uri") or ""
            added = (it.get("added_at") or "")[:10]
            if uri.startswith("spotify:local:"):
                artist, album, title = _decode_local(uri)
                out.append({
                    "uri": uri, "name": title, "artists": [artist] if artist else [],
                    "album": album, "pop": None, "ms": t.get("duration_ms") or 0,
                    "release": "", "added": added, "local": True,
                })
            elif uri:
                alb = t.get("album") or {}
                out.append({
                    "uri": uri, "name": t.get("name") or "",
                    "artists": [a["name"] for a in t.get("artists", [])],
                    "album": alb.get("name") or "", "pop": t.get("popularity"),
                    "ms": t.get("duration_ms") or 0,
                    "release": (alb.get("release_date") or "")[:4],
                    "added": added, "local": False,
                })
        res = sp.next(res) if res.get("next") else None
    return out


def _pools(blob, include_followed=False):
    """The playlists to count taste from — owned only unless asked otherwise.

    A followed playlist is someone else's curation; it says the user liked it
    enough to follow, which is weaker evidence than a list they built.
    """
    return [p for p in blob["playlists"]
            if (p["owned"] or include_followed) and p.get("tracks")]


def _artist_counter(pools):
    c = collections.Counter()
    for p in pools:
        for t in p["tracks"]:
            for a in t["artists"]:
                if a:
                    c[a] += 1
    return c


def _cory_artists():
    """artist(lowercased) -> how many of Cory's own playlists contain them.

    Read from the mix skill's `.mix_cache` track pulls, which is the only place
    Cory's actual track contents live on disk (the app's DB caches names only).
    """
    counts = collections.Counter()
    files = glob.glob(os.path.join(MIX_CACHE_DIR, "*.json"))
    for f in files:
        try:
            with open(f, encoding="utf-8") as fh:
                d = json.load(fh)
        except (OSError, ValueError):
            continue
        items = d.get("tracks") if isinstance(d, dict) else d
        if not isinstance(items, list):
            continue
        seen = set()
        for t in items:
            if isinstance(t, dict) and t.get("artist"):
                for a in str(t["artist"]).split(","):
                    if a.strip():
                        seen.add(a.strip().lower())
        for a in seen:
            counts[a] += 1
    return counts, len(files)


# ---------------------------------------------------------------- pull

def cmd_pull(args):
    cached = _read_profile(args.user, required=False)
    if cached and not args.no_cache:
        print(f"(cached pull from {cached.get('fetched_at', '?')} — use --no-cache to refresh)\n")
        _print_pull(cached)
        return

    sp = mh._client()
    try:
        u = sp.user(args.user)
    except Exception as e:
        sys.exit(f"Could not fetch user {args.user!r}: {e}")

    playlists, off = [], 0
    while True:
        res = sp.user_playlists(args.user, limit=50, offset=off)
        for p in res["items"]:
            if not p:
                continue
            playlists.append({
                "id": p["id"], "name": p["name"],
                "owner": p["owner"]["id"], "owned": p["owner"]["id"] == args.user,
                "total": p["tracks"]["total"],
            })
        if not res.get("next"):
            break
        off += 50

    for p in playlists:
        if p["id"].startswith(ALGORITHMIC_PREFIX):
            p["tracks"] = []
            p["error"] = "algorithmic playlist — not readable via the API"
            continue
        try:
            p["tracks"] = _fetch_playlist_tracks(sp, p["id"])
        except Exception as e:              # one dead playlist shouldn't kill the pull
            p["tracks"] = []
            p["error"] = str(e)[:120]

    blob = {
        "user_id": args.user,
        "display_name": u.get("display_name"),
        "followers": (u.get("followers") or {}).get("total"),
        "url": (u.get("external_urls") or {}).get("spotify"),
        "fetched_at": datetime.now().isoformat(sep=" ", timespec="seconds"),
        "playlists": playlists,
    }
    _write_profile(args.user, blob)
    _print_pull(blob)


def _print_pull(blob):
    owned = [p for p in blob["playlists"] if p["owned"]]
    print(f"{blob['display_name']}  ({blob['user_id']})  followers: {blob['followers']}")
    print(f"{len(blob['playlists'])} public playlists — {len(owned)} owned, "
          f"{len(blob['playlists']) - len(owned)} followed\n")
    print(f"  {'tracks':>7} {'local':>6}  {'added_at range':25}  name")
    for p in sorted(blob["playlists"], key=lambda x: (not x["owned"], -len(x.get("tracks") or []))):
        tr = p.get("tracks") or []
        loc = sum(1 for t in tr if t["local"])
        dates = sorted(t["added"] for t in tr if t["added"])
        span = f"{dates[0]} -> {dates[-1]}" if dates else "-"
        flag = "." if p["owned"] else "f"
        note = f"   !! {p['error']}" if p.get("error") else ""
        print(f"{flag} {len(tr):>7} {loc:>6}  {span:25}  {p['name'][:44]}{note}")
    print("\n(. = owned, f = followed)")


# ---------------------------------------------------------------- stats

def cmd_stats(args):
    blob = _read_profile(args.user)
    pools = _pools(blob, args.include_followed)
    if args.pool:
        pools = [p for p in pools if args.pool.lower() in p["name"].lower()]
        if not pools:
            sys.exit(f"No playlist matches {args.pool!r}.")

    tracks = [t for p in pools for t in p["tracks"]]
    local = [t for t in tracks if t["local"]]
    scope = "owned + followed" if args.include_followed else "owned only"
    print(f"=== {blob['display_name']} ({blob['user_id']}) — {len(pools)} playlists, {scope} ===")
    print(f"{len(tracks)} tracks  ({len(local)} local-file, unplayable but counted for taste)\n")

    # --- staleness: the single most important sanity check on a profile
    dates = sorted(t["added"] for t in tracks if t["added"])
    if dates:
        newest = dates[-1]
        age_days = (datetime.now() - datetime.strptime(newest, "%Y-%m-%d")).days
        verdict = ("CURRENT" if age_days < 365 else
                   "STALE — profile reflects the past, not present-day taste")
        print(f"--- RECENCY ---\noldest add {dates[0]}   newest add {newest}   "
              f"({age_days // 30} months ago)  => {verdict}\n")

    # --- popularity: the deep-cut vs hits read
    pops = [t["pop"] for t in tracks if t["pop"] is not None]
    if pops:
        med, mean = statistics.median(pops), statistics.mean(pops)
        read = ("DEEP-CUT listener — popularity is a negative signal for them" if med < 25 else
                "MIDDLE — mixes recognizable and obscure" if med < 50 else
                "POPULAR / hits listener")
        print(f"--- POPULARITY --- n={len(pops)}  mean={mean:.1f}  median={med}  => {read}")
        buckets = collections.Counter(min(p // 10 * 10, 90) for p in pops)
        top = max(buckets.values())
        for k in sorted(buckets):
            print(f"  {k:>2}-{k + 9}: {'#' * (buckets[k] * 40 // top)} {buckets[k]}")
        print()

    # --- era
    decades = collections.Counter(t["release"][:3] + "0s" for t in tracks if t["release"])
    if decades:
        print("--- ERA --- " + ", ".join(f"{d}:{n}" for d, n in sorted(decades.items())) + "\n")

    # --- artists overall then per pool
    c = _artist_counter(pools)
    print(f"--- TOP {args.top} ARTISTS (all pools) ---")
    print(", ".join(f"{a}({n})" for a, n in c.most_common(args.top)) + "\n")
    if not args.pool and len(pools) > 1:
        print("--- TOP ARTISTS PER POOL ---")
        for p in sorted(pools, key=lambda x: -len(x["tracks"])):
            pc = _artist_counter([p])
            if not pc:
                continue
            pdates = sorted(t["added"] for t in p["tracks"] if t["added"])
            span = f"{pdates[0]}->{pdates[-1]}" if pdates else "-"
            print(f"\n  [{p['name'][:40]}]  {len(p['tracks'])} trk  {span}")
            print("    " + ", ".join(f"{a}({n})" for a, n in pc.most_common(12)))


# ---------------------------------------------------------------- overlap

def cmd_overlap(args):
    blobs = [_read_profile(u) for u in args.users]
    sets = []
    for b in blobs:
        c = _artist_counter(_pools(b, include_followed=True))
        sets.append({a.lower(): n for a, n in c.items()})

    if args.no_cory and len(blobs) == 1:
        sys.exit("Nothing to compare: pass a second user, or drop --no-cory.")

    cory = collections.Counter()
    if not args.no_cory:
        cory, nfiles = _cory_artists()
        print(f"(Cory's side read from {nfiles} .mix_cache pulls — "
              f"only playlists the mix skill has touched)\n")
        for b, s in zip(blobs, sets):
            shared = [(a, s[a], cory[a]) for a in s if a in cory]
            shared.sort(key=lambda x: (-x[2], -x[1]))
            print(f"=== {b['display_name']} x CORY — {len(shared)} shared artists ===")
            for a, theirs, mine in shared[:args.top]:
                print(f"  {a[:38]:38} them:{theirs:>4}  cory_playlists:{mine:>3}")
            print()

    if len(blobs) > 1:
        common = set(sets[0])
        for s in sets[1:]:
            common &= set(s)
        label = ""
        if cory:
            common &= set(cory)
            label = " + CORY"
        names = " x ".join(b["display_name"] or b["user_id"] for b in blobs)
        # Rank by the weakest link so an artist strong in everyone's library beats
        # one that is huge for a single person and incidental for the rest.
        ranked = sorted(common, key=lambda a: -min([s[a] for s in sets] +
                                                   ([cory[a]] if cory else [])))
        print(f"=== INTERSECTION: {names}{label} — {len(ranked)} artists ===")
        print("(ranked by weakest link — these are the only places every taste meets)\n")
        for a in ranked[:args.top]:
            cols = "  ".join(
                f"{(b['display_name'] or b['user_id']).split()[0][:8]}:{s[a]:>4}"
                for b, s in zip(blobs, sets))
            extra = f"  cory_pls:{cory[a]:>3}" if cory else ""
            print(f"  {a[:34]:34} {cols}{extra}")


# ---------------------------------------------------------------- resolve-local

def _norm_artist(s):
    """Loose artist key: casefold and drop a leading 'the' so 'The Shins' == 'Shins'."""
    s = " ".join(s.lower().replace("&", "and").split())
    return s[4:] if s.startswith("the ") else s


def _first_artist_match(results, want):
    """First search result actually credited to `want`, else None.

    Spotify's non-fielded search will confidently return a completely different
    song by a completely different artist when the query has no good match, so a
    resolved local file is only trustworthy if the artist survives the round trip.
    """
    if not want:
        return results[0] if results else None
    target = _norm_artist(want)
    for r in results:
        for a in r.get("artists", []):
            got = _norm_artist(a["name"])
            if got == target or got in target or target in got:
                return r
    return None


def cmd_resolve_local(args):
    blob = _read_profile(args.user)
    pools = _pools(blob, include_followed=False)
    if args.pool:
        pools = [p for p in pools if args.pool.lower() in p["name"].lower()]
    locals_ = [t for p in pools for t in p["tracks"] if t["local"]]
    # One search per distinct (artist, title) — the same song often recurs across mixes.
    uniq, seen = [], set()
    for t in locals_:
        key = (t["artists"][0].lower() if t["artists"] else "", t["name"].lower())
        if key in seen or not key[1]:
            continue
        seen.add(key)
        uniq.append(t)
    todo = uniq[:args.limit]
    print(f"{len(locals_)} local entries, {len(uniq)} distinct; resolving {len(todo)} "
          f"(--limit to change)\n")

    sp = mh._client()
    hits = misses = 0
    out = []
    for t in todo:
        artist = t["artists"][0] if t["artists"] else ""
        try:
            res = sp.search(f'track:"{t["name"]}" artist:"{artist}"',
                            type="track", limit=1)["tracks"]["items"]
            if not res:                      # fielded search is strict; retry loose
                res = sp.search(f'{artist} {t["name"]}',
                                type="track", limit=5)["tracks"]["items"]
        except Exception as e:
            print(f"  !! {artist} — {t['name']}: {e}")
            continue
        # The loose fallback happily returns an unrelated song by an unrelated
        # artist, which would silently poison a playlist — so require the artist
        # to actually match before accepting a hit.
        r = _first_artist_match(res, artist)
        if r:
            got = ", ".join(a["name"] for a in r["artists"])
            print(f"  OK   {artist[:22]:22} — {t['name'][:30]:30} -> "
                  f"{got[:22]:22} — {r['name'][:28]}")
            out.append(r["uri"])
            hits += 1
        else:
            print(f"  MISS {artist[:22]:22} — {t['name'][:30]}")
            misses += 1
    print(f"\nresolved {hits}, missed {misses}")
    if args.out and out:
        # newline="\n" matters on Windows: default text mode writes CRLF and the
        # trailing \r rides along into every URI, which Spotipy rejects as a bad id.
        with open(args.out, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("\n".join(out) + "\n")
        print(f"wrote {len(out)} URIs -> {args.out}")
    print("\nNOTE: a match is the top search hit — a different mix/remaster/live cut is\n"
          "possible. Skim the mapping above before shipping these into a playlist.")


# ---------------------------------------------------------------- cli

def main():
    ap = argparse.ArgumentParser(description="Plumbing for the `profile` skill.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("pull", help="fetch a user's public playlists + tracks (cached)")
    p.add_argument("user", help="Spotify user id (from /user/<id> in their profile URL)")
    p.add_argument("--no-cache", action="store_true", help="force a live re-pull")
    p.set_defaults(func=cmd_pull)

    s = sub.add_parser("stats", help="artists, popularity, era, and recency")
    s.add_argument("user")
    s.add_argument("--pool", help="only playlists whose name contains this")
    s.add_argument("--include-followed", action="store_true",
                   help="also count playlists they follow but didn't make")
    s.add_argument("--top", type=int, default=40, help="artists to list (default 40)")
    s.set_defaults(func=cmd_stats)

    o = sub.add_parser("overlap", help="shared artists vs Cory, and N-way intersection")
    o.add_argument("users", nargs="+", help="one or more already-pulled user ids")
    o.add_argument("--no-cory", action="store_true", help="compare the users only")
    o.add_argument("--top", type=int, default=45)
    o.set_defaults(func=cmd_overlap)

    r = sub.add_parser("resolve-local", help="re-resolve local-file entries to real URIs")
    r.add_argument("user")
    r.add_argument("--pool", help="only playlists whose name contains this")
    r.add_argument("--limit", type=int, default=50, help="max searches (default 50)")
    r.add_argument("--out", help="write resolved URIs to this file")
    r.set_defaults(func=cmd_resolve_local)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
