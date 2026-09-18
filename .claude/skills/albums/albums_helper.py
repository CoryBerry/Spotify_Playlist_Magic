"""Plumbing for the `albums` skill — build whole-album source pools.

Two jobs, and they are deliberately split the way `feed_service` splits fetch from
extract: everything that talks to Spotify lives in the `cmd_*` functions, and every
decision worth arguing about — is this a live record, which edition is the real one,
where does the bonus material start — is a pure function tested in test_albums.py.

This helper never creates or edits a playlist. It decides *which tracks, in what
order*, and writes a URI file; `mix_helper create|replace` ships it. That keeps one
Spotify write path for the repo, with its recording and cache-upsert already solved.

    PYTHONUTF8=1 python .claude/skills/albums/albums_helper.py releases --artist Tool
    PYTHONUTF8=1 python .claude/skills/albums/albums_helper.py plan --from albums.txt --uris out.txt
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "mix"))
from mix_helper import _client  # noqa: E402  — one auth path for the repo


# --- classification (pure) -------------------------------------------------

# Markers that say "this is a re-release of an album that already exists".
EDITION_RE = re.compile(
    r"\b(deluxe|expanded|extended|anniversary|remaster(ed)?|legacy|reissue|revisited|"
    r"\d+th\s+anniversary)\b", re.I)

LIVE_RE = re.compile(r"\b(live|unplugged|in\s+concert)\b", re.I)
REMIX_RE = re.compile(r"\b(remix|remixes|remixed|re-mixed|re-wired|rewired|re-load|"
                      r"dub|dubby|dubs|mixes|versions)\b", re.I)
COMPILATION_RE = re.compile(
    r"\b(greatest\s+hits|best\s+of|anthology|collection|essential|singles|"
    r"b-sides|rarities|retrospective)\b", re.I)

# Titles that mark a track as bonus material when it sits in a reissue's tail.
BONUS_TRACK_RE = re.compile(
    r"[(\[\-–]\s*[^)\]]*\b(live|demo|remix|alternate|alternative|acoustic|instrumental|"
    r"outtake|rough|early|unreleased|session|rehearsal|radio\s+edit|single\s+version|"
    r"mix|version|take)\b", re.I)


def base_name(name: str) -> str:
    """Strip edition markers and parentheticals so editions of one album group together.

    'Rumours (Super Deluxe)' and 'Rumours - 2004 Remaster' both reduce to 'rumours',
    which is what lets pick_edition see them as one album with several listings.
    """
    out = re.sub(r"[(\[][^)\]]*[)\]]", " ", name)          # (Deluxe Edition), [Remastered]
    if EDITION_RE.search(out):
        out = re.sub(r"\s+-\s+.*$", " ", out)              # "Album - 2004 Remaster"
    out = EDITION_RE.sub(" ", out)
    out = re.sub(r"[^\w\s]", " ", out)
    return re.sub(r"\s+", " ", out).strip().lower()


# The subset of edition markers that actually ADD tracks. A plain '(Remastered)' is
# a mastering change and carries the original tracklist, so pruning it would be wrong;
# a Deluxe or Anniversary box is what needs cutting back.
EXPANDED_RE = re.compile(
    r"\b(deluxe|expanded|extended|anniversary|legacy|collector|bonus\s+track|"
    r"\d+th\s+anniversary)\b", re.I)


def adds_tracks(name: str) -> bool:
    """True when a title claims extra material, not merely a new master."""
    return bool(EXPANDED_RE.search(name))


def is_edition(name: str) -> bool:
    """True when a title advertises itself as a re-release rather than the album."""
    return bool(EDITION_RE.search(name))


def classify(name: str, album_type: str, album_group: str, total_tracks: int,
             track_names: list | None = None) -> str:
    """Bucket a release: studio | ep | single | live | remix | compilation.

    Track names are optional but decisive for live records that don't say so in the
    title — a whole record of '... - Live' tracks is a live album whatever it's called.
    """
    if LIVE_RE.search(name):
        return "live"
    if track_names:
        lives = sum(1 for t in track_names if re.search(r"[(\[\-–]\s*live\b", t, re.I))
        if lives >= max(2, len(track_names) * 0.6):
            return "live"
    if REMIX_RE.search(name):
        return "remix"
    if album_type == "compilation" or COMPILATION_RE.search(name):
        return "compilation"
    if album_group == "single" or total_tracks <= 7:
        # Cory wants EPs in the pool. A 1-3 track release is a single and stays out.
        return "ep" if total_tracks >= 4 else "single"
    return "studio"


def flag_recycled(albums: list, threshold: float = 0.6) -> list:
    """Mark releases that are mostly re-runs of earlier ones. Returns the same list.

    The trap this exists for: Puscifer's 'In Case You Were Napping' (2025) is a
    chill-out compilation of fifteen songs that all already sit on earlier records,
    and nothing in its title says so. Title regexes cannot catch that; only comparing
    tracklists can. Anything at or above `threshold` overlap with *earlier* releases
    gets kind='recycled' so it drops out of DEFAULT_KEEP.

    Albums need a `track_names` key to be checked; ones without it are left alone,
    so this is a no-op unless the caller paid for the extra reads.
    """
    def norm(t):
        base = re.split(r"\s+[-–(\[]", t, maxsplit=1)[0]      # drop '- Live', '(Remix)'
        return re.sub(r"[^\w]", "", base).lower()

    seen = set()
    for a in order_albums(albums):
        names = a.get("track_names")
        if names is None:
            continue
        titles = {norm(n) for n in names if norm(n)}
        if titles and a.get("kind") in ("studio", "ep"):
            overlap = len(titles & seen) / len(titles)
            if overlap >= threshold:
                a["kind"] = "recycled"
                a["recycled_pct"] = round(overlap * 100)
            else:
                # Only originals establish precedence. A live or remix record must
                # never seed the baseline: 'Thirteenth Step - Live' shares a year
                # with 'Thirteenth Step' and was making the studio album itself
                # read as 100% recycled.
                seen |= titles
    return albums


# Buckets that land in a pool by default. Live is excluded but always *reported* —
# Cory's call was "exclude, but report them", so a skipped live record is one word
# away from being added instead of a reason to rebuild the list.
DEFAULT_KEEP = {"studio", "ep"}


def norm_artist(name: str) -> str:
    """Normalize an artist name for comparison: case, 'The', and punctuation dropped."""
    out = re.sub(r"^the\s+", "", (name or "").strip(), flags=re.I)
    return re.sub(r"[^\w]", "", out).lower()


def match_artist(requested: str, candidates: list) -> list:
    """Narrow album candidates to the artist actually asked for.

    Exists because of a real miss: searching 'The Beatles - Abbey Road' returns the
    genuine album only as '(Remastered)' and '(Super Deluxe Edition)', while a
    ukulele tribute act's cover is titled plain 'Abbey Road'. pick_edition prefers
    an unmarked title, so the tribute won. Exact artist matches are taken first and
    only fall back to containment when nothing matches exactly — otherwise
    'The Beatles' would happily accept 'The Beatles Complete On Ukulele'.
    """
    if not requested:
        return candidates
    want = norm_artist(requested)
    exact = [c for c in candidates if norm_artist(c.get("artist")) == want]
    if exact:
        return exact
    loose = [c for c in candidates if want and want in norm_artist(c.get("artist"))]
    return loose or candidates


def pick_edition(editions: list) -> dict:
    """Choose the listing that best represents the original album.

    Prefer a title with no edition marker; among those, the earliest release. Only
    when every listing is a reissue do we take one — earliest, then fewest tracks,
    so 'Rumours - 2004 Remaster' (11 tracks) wins over 'Rumours (Super Deluxe)' (58).
    """
    clean = [e for e in editions if not is_edition(e["name"])]
    pool = clean or editions
    return sorted(pool, key=lambda e: (e.get("release_date") or "9999",
                                       e.get("total_tracks") or 999))[0]


def canonical_length(editions: list):
    """Track count of the shortest clean edition — the yardstick for pruning a reissue.

    None when every listing is a reissue, which is the signal to fall back to
    marker-based tail pruning instead of a hard truncation.
    """
    clean = [e.get("total_tracks") for e in editions if not is_edition(e["name"])]
    clean = [n for n in clean if n]
    return min(clean) if clean else None


def prune_bonus(tracks: list, canonical=None, deluxe: bool = False):
    """Cut a reissue back toward its original tracklist. Returns (kept, note).

    Three passes, cheapest signal first:
      1. Extra discs — a multi-disc reissue keeps disc 1. This alone is what saves
         us from the 58-track Rumours box, with no title guessing at all.
      2. A canonical length from a sibling clean edition: truncate to it.
      3. Marker-based tail pruning: walk backwards while titles look like bonus
         material. Tail only — pruning mid-album would eat real tracks from records
         that legitimately have 'Version' or 'Mix' in a title.
    """
    if not tracks:
        return [], "empty"
    notes = []
    kept = tracks

    discs = {t.get("disc_number", 1) for t in kept}
    if len(discs) > 1:
        kept = [t for t in kept if t.get("disc_number", 1) == 1]
        notes.append("dropped %d extra disc(s)" % (len(discs) - 1))

    if canonical and len(kept) > canonical:
        cut = len(kept) - canonical
        kept = kept[:canonical]
        notes.append("truncated %d to the %d-track original" % (cut, canonical))
    elif deluxe:
        end = len(kept)
        while end > 1 and BONUS_TRACK_RE.search(kept[end - 1].get("name", "")):
            end -= 1
        if end < len(kept):
            notes.append("trimmed %d bonus track(s) off the tail" % (len(kept) - end))
            kept = kept[:end]
        elif not notes:
            notes.append("reissue with no prunable tail — VERIFY")

    return kept, "; ".join(notes) or "clean"


def _date_key(value: str) -> str:
    """Pad a partial Spotify date so string comparison stays chronological.

    Spotify returns '2003', '2003-01' or '2003-01-01' depending on how much the label
    filed. Compared raw, '2003' sorts *after* '2003-01-01', which is how a live album
    dated by year alone jumped ahead of the studio record it was taken from.
    """
    parts = (value or "9999").split("-")
    parts += ["00"] * (3 - len(parts))
    return "%s-%s-%s" % (parts[0], parts[1].zfill(2), parts[2].zfill(2))


def order_albums(albums: list) -> list:
    """Chronological by the album's earliest known release date, then by title.

    `sort_date` is the earliest date across an album's editions, so picking a 2004
    remaster as the best listing doesn't shove a 1977 record to the wrong end.
    """
    return sorted(albums, key=lambda a: (_date_key(a.get("sort_date") or a.get("release_date")),
                                         (a.get("name") or a.get("album") or "").lower()))


# --- Spotify reads ---------------------------------------------------------

def _all_releases(sp, artist_id: str) -> list:
    out, seen = [], set()
    for group in ("album", "single", "compilation"):
        offset = 0
        while True:
            res = sp.artist_albums(artist_id, album_type=group, country="US",
                                   limit=50, offset=offset)
            for al in res["items"]:
                if al["id"] in seen:
                    continue
                seen.add(al["id"])
                out.append({"id": al["id"], "name": al["name"],
                            "release_date": al.get("release_date", ""),
                            "album_type": al.get("album_type", ""),
                            "album_group": al.get("album_group", ""),
                            "total_tracks": al.get("total_tracks", 0),
                            "artist": al["artists"][0]["name"] if al.get("artists") else ""})
            if not res["next"]:
                break
            offset += 50
    return out


def _group_editions(releases: list) -> dict:
    groups = {}
    for r in releases:
        groups.setdefault(base_name(r["name"]), []).append(r)
    return groups


def _album_tracks(sp, album_id: str) -> list:
    items, offset = [], 0
    while True:
        res = sp.album_tracks(album_id, limit=50, offset=offset)
        items.extend(res["items"])
        if not res["next"]:
            break
        offset += 50
    return [{"uri": t["uri"], "name": t["name"], "disc_number": t.get("disc_number", 1)}
            for t in items if t.get("uri", "").startswith("spotify:track:")]


def _find_artist(sp, name: str):
    res = sp.search(q="artist:%s" % name, type="artist", limit=10)
    items = res["artists"]["items"]
    if not items:
        return None
    exact = [a for a in items if a["name"].lower() == name.lower()]
    return max(exact or items, key=lambda a: a["followers"]["total"])


# --- commands --------------------------------------------------------------

def cmd_releases(args):
    """Discovery: every release by these artists, classified, with a keep/skip call."""
    sp = _client()
    rows = []
    for name in args.artist:
        art = _find_artist(sp, name)
        if not art:
            print("!! no artist found for %r" % name, file=sys.stderr)
            continue
        for _key, eds in _group_editions(_all_releases(sp, art["id"])).items():
            best = pick_edition(eds)
            names = None
            if args.deep:
                names = [t["name"] for t in _album_tracks(sp, best["id"])]
            kind = classify(best["name"], best["album_type"], best["album_group"],
                            best["total_tracks"], track_names=names)
            rows.append({
                "track_names": names,
                "artist": art["name"], "album": best["name"], "id": best["id"],
                "release_date": best["release_date"],
                "sort_date": min(e.get("release_date") or "9999" for e in eds),
                "kind": kind, "total_tracks": best["total_tracks"],
                "keep": kind in DEFAULT_KEEP,
                "editions": [{"name": e["name"], "id": e["id"],
                              "total_tracks": e["total_tracks"],
                              "release_date": e["release_date"]} for e in eds],
            })
    if args.deep:
        flag_recycled(rows)
    for r in rows:
        r["keep"] = r["kind"] in DEFAULT_KEEP
        r.pop("track_names", None)
    rows = order_albums(rows)

    if args.json:
        print(json.dumps(rows, indent=2, ensure_ascii=False))
        return 0
    for r in rows:
        mark = "KEEP" if r["keep"] else "skip"
        extra = "  (%d editions)" % len(r["editions"]) if len(r["editions"]) > 1 else ""
        if r.get("recycled_pct"):
            extra += "  %d%% already on earlier records" % r["recycled_pct"]
        print("  [%s] %s  %-18s %-42s %-11s %2dt%s"
              % (mark, r["sort_date"][:4], r["artist"][:18], r["album"][:42],
                 r["kind"], r["total_tracks"], extra))
    kept = [r for r in rows if r["keep"]]
    skipped = sorted({r["kind"] for r in rows if not r["keep"]})
    print("\n%d to keep / %d releases. Skipped kinds: %s"
          % (len(kept), len(rows), ", ".join(skipped) or "none"))
    return 0


def _parse_album_lines(raw: str) -> list:
    return [ln.strip() for ln in raw.splitlines()
            if ln.strip() and not ln.strip().startswith("#")]


def _resolve_album(sp, line: str):
    """Resolve one 'Artist - Album' line (or a bare album id/URL) to the best edition."""
    tok = line.strip()
    m = re.search(r"(?:album[:/])([A-Za-z0-9]{22})", tok)
    if m:
        al = sp.album(m.group(1))
        return {"id": al["id"], "name": al["name"], "release_date": al.get("release_date", ""),
                "sort_date": al.get("release_date", ""), "total_tracks": al["total_tracks"],
                "artist": al["artists"][0]["name"], "editions": []}

    artist, _, album = tok.partition(" - ")
    if not album:
        artist, album = "", tok
    q = "album:%s" % album.strip() + (" artist:%s" % artist.strip() if artist else "")
    res = sp.search(q=q, type="album", limit=20)
    items = res["albums"]["items"]
    if not items:
        return None
    target = base_name(album)
    same = [i for i in items if base_name(i["name"]) == target] or items
    eds = [{"id": i["id"], "name": i["name"], "release_date": i.get("release_date", ""),
            "total_tracks": i.get("total_tracks", 0),
            "artist": (i.get("artists") or [{}])[0].get("name", "")} for i in same]
    eds = match_artist(artist.strip(), eds)
    best = pick_edition(eds)
    return {"id": best["id"], "name": best["name"], "release_date": best["release_date"],
            "sort_date": min(e["release_date"] or "9999" for e in eds),
            "total_tracks": best["total_tracks"],
            "artist": best.get("artist", ""),
            "editions": eds}


def _pool_albums(sp, playlist_ref: str) -> list:
    """Read an existing pool back as the album list it was built from.

    Lets `--extend` drop new records into their real chronological slot instead of
    appending them to the end, which is what a plain add would do.
    """
    pid = re.sub(r".*[:/]", "", playlist_ref.split("?")[0])
    seen, out, offset = set(), [], 0
    fields = ("items(track(album(id,name,release_date,total_tracks,artists(name)))),next")
    while True:
        res = sp.playlist_items(pid, limit=100, offset=offset, fields=fields)
        for it in res["items"]:
            al = ((it.get("track") or {}).get("album")) or {}
            if not al.get("id") or al["id"] in seen:
                continue
            seen.add(al["id"])
            out.append({"id": al["id"], "name": al["name"],
                        "release_date": al.get("release_date", ""),
                        "sort_date": al.get("release_date", ""),
                        "total_tracks": al.get("total_tracks", 0),
                        "artist": (al.get("artists") or [{}])[0].get("name", ""),
                        "editions": []})
        if not res.get("next"):
            break
        offset += 100
    return out


def cmd_plan(args):
    """Resolve an album list, prune reissues, order it chronologically, emit URIs."""
    sp = _client()
    raw = open(args.from_file, encoding="utf-8").read() if args.from_file else sys.stdin.read()
    lines = _parse_album_lines(raw)

    albums, misses = [], []
    for ln in lines:
        found = _resolve_album(sp, ln)
        if found:
            albums.append(found)
        else:
            misses.append(ln)

    if args.extend:
        existing = _pool_albums(sp, args.extend)
        have = {a["id"] for a in albums}
        albums = [a for a in existing if a["id"] not in have] + albums

    seen, deduped = set(), []
    for a in albums:
        if a["id"] not in seen:
            seen.add(a["id"])
            deduped.append(a)
    albums = order_albums(deduped)

    uris, manifest = [], []
    for a in albums:
        tracks = _album_tracks(sp, a["id"])
        canonical = canonical_length(a["editions"]) if a.get("editions") else None
        kept, note = prune_bonus(tracks, canonical=canonical, deluxe=adds_tracks(a["name"]))
        uris.extend(t["uri"] for t in kept)
        manifest.append({"year": (a.get("sort_date") or "")[:4], "artist": a["artist"],
                         "album": a["name"], "tracks": len(kept), "note": note, "id": a["id"]})

    uris = list(dict.fromkeys(uris))
    if args.uris:
        with open(args.uris, "w", encoding="utf-8") as fh:
            fh.write("\n".join(uris) + "\n")

    if args.json:
        print(json.dumps({"albums": manifest, "misses": misses, "track_count": len(uris),
                          "uris_file": args.uris}, indent=2, ensure_ascii=False))
        return 0
    for m in manifest:
        flag = "" if m["note"] == "clean" else "   <- %s" % m["note"]
        print("  %s  %-20s %-40s %2dt%s"
              % (m["year"], m["artist"][:20], m["album"][:40], m["tracks"], flag))
    for miss in misses:
        print("  [MISS] %s" % miss)
    tail = (" -> %s" % args.uris) if args.uris else "  (no --uris given, nothing written)"
    print("\n%d albums, %d tracks%s" % (len(manifest), len(uris), tail))
    return 0


def build_parser():
    p = argparse.ArgumentParser(prog="albums_helper",
                                description="Build whole-album source pools.")
    sub = p.add_subparsers(dest="command", required=True)

    r = sub.add_parser("releases", help="list an artist's releases, classified, with keep/skip")
    r.add_argument("--artist", action="append", required=True, help="artist name (repeatable)")
    r.add_argument("--deep", action="store_true",
                   help="read every tracklist too: catches untitled live records and "
                        "compilations that recycle earlier albums (one call per release)")
    r.add_argument("--json", action="store_true")
    r.set_defaults(func=cmd_releases)

    pl = sub.add_parser("plan", help="resolve albums, prune reissues, order them, write URIs")
    pl.add_argument("--from", dest="from_file",
                    help="file of 'Artist - Album' lines or album ids/URLs (else stdin)")
    pl.add_argument("--uris", help="write the ordered track URIs here (for mix_helper create/replace)")
    pl.add_argument("--extend", metavar="PLAYLIST",
                    help="merge an existing pool's albums in, so new ones land in date order")
    pl.add_argument("--json", action="store_true")
    pl.set_defaults(func=cmd_plan)
    return p


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
