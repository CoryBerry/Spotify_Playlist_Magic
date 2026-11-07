"""Music recall — practice turning artists you recognize into names you can bring up.

The target moment: a band comes on at a brewery, you talk about it, and two minutes after
leaving you remember the two artists you meant to recommend. This module is the practice loop
that makes those names easier to reach: a small rotating "ready to recommend" set built from
real listening, curated associations ("when Remember Sports comes up → Lime Garden"), and
short open-recall sessions whose ratings drive a simple review schedule.

Like ``spotify_service`` it imports no Flask and reads no session — plain ``sqlite3`` against
the app's DB — so ``cli.py recall …`` and the ``/recall`` web pages share one implementation.
It owns its own ``recall_*`` tables (``ensure_schema``) rather than SQLAlchemy models, the same
way ``mix_helper`` reads and writes the app DB directly.

Three layers, kept apart on purpose:

* **Load** (side-effecting): ``load_listening`` reads Last.fm (key-only, TTL-cached);
  ``load_playlists`` reads Cory's own pools from the local ``.mix_cache`` — no Spotify calls.
* **Refresh** (idempotent): ``refresh`` turns that data into explainable candidates, rotates the
  active set, and *proposes* neighbor links. It never overwrites a user decision: hooks,
  pin/dismiss/snooze/nope, and accepted/rejected links survive every refresh.
* **Practice** (step-based): ``start_session`` → ``next_prompt`` → ``hint`` / ``submit`` /
  ``reveal`` → ``rate`` → ``end_session``. Each step persists, so a terminal loop, a web page,
  or an interrupted session all see the same state. The public prompt view never contains the
  answer; answers come back only from ``submit`` / ``reveal``.

Nothing here claims similarity it can't source. Links carry an origin — ``user`` (Cory said
so), ``starter`` (from the brewery story, needs his acceptance), ``data`` (shared placement in
his own playlists), ``inferred`` (Last.fm's similar-artists list) — and only accepted links
become practice material. Times are naive local ``datetime.now()``, matching the app.
"""
from __future__ import annotations

import difflib
import glob
import json
import math
import os
import re
import sqlite3
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable, Optional

_HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB = os.path.join(_HERE, "instance", "spotify_tools.db")
DEFAULT_MIX_CACHE = os.path.join(_HERE, ".mix_cache")

RATINGS = ("easy", "moment", "couldnt")
RATING_LABELS = {"easy": "came easily", "moment": "took a moment", "couldnt": "couldn't recall"}
CHOICES = ("pinned", "dismissed", "nope")

# (default, min, max) — everything user-tunable via `recall config` / set_setting.
SETTINGS = {
    "set_size":         (30, 15, 50),   # ready-to-recommend set size
    "rotate_days":      (28, 7, 365),   # a non-pinned member may rotate out after this long
    "recent_weeks":     (4, 2, 8),      # "recent interest" window, in chart weeks
    "recent_min_weeks": (3, 1, 8),      # …of which this many must have plays (capped at recent_weeks)
    "recent_min_plays": (5, 1, 500),    # …with at least this many plays across them
    "recent_lift_pct":  (150, 100, 1000),  # …at this % of the artist's earlier weekly rate (100 = off)
    "window_weeks":     (13, 4, 52),    # "developing familiarity" window
    "min_weeks":        (3, 2, 13),     # weeks-with-plays needed to count as familiar
    "anchor_min_plays": (100, 20, 10000),  # all-time plays for an "established anchor"
    "famous_listeners": (1500000, 10000, 100000000),  # Last.fm listeners = household name
    "session_prompts":  (3, 1, 5),
    "new_per_session":  (1, 0, 3),
    "relearn_days":     (1, 1, 7),      # couldn't recall → back next day
    "snooze_days":      (30, 1, 365),
}
DEFAULT_LADDER = "3,7,14,30"   # days between successful unassisted recalls

# Own-playlist tags (the mix skill's tiers, on playlist_tag) that make weak *link* evidence:
# a year-end "My Spotify Top 100" or a broad "Albums - 2020s" pool puts artists side by side
# because they shared a year, not a sound. They still count toward familiarity.
WEAK_LINK_TAGS = ("annual", "decade", "rotation")
WEAK_LINK_WEIGHT = 0.25
# Bump to discard every cached Last.fm listener count on the next refresh (rev 2: counts
# fetched with autocorrect=1 could belong to a different act — Ratboys → "Ratboy").
LISTENERS_REV = "2"

SCHEMA = """
CREATE TABLE IF NOT EXISTS recall_artist (
    id            INTEGER PRIMARY KEY,
    name          TEXT NOT NULL,
    norm          TEXT NOT NULL UNIQUE,   -- identity key: case/accents/punctuation folded
    loose         TEXT NOT NULL,          -- norm minus a leading "the"; NOT unique (ambiguity)
    origin        TEXT NOT NULL,          -- listening | playlist | user | starter
    choice        TEXT,                   -- NULL | pinned | dismissed | nope  (user-owned)
    snooze_until  TEXT,                   -- user-owned
    hook          TEXT,                   -- user-owned, one line
    hook_suggested TEXT,                  -- refresh-owned, from listening data only
    bucket        TEXT,                   -- refresh-owned: recent | developing | anchor | playlist | NULL
    score         REAL,
    listeners     INTEGER,                -- refresh-owned: Last.fm global listeners, when fetched
    evidence      TEXT,                   -- refresh-owned JSON: counts + reasons
    created_at    TEXT NOT NULL,
    refreshed_at  TEXT
);
CREATE TABLE IF NOT EXISTS recall_alias (
    alias_norm TEXT PRIMARY KEY,
    artist_id  INTEGER NOT NULL REFERENCES recall_artist(id),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS recall_link (
    id         INTEGER PRIMARY KEY,
    src_id     INTEGER NOT NULL REFERENCES recall_artist(id),
    dst_id     INTEGER NOT NULL REFERENCES recall_artist(id),
    origin     TEXT NOT NULL,            -- user | starter | data | inferred
    state      TEXT NOT NULL,            -- proposed | accepted | rejected
    reason     TEXT,
    evidence   TEXT,                     -- JSON
    confidence REAL,                     -- evidence strength for data/inferred; NULL for user
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (src_id, dst_id)
);
CREATE TABLE IF NOT EXISTS recall_context (
    artist_id  INTEGER NOT NULL REFERENCES recall_artist(id),
    context    TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (artist_id, context)
);
CREATE TABLE IF NOT EXISTS recall_active (
    id          INTEGER PRIMARY KEY,
    artist_id   INTEGER NOT NULL REFERENCES recall_artist(id),
    bucket      TEXT,
    reason      TEXT,
    selected_at TEXT NOT NULL,
    retired_at  TEXT
);
CREATE TABLE IF NOT EXISTS recall_item (
    id            INTEGER PRIMARY KEY,
    kind          TEXT NOT NULL,          -- recommend | lately | context | hook
    key           TEXT NOT NULL UNIQUE,
    cue_id        INTEGER,
    target_id     INTEGER,
    context       TEXT,
    live          INTEGER NOT NULL DEFAULT 1,
    step          INTEGER NOT NULL DEFAULT 0,
    interval_days REAL,
    due_at        TEXT,                   -- NULL = never practiced (new)
    reps          INTEGER NOT NULL DEFAULT 0,
    lapses        INTEGER NOT NULL DEFAULT 0,
    last_rating   TEXT,
    last_seen_at  TEXT,
    created_at    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS recall_session (
    id         INTEGER PRIMARY KEY,
    started_at TEXT NOT NULL,
    ended_at   TEXT,
    queue      TEXT NOT NULL,             -- JSON list of item ids
    fresh      INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS recall_attempt (
    id          INTEGER PRIMARY KEY,
    session_id  INTEGER NOT NULL REFERENCES recall_session(id),
    item_id     INTEGER NOT NULL REFERENCES recall_item(id),
    kind        TEXT NOT NULL,
    prompt      TEXT NOT NULL,            -- snapshot, so old attempts stay readable
    expected    TEXT NOT NULL,            -- JSON snapshot of the answer key at prompt time
    shown_at    TEXT NOT NULL,
    answer      TEXT,
    resolved    TEXT,                     -- JSON: how each answer piece resolved
    answered_at TEXT,
    hint_level  INTEGER NOT NULL DEFAULT 0,
    revealed    INTEGER NOT NULL DEFAULT 0,   -- revealed BEFORE answering
    rating      TEXT,
    rated_at    TEXT,
    elapsed_s   REAL
);
CREATE TABLE IF NOT EXISTS recall_note (
    id         INTEGER PRIMARY KEY,
    text       TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS recall_settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class RecallError(ValueError):
    """A user-facing problem: unknown artist, ambiguous name, bad rating, etc."""


# ---------------------------------------------------------------------------
# Connection, clock, settings
# ---------------------------------------------------------------------------

def connect(path: str = DEFAULT_DB, seed: bool = True) -> sqlite3.Connection:
    """Open the DB with the recall tables in place. ``seed`` offers the starter links (once,
    as proposals) so they're there to accept before any refresh has run."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    if seed:
        seed_starter(conn)
    return conn


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def _ts(dt: datetime) -> str:
    return dt.isoformat(sep=" ", timespec="seconds")


def _dt(s: Optional[str]) -> Optional[datetime]:
    return datetime.fromisoformat(s) if s else None


def _now(now: Optional[datetime]) -> datetime:
    return now or datetime.now()


def get_setting(conn, key: str):
    if key == "ladder":
        row = conn.execute("SELECT value FROM recall_settings WHERE key='ladder'").fetchone()
        return _parse_ladder(row["value"] if row else DEFAULT_LADDER)
    default, lo, hi = SETTINGS[key]
    row = conn.execute("SELECT value FROM recall_settings WHERE key=?", (key,)).fetchone()
    try:
        return max(lo, min(hi, int(row["value"]))) if row else default
    except (TypeError, ValueError):
        return default


def set_setting(conn, key: str, value: str) -> object:
    if key == "ladder":
        _parse_ladder(value, strict=True)
    elif key in SETTINGS:
        _, lo, hi = SETTINGS[key]
        try:
            v = int(value)
        except ValueError:
            raise RecallError(f"{key} must be a whole number")
        if not lo <= v <= hi:
            raise RecallError(f"{key} must be between {lo} and {hi}")
    else:
        raise RecallError(f"unknown setting {key!r} (try: ladder, {', '.join(SETTINGS)})")
    conn.execute("INSERT INTO recall_settings (key, value) VALUES (?, ?) "
                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))
    conn.commit()
    return get_setting(conn, key)


def all_settings(conn) -> dict:
    out = {k: get_setting(conn, k) for k in SETTINGS}
    out["ladder"] = get_setting(conn, "ladder")
    return out


def _parse_ladder(value: str, strict: bool = False) -> list[int]:
    try:
        steps = [int(x) for x in str(value).split(",") if x.strip()]
        if steps and all(0 < s <= 365 for s in steps):
            return steps
    except ValueError:
        pass
    if strict:
        raise RecallError("ladder must be comma-separated day counts, e.g. 3,7,14,30")
    return _parse_ladder(DEFAULT_LADDER)


# ---------------------------------------------------------------------------
# Identity: normalization, lookup, answer matching
# ---------------------------------------------------------------------------

def norm(name: str) -> str:
    """Identity key: accents, case, punctuation and '&' folded. 'Sigur Rós' == 'sigur ros'."""
    s = unicodedata.normalize("NFKD", name or "")
    s = "".join(c for c in s if not unicodedata.combining(c)).casefold()
    s = s.replace("&", " and ")
    s = re.sub(r"[-_/]", " ", s)          # 'Hop-Along' == 'Hop Along'
    s = re.sub(r"[^\w\s]", "", s)
    return re.sub(r"\s+", " ", s).strip()


def loose(name: str) -> str:
    """norm without a leading 'the' — a *match* key only, never an identity key, because
    'The Band' and 'Band' can be two different acts. Collisions surface as ambiguity."""
    n = norm(name)
    return n[4:] if n.startswith("the ") else n


def find_artist(conn, name: str) -> dict:
    """Strict lookup for curation commands: exact, alias, or an unambiguous 'the'-variant.

    Returns {"id", "name", "how"} or raises RecallError. Never fuzzy-merges — a typo here
    would silently attach a hook or link to the wrong act.
    """
    n = norm(name)
    row = conn.execute("SELECT id, name FROM recall_artist WHERE norm=?", (n,)).fetchone()
    if row:
        return {"id": row["id"], "name": row["name"], "how": "exact"}
    row = conn.execute("SELECT a.id, a.name FROM recall_alias x JOIN recall_artist a "
                       "ON a.id=x.artist_id WHERE x.alias_norm=?", (n,)).fetchone()
    if row:
        return {"id": row["id"], "name": row["name"], "how": "alias"}
    rows = conn.execute("SELECT id, name FROM recall_artist WHERE loose=?", (loose(name),)).fetchall()
    if len(rows) == 1:
        return {"id": rows[0]["id"], "name": rows[0]["name"], "how": "loose"}
    if len(rows) > 1:
        raise RecallError(f"{name!r} is ambiguous: " + " / ".join(r["name"] for r in rows)
                          + " — use the exact name")
    near = _near_names(conn, name)
    hint = f" (did you mean {' / '.join(near)}?)" if near else ""
    raise RecallError(f"no artist named {name!r} yet{hint}")


def ensure_artist(conn, name: str, origin: str, now: Optional[datetime] = None) -> dict:
    """find_artist, or create a bare row with no evidence (nothing fabricated).

    Returns {"id", "name", "created", "near"} — ``near`` lists close existing names so the
    caller can warn "created 'Lime Gardn' — did you mean Lime Garden?" instead of merging.
    """
    name = (name or "").strip()
    if not norm(name):
        raise RecallError("artist name is empty")
    try:
        hit = find_artist(conn, name)
        return {"id": hit["id"], "name": hit["name"], "created": False, "near": []}
    except RecallError as exc:
        if "ambiguous" in str(exc):
            raise
    near = _near_names(conn, name)
    cur = conn.execute(
        "INSERT INTO recall_artist (name, norm, loose, origin, created_at) VALUES (?,?,?,?,?)",
        (name, norm(name), loose(name), origin, _ts(_now(now))))
    return {"id": cur.lastrowid, "name": name, "created": True, "near": near}


def _near_names(conn, name: str, cutoff: float = 0.84) -> list[str]:
    rows = conn.execute("SELECT name, norm FROM recall_artist").fetchall()
    by_norm = {r["norm"]: r["name"] for r in rows}
    return [by_norm[n] for n in difflib.get_close_matches(norm(name), list(by_norm), n=3, cutoff=cutoff)]


def split_answer(text: str) -> list[str]:
    """Split free text into artist-name pieces on commas, semicolons, slashes and newlines.

    ' and ' / ' & ' are NOT split here — 'Belle and Sebastian' is one act. ``resolve_answer``
    tries that second split only when the whole piece doesn't resolve.
    """
    parts = re.split(r"[,;\n]+|\s+/\s+|\s+\+\s+", text or "")
    return [p.strip(" .!?\"'“”") for p in parts if p.strip(" .!?\"'“”")]


def match_name(conn, text: str, prefer: tuple[int, ...] = ()) -> dict:
    """Resolve one answer piece to a known artist, visibly.

    how: exact | alias | loose | spelling | ambiguous | unknown. A spelling match is only
    taken when exactly one name is clearly closest; near-ties come back as ``ambiguous`` with
    the options listed, never merged. ``prefer`` (the prompt's expected ids) wins ties, so a
    typo of an expected answer isn't lost to a similarly-spelled stranger.
    """
    out = {"text": text, "how": "unknown", "artist_id": None, "name": None, "options": []}
    try:
        hit = find_artist(conn, text)
        out.update(how=hit["how"], artist_id=hit["id"], name=hit["name"])
        return out
    except RecallError as exc:
        if "ambiguous" in str(exc):
            rows = conn.execute("SELECT id, name FROM recall_artist WHERE loose=?",
                                (loose(text),)).fetchall()
            preferred = [r for r in rows if r["id"] in prefer]
            if len(preferred) == 1:
                out.update(how="loose", artist_id=preferred[0]["id"], name=preferred[0]["name"])
            else:
                out.update(how="ambiguous", options=[r["name"] for r in rows])
            return out

    rows = conn.execute("SELECT id, name, norm FROM recall_artist").fetchall()
    target = norm(text)
    if len(target) < 3:
        return out
    scored = sorted(((difflib.SequenceMatcher(None, target, r["norm"]).ratio(), r) for r in rows),
                    key=lambda x: -x[0])
    close = [(s, r) for s, r in scored if s >= 0.84]
    if not close:
        return out
    best = close[0][0]
    tied = [r for s, r in close if best - s < 0.04]
    preferred = [r for r in tied if r["id"] in prefer]
    if len(tied) == 1 or len(preferred) == 1:
        r = tied[0] if len(tied) == 1 else preferred[0]
        out.update(how="spelling", artist_id=r["id"], name=r["name"])
    else:
        out.update(how="ambiguous", options=[r["name"] for r in tied])
    return out


def resolve_answer(conn, text: str, prefer: tuple[int, ...] = ()) -> list[dict]:
    """Every artist-ish piece of a free-text answer, each resolved via ``match_name``."""
    out = []
    for piece in split_answer(text):
        m = match_name(conn, piece, prefer)
        if m["how"] == "unknown" and re.search(r"\s(?:and|&)\s", piece, re.I):
            halves = [h.strip() for h in re.split(r"\s(?:and|&)\s", piece, flags=re.I) if h.strip()]
            sub = [match_name(conn, h, prefer) for h in halves]
            if all(s["how"] != "unknown" for s in sub):
                out.extend(sub)
                continue
        out.append(m)
    return out


def add_alias(conn, alias: str, artist_name: str, now: Optional[datetime] = None) -> dict:
    """Remember a spelling: future answers of ``alias`` count as ``artist_name``."""
    art = find_artist(conn, artist_name)
    a = norm(alias)
    if not a:
        raise RecallError("alias is empty")
    clash = conn.execute("SELECT name FROM recall_artist WHERE norm=?", (a,)).fetchone()
    if clash and clash["name"] != art["name"]:
        raise RecallError(f"{alias!r} is already the artist {clash['name']!r}")
    conn.execute("INSERT INTO recall_alias (alias_norm, artist_id, created_at) VALUES (?,?,?) "
                 "ON CONFLICT(alias_norm) DO UPDATE SET artist_id=excluded.artist_id",
                 (a, art["id"], _ts(_now(now))))
    conn.commit()
    return {"alias": alias, "artist": art["name"]}


def _leaks(text: str, names: list[str]) -> bool:
    """Would showing ``text`` give away one of ``names``? Word-start match only, so a
    possessive ("Hop Along's") counts; erring toward 'leaks' just hides a hint."""
    t = f" {norm(text)}"
    return any(len(norm(n)) >= 3 and f" {norm(n)}" in t for n in names)


# ---------------------------------------------------------------------------
# Load: source data (side-effecting; everything downstream takes these as plain values)
# ---------------------------------------------------------------------------

@dataclass
class Listening:
    """Last.fm listening data. Every count downstream is computed from these lists."""
    weeks: list[dict] = field(default_factory=list)       # [{from, to, artists: {name: plays}}], oldest first
    top_12m: list[dict] = field(default_factory=list)     # [{artist, playcount}]
    top_overall: list[dict] = field(default_factory=list)
    loved: list[dict] = field(default_factory=list)       # [{artist, title}]
    top_tracks: list[dict] = field(default_factory=list)  # [{artist, title, playcount}]


@dataclass
class Playlists:
    """Cory's own playlists, from the local track cache: {playlist_id: {name, artists: [str], tags}}."""
    pools: dict = field(default_factory=dict)
    note: str = ""


def load_listening(window_weeks: int = 13) -> tuple[Optional[Listening], str]:
    """Read Last.fm. Returns (None, reason) when it's unavailable; never raises."""
    try:
        import lastfm_service as lfm
        lfm.get_api_key()
        lfm.get_user()
        data = Listening(
            weeks=lfm.user_weekly_artist_charts(weeks=window_weeks),
            top_12m=lfm.user_top_artists(period="12month", max_pages=3),
            top_overall=lfm.user_top_artists(period="overall", max_pages=2),
            loved=lfm.user_loved_tracks(max_pages=2),
            top_tracks=lfm.user_top_tracks(period="overall", max_pages=5),
        )
    except Exception as exc:  # missing key, network, rate limit — degrade, don't die
        return None, f"Last.fm unavailable: {exc}"
    if not (data.weeks or data.top_12m or data.top_overall):
        return None, "Last.fm returned no listening data"
    return data, ""


def load_playlists(conn, cache_dir: str = DEFAULT_MIX_CACHE) -> Playlists:
    """Cory's own playlists from the local ``.mix_cache`` — no Spotify calls.

    'Own' = owned by the account that owns most of the cached playlist list, minus every
    playlist Crate itself generated (``created_playlist``): a mix Claude picked isn't evidence
    of what Cory chose to put together.
    """
    try:
        row = conn.execute("SELECT data FROM playlist_cache LIMIT 1").fetchone()
        listing = json.loads(row[0]) if row else []
    except (sqlite3.Error, ValueError, TypeError):
        listing = []
    if not listing:
        return Playlists(note="no playlist list cached (open Manage in the app or run "
                              "`mix_helper.py refresh-cache`)")
    owners: dict[str, int] = {}
    for p in listing:
        oid = (p.get("owner") or {}).get("id")
        if oid:
            owners[oid] = owners.get(oid, 0) + 1
    me = max(owners, key=owners.get) if owners else None
    try:
        generated = {r[0] for r in conn.execute("SELECT playlist_id FROM created_playlist")}
    except sqlite3.Error:
        generated = set()
    by_id = {p["id"]: p for p in listing if p.get("id")}
    tags: dict[str, set] = {}
    try:
        for pid, tag in conn.execute("SELECT playlist_id, tag FROM playlist_tag"):
            tags.setdefault(pid, set()).add(tag)
    except sqlite3.Error:
        pass

    pools = {}
    for path in sorted(glob.glob(os.path.join(cache_dir, "*.json"))):
        pid = os.path.splitext(os.path.basename(path))[0]
        meta = by_id.get(pid)
        if not meta or pid in generated or (meta.get("owner") or {}).get("id") != me:
            continue
        if "nomix" in tags.get(pid, ()):  # past outputs and provider stat-mirrors, not his picks
            continue
        try:
            with open(path, encoding="utf-8") as fh:
                blob = json.load(fh)
        except (OSError, ValueError):
            continue
        if not isinstance(blob, dict):
            continue
        artists = [t.get("artist", "") for t in blob.get("tracks") or [] if t.get("artist")]
        if artists:
            pools[pid] = {"name": meta.get("name") or pid, "artists": artists,
                          "tags": sorted(tags.get(pid, ()))}
    note = "" if pools else "no track lists cached for your own playlists yet (build a mix once)"
    return Playlists(pools=pools, note=note)


def credited_artists(artist_field: str, known: dict[str, str]) -> set[str]:
    """Which known artists a Spotify ``"A, B"`` artist field credits, as norms.

    Spotify joins collaborators with ', ' — which also appears *inside* names ('Tyler, The
    Creator'). So never split blindly: try runs of 1–3 adjacent parts against the known names.
    """
    out = set()
    whole = norm(artist_field)
    if whole in known:
        out.add(whole)
    parts = [p.strip() for p in artist_field.split(", ")]
    for i in range(len(parts)):
        for j in range(i + 1, min(i + 3, len(parts)) + 1):
            n = norm(", ".join(parts[i:j]))
            if n in known:
                out.add(n)
    return out


# ---------------------------------------------------------------------------
# Refresh: candidates → active set → proposed neighbor links (idempotent)
# ---------------------------------------------------------------------------

def compute_candidates(listening: Optional[Listening], playlists: Playlists, cfg: dict) -> dict:
    """{norm: candidate} for every artist the data says Cory knows, with reasons.

    Familiarity needs *spread*: several distinct weeks, a big all-time count, or several of
    his own playlists. One intense week alone never qualifies — a fresh import isn't knowledge.

    "Recent" means *into them lately*, not merely played lately: ~600 artists get a play in a
    typical week, so weeks-with-plays alone saturates. It needs most of the recent weeks, a
    minimum play count across them, and a weekly rate above the artist's own earlier baseline.
    """
    known: dict[str, str] = {}
    weeks = listening.weeks if listening else []
    recent_n = min(cfg["recent_weeks"], len(weeks))
    per: dict[str, dict] = {}

    def slot(name: str) -> dict:
        n = norm(name)
        known.setdefault(n, name)
        return per.setdefault(n, {"weeks_recent": 0, "weeks_window": 0, "plays_recent": 0,
                                  "plays_window": 0, "plays_12m": 0, "plays_all": 0,
                                  "loved": 0, "top_track": None, "playlists": []})

    if listening:
        for idx, wk in enumerate(weeks):
            is_recent = idx >= len(weeks) - recent_n
            for name, plays in wk["artists"].items():
                if plays <= 0:
                    continue
                c = slot(name)
                c["weeks_window"] += 1
                c["plays_window"] += plays
                if is_recent:
                    c["weeks_recent"] += 1
                    c["plays_recent"] += plays
        for r in listening.top_12m:
            slot(r["artist"])["plays_12m"] = r["playcount"]
        for r in listening.top_overall:
            slot(r["artist"])["plays_all"] = r["playcount"]
        for r in listening.loved:
            slot(r["artist"])["loved"] += 1
        for r in listening.top_tracks:  # most-played first, so first seen wins
            n = norm(r["artist"])
            if n in per and per[n]["top_track"] is None:
                per[n]["top_track"] = {"title": r["title"], "plays": r["playcount"]}
    else:
        # Offline: only artist fields that are a single, comma-free credit become names.
        for pool in playlists.pools.values():
            for a in pool["artists"]:
                if ", " not in a:
                    known.setdefault(norm(a), a)

    for pool in playlists.pools.values():
        credited = set()
        for a in pool["artists"]:
            credited |= credited_artists(a, known)
        for n in credited:
            slot(known[n])["playlists"].append(pool["name"])

    out = {}
    W, R = len(weeks), recent_n
    for n, c in per.items():
        n_pl = len(c["playlists"])
        reasons = []
        base_weeks = W - R
        base_rate = (c["plays_window"] - c["plays_recent"]) / base_weeks if base_weeks else 0.0
        recent_rate = c["plays_recent"] / R if R else 0.0
        lifted = recent_rate * 100 >= cfg["recent_lift_pct"] * base_rate
        is_recent = (R > 0 and c["weeks_recent"] >= min(cfg["recent_min_weeks"], R)
                     and c["plays_recent"] >= cfg["recent_min_plays"] and lifted)
        if is_recent:
            lift = (f", {recent_rate / base_rate:.1f}× your usual rate" if base_rate
                    else ", new to you")
            reasons.append(f"into them lately: played in {c['weeks_recent']} of the last {R} weeks"
                           f" ({c['plays_recent']} plays{lift})")
        elif c["weeks_recent"] >= 2:
            reasons.append(f"played in {c['weeks_recent']} of the last {R} weeks ({c['plays_recent']} plays)")
        if c["weeks_window"] >= cfg["min_weeks"]:
            reasons.append(f"played in {c['weeks_window']} of the last {W} weeks")
        if c["plays_all"] >= cfg["anchor_min_plays"]:
            yr = f", {c['plays_12m']} in the last year" if c["plays_12m"] else ""
            reasons.append(f"{c['plays_all']} plays all-time{yr}")
        if n_pl:
            names = ", ".join(f"“{p}”" for p in sorted(c["playlists"])[:3])
            reasons.append(f"in {n_pl} of your playlist{'s' if n_pl > 1 else ''} ({names}"
                           + (", …)" if n_pl > 3 else ")"))
        if c["loved"]:
            reasons.append(f"{c['loved']} loved track{'s' if c['loved'] > 1 else ''}")

        if is_recent:
            bucket = "recent"
        elif c["weeks_window"] >= cfg["min_weeks"]:
            bucket = "developing"
        elif c["plays_all"] >= cfg["anchor_min_plays"]:
            bucket = "anchor"
        elif n_pl >= 3:
            bucket = "playlist"
        else:
            continue  # not enough spread to call it familiar

        # Log-damped plays + week counts: a 200-play binge week can't outrank steady listening.
        score = (3 * c["weeks_recent"] + 1.5 * c["weeks_window"] + math.log2(1 + c["plays_12m"])
                 + 0.5 * math.log2(1 + c["plays_all"]) + 0.75 * min(n_pl, 4) + 0.5 * min(c["loved"], 3))
        tt = c["top_track"]
        out[n] = {"name": known[n], "bucket": bucket, "score": round(score, 3), "reasons": reasons,
                  "hook_suggested": (f"your most-played: “{tt['title']}” ({tt['plays']} plays)"
                                     if tt else None),
                  "counts": {k: c[k] for k in ("weeks_recent", "weeks_window", "plays_recent",
                                               "plays_window", "plays_12m", "plays_all", "loved")},
                  "playlists": sorted(c["playlists"]),
                  "window": {"recent_weeks": R, "window_weeks": W}}
    return out


def _eligible_now(row, now: datetime) -> bool:
    if row["choice"] in ("dismissed", "nope"):
        return False
    until = _dt(row["snooze_until"])
    return not (until and until > now)


def refresh(conn, listening: Optional[Listening], playlists: Playlists,
            now: Optional[datetime] = None,
            similar_fn: Optional[Callable[[str], list[dict]]] = None,
            listening_note: str = "",
            listeners_fn: Optional[Callable[[str], Optional[int]]] = None) -> dict:
    """Rebuild candidates, rotate the active set, propose neighbors. Safe to run any time.

    Writes only refresh-owned fields (bucket/score/evidence/hook_suggested) and *proposed*
    data/inferred links. Hooks, pin/dismiss/snooze/nope, aliases, contexts, accepted and
    rejected links, and all practice history are left untouched.
    """
    now = _now(now)
    cfg = all_settings(conn)
    seeded = seed_starter(conn, now)
    _expire_listeners(conn)
    cands = compute_candidates(listening, playlists, cfg)

    # 1. Upsert candidate rows (refresh-owned columns only).
    ids: dict[str, int] = {}
    for n, c in cands.items():
        row = conn.execute("SELECT id FROM recall_artist WHERE norm=?", (n,)).fetchone()
        ev = json.dumps({"reasons": c["reasons"], "counts": c["counts"],
                         "playlists": c["playlists"], "window": c["window"]}, ensure_ascii=False)
        if row:
            conn.execute("UPDATE recall_artist SET bucket=?, score=?, evidence=?, hook_suggested=?,"
                         " refreshed_at=? WHERE id=?",
                         (c["bucket"], c["score"], ev, c["hook_suggested"], _ts(now), row["id"]))
            ids[n] = row["id"]
        else:
            origin = "listening" if listening else "playlist"
            cur = conn.execute(
                "INSERT INTO recall_artist (name, norm, loose, origin, bucket, score, evidence,"
                " hook_suggested, created_at, refreshed_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (c["name"], n, loose(c["name"]), origin, c["bucket"], c["score"], ev,
                 c["hook_suggested"], _ts(now), _ts(now)))
            ids[n] = cur.lastrowid
    # Rows the data no longer supports lose their bucket (but keep everything user-owned).
    # Skipped when a source was down, so an outage can't wipe the candidate pool.
    if listening is not None and cands:
        keep = tuple(ids.values())
        conn.execute(f"UPDATE recall_artist SET bucket=NULL, score=NULL WHERE bucket IS NOT NULL"
                     f" AND id NOT IN ({','.join('?' * len(keep))})", keep)

    # 2. Rotate the active set.
    active = _rotate_active(conn, cfg, now, listeners_fn)

    # 3. Propose neighbors for current members.
    proposed = _propose_links(conn, playlists, similar_fn, now)
    conn.commit()

    by_bucket: dict[str, int] = {}
    for c in cands.values():
        by_bucket[c["bucket"]] = by_bucket.get(c["bucket"], 0) + 1
    report = {
        "listening": ({"weeks": len(listening.weeks), "top_12m": len(listening.top_12m),
                       "top_overall": len(listening.top_overall), "loved": len(listening.loved),
                       "top_tracks": len(listening.top_tracks)} if listening
                      else {"unavailable": listening_note or "not loaded"}),
        "playlists": {"pools": len(playlists.pools),
                      "tracks": sum(len(p["artists"]) for p in playlists.pools.values()),
                      "note": playlists.note},
        "candidates": len(cands), "by_bucket": by_bucket, "active": active,
        "links_proposed": proposed, "starter_offered": seeded,
    }
    report["explain"] = _explain_empty(conn, report, now)
    return report


def refresh_will_be_slow(conn) -> bool:
    """True before the first refresh: filling an empty set means a Last.fm listener lookup per
    pick (plus every household name it skips on the way) — about 2 minutes. Later refreshes
    only top up a few rotated-out slots."""
    return not conn.execute("SELECT 1 FROM recall_active WHERE retired_at IS NULL LIMIT 1").fetchone()


def _expire_listeners(conn) -> None:
    row = conn.execute("SELECT value FROM recall_settings WHERE key='listeners_rev'").fetchone()
    if row and row["value"] == LISTENERS_REV:
        return
    conn.execute("UPDATE recall_artist SET listeners=NULL")
    conn.execute("INSERT INTO recall_settings (key, value) VALUES ('listeners_rev', ?) "
                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (LISTENERS_REV,))


def _explain_empty(conn, report: dict, now: datetime) -> str:
    if report["active"]["size"]:
        return ""
    if not report["candidates"]:
        bits = []
        if "unavailable" in report["listening"]:
            bits.append(report["listening"]["unavailable"])
        if report["playlists"]["note"]:
            bits.append(report["playlists"]["note"])
        return ("No candidates: " + ("; ".join(bits) or "no artist had enough spread "
                "(several weeks, big all-time count, or 3+ of your playlists)")
                + ". Curated links still work — try `recall link` and `recall practice`.")
    held = conn.execute("SELECT COUNT(*) FROM recall_artist WHERE bucket IS NOT NULL AND "
                        "(choice IN ('dismissed','nope') OR snooze_until > ?)", (_ts(now),)).fetchone()[0]
    return f"All {held} candidates are dismissed, snoozed, or marked 'nope' — unsnooze or pin some."


def _rotate_active(conn, cfg: dict, now: datetime, listeners_fn=None) -> dict:
    """Keep what's still valid, age out a few, fill by bucket quota.

    Auto-picks skip household names (Last.fm listeners ≥ ``famous_listeners``) — nobody
    needs practice to bring up The Beatles; the point is the Lime Garden tier. Pins ignore
    that rule. Artists rotated out within ``rotate_days`` sit out before they can return.
    """
    size, rotate = cfg["set_size"], timedelta(days=cfg["rotate_days"])
    rows = {r["id"]: r for r in conn.execute("SELECT * FROM recall_artist").fetchall()}
    members = conn.execute("SELECT * FROM recall_active WHERE retired_at IS NULL").fetchall()

    def ok(aid):  # still backed by data (or pinned) and not held back by a user choice
        r = rows.get(aid)
        return r is not None and _eligible_now(r, now) and (r["bucket"] or r["choice"] == "pinned")

    famous: list[str] = []

    def too_famous(r) -> bool:
        n = r["listeners"]
        if n is None and listeners_fn:
            try:
                n = listeners_fn(r["name"])
            except Exception:
                n = None
            if n is not None:
                conn.execute("UPDATE recall_artist SET listeners=? WHERE id=?", (n, r["id"]))
        if n is not None and n >= cfg["famous_listeners"]:
            famous.append(r["name"])
            return True
        return False

    retired, kept = [], []
    for m in members:
        r = rows.get(m["artist_id"])
        # A member whose count was never fetched (or was discarded as untrustworthy) gets
        # checked now; one that turns out to be a household name leaves unless pinned.
        unfamous = r is None or r["choice"] == "pinned" or r["listeners"] is not None or not too_famous(r)
        (kept if ok(m["artist_id"]) and unfamous else retired).append(m)
    # Stagger rotation: at most a quarter of the set ages out per refresh, oldest first.
    aged = sorted((m for m in kept if rows[m["artist_id"]]["choice"] != "pinned"
                   and now - _dt(m["selected_at"]) > rotate), key=lambda m: m["selected_at"])
    for m in aged[: max(1, size // 4)]:
        kept.remove(m)
        retired.append(m)
    # Shrink if the size setting dropped: lowest-score non-pinned go first.
    overflow = len(kept) - size
    if overflow > 0:
        for m in sorted((m for m in kept if rows[m["artist_id"]]["choice"] != "pinned"),
                        key=lambda m: rows[m["artist_id"]]["score"] or 0)[:overflow]:
            kept.remove(m)
            retired.append(m)
    for m in retired:
        conn.execute("UPDATE recall_active SET retired_at=? WHERE id=?", (_ts(now), m["id"]))

    have = {m["artist_id"] for m in kept}
    recently_retired = {m["artist_id"] for m in retired} | {r[0] for r in conn.execute(
        "SELECT artist_id FROM recall_active WHERE retired_at > ?", (_ts(now - rotate),))}
    added = []

    def add(aid, why):
        r = rows[aid]
        reasons = json.loads(r["evidence"] or "{}").get("reasons") or []
        conn.execute("INSERT INTO recall_active (artist_id, bucket, reason, selected_at) VALUES (?,?,?,?)",
                     (aid, r["bucket"], why or (reasons[0] if reasons else "pinned"), _ts(now)))
        have.add(aid)
        added.append(r["name"])

    for aid, r in rows.items():  # pins always belong
        if r["choice"] == "pinned" and aid not in have and ok(aid):
            add(aid, "pinned")

    pool = [r for aid, r in rows.items() if aid not in have and aid not in recently_retired and ok(aid)]
    quotas = {"recent": round(size * 0.4), "developing": round(size * 0.3), "anchor": round(size * 0.3)}
    for bucket, quota in quotas.items():
        have_b = sum(1 for aid in have if rows[aid]["bucket"] == bucket)
        for r in sorted((r for r in pool if r["bucket"] == bucket), key=lambda r: -(r["score"] or 0)):
            if have_b >= quota or len(have) >= size:
                break
            if r["id"] not in have and r["name"] not in famous and not too_famous(r):
                add(r["id"], None)
                have_b += 1
    for r in sorted(pool, key=lambda r: -(r["score"] or 0)):  # fill any shortfall by score
        if len(have) >= size:
            break
        if r["id"] not in have and r["name"] not in famous and not too_famous(r):
            add(r["id"], None)
    return {"size": len(have), "kept": len(kept), "added": added, "skipped_famous": famous,
            "retired": [rows[m["artist_id"]]["name"] for m in retired if m["artist_id"] in rows]}


def _link_weight(pool: dict) -> float:
    return WEAK_LINK_WEIGHT if set(pool.get("tags") or ()) & set(WEAK_LINK_TAGS) else 1.0


def _propose_links(conn, playlists: Playlists, similar_fn, now: datetime) -> int:
    """Up to 3 data neighbors (shared own-playlist placement) + 2 inferred (Last.fm similar)
    per active member, restricted to artists Cory already knows. Proposals only.

    Shared placement is weighted: a year-end or decade pool (``WEAK_LINK_TAGS``) counts a
    quarter as much as a playlist he built around a sound, and a pair needs the equivalent of
    two real shared playlists. Open data proposals the evidence no longer supports are
    withdrawn; accepted and rejected links are never touched.
    """
    universe = {r["norm"]: r for r in conn.execute(
        "SELECT id, name, norm FROM recall_artist WHERE bucket IS NOT NULL").fetchall()}
    known = {n: r["name"] for n, r in universe.items()}
    weight = {pid: _link_weight(pool) for pid, pool in playlists.pools.items()}
    sets: dict[str, set] = {}
    for pid, pool in playlists.pools.items():
        credited = set()
        for a in pool["artists"]:
            credited |= credited_artists(a, known)
        for n in credited:
            sets.setdefault(n, set()).add(pid)
    members = conn.execute("SELECT a.id, a.name, a.norm FROM recall_active m JOIN recall_artist a "
                           "ON a.id=m.artist_id WHERE m.retired_at IS NULL").fetchall()
    mass = lambda pids: sum(weight[p] for p in pids)  # noqa: E731
    n_new = 0
    for m in members:
        mine = sets.get(m["norm"], set())
        scored = []
        for n, s in sets.items():
            shared = mine & s
            if n == m["norm"] or mass(shared) < 2:
                continue
            cos = mass(shared) / math.sqrt(mass(mine) * mass(s))
            scored.append((cos, n, shared))
        supported = set()
        for cos, n, shared in sorted(scored, key=lambda x: -x[0])[:3]:
            strong = sorted(playlists.pools[p]["name"] for p in shared if weight[p] == 1.0)
            weak = sorted(playlists.pools[p]["name"] for p in shared if weight[p] != 1.0)
            reason = (f"both in {len(strong)} of your playlists: "
                      + ", ".join(f"“{x}”" for x in strong[:3]) + (", …" if len(strong) > 3 else ""))
            if weak:
                reason += (f"; also {len(weak)} year/decade list{'s' if len(weak) > 1 else ''}"
                           " (weak evidence)")
            supported.add(universe[n]["id"])
            n_new += _upsert_proposal(conn, m["id"], universe[n]["id"], "data", reason,
                                      {"playlists": strong, "weak_playlists": weak}, round(cos, 3), now)
        stale = conn.execute("SELECT id, dst_id FROM recall_link WHERE src_id=? AND origin='data'"
                             " AND state='proposed'", (m["id"],)).fetchall()
        for row in stale:
            if row["dst_id"] not in supported:
                conn.execute("DELETE FROM recall_link WHERE id=?", (row["id"],))
        if similar_fn:
            try:
                sims = similar_fn(m["name"]) or []
            except Exception:
                sims = []
            picks = [s for s in sims if norm(s["name"]) in universe and norm(s["name"]) != m["norm"]
                     and s.get("match", 0) >= 0.2][:2]
            for s in picks:
                n_new += _upsert_proposal(
                    conn, m["id"], universe[norm(s["name"])]["id"], "inferred",
                    f"Last.fm lists them as similar (match {s['match']:.2f}) — a suggestion, not your pick",
                    {"lastfm_match": s["match"]}, round(float(s["match"]), 3), now)
    return n_new


def _upsert_proposal(conn, src, dst, origin, reason, evidence, confidence, now) -> int:
    row = conn.execute("SELECT id, origin, state FROM recall_link WHERE src_id=? AND dst_id=?",
                       (src, dst)).fetchone()
    if row is None:
        conn.execute("INSERT INTO recall_link (src_id, dst_id, origin, state, reason, evidence,"
                     " confidence, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                     (src, dst, origin, "proposed", reason, json.dumps(evidence, ensure_ascii=False),
                      confidence, _ts(now), _ts(now)))
        return 1
    # Only refresh our own still-open proposals; data outranks inferred for the same pair.
    if row["state"] == "proposed" and row["origin"] in ("data", "inferred") and \
            not (row["origin"] == "data" and origin == "inferred"):
        conn.execute("UPDATE recall_link SET origin=?, reason=?, evidence=?, confidence=?, updated_at=?"
                     " WHERE id=?", (origin, reason, json.dumps(evidence, ensure_ascii=False),
                                     confidence, _ts(now), row["id"]))
    return 0


def lookup_listeners(name: str, info_fn) -> Optional[int]:
    """Global Last.fm listeners for exactly this artist, or None when unsure.

    The name already came from Last.fm or Spotify, so ask for it verbatim first:
    ``autocorrect=1`` can redirect to a different act (Ratboys → "Ratboy", 2k listeners vs
    200k). Only when the exact name is unknown is the autocorrected answer tried, and it is
    kept only if it is still the same name.
    """
    info = info_fn(name, autocorrect=False)
    if info and info.get("listeners"):
        return info["listeners"]
    info = info_fn(name, autocorrect=True)
    if info and info.get("listeners") and loose(info.get("name") or "") == loose(name):
        return info["listeners"]
    return None


def run_refresh(conn, cache_dir: str = DEFAULT_MIX_CACHE, use_similar: bool = True,
                now: Optional[datetime] = None) -> dict:
    """The side-effecting wrapper both surfaces call: load sources, then ``refresh``."""
    listening, note = load_listening(get_setting(conn, "window_weeks"))
    playlists = load_playlists(conn, cache_dir)
    similar_fn = listeners_fn = None
    if listening is not None:
        import lastfm_service as lfm

        def listeners_fn(name):
            return lookup_listeners(name, lfm.artist_info)
        if use_similar:
            similar_fn = lambda name: lfm.similar_artists(name, limit=30)  # noqa: E731
    return refresh(conn, listening, playlists, now, similar_fn, note, listeners_fn)


# ---------------------------------------------------------------------------
# Curation
# ---------------------------------------------------------------------------

def seed_starter(conn, now: Optional[datetime] = None) -> int:
    """Offer the brewery-story links once, as *proposed* — practice only after acceptance.

    Runs at most once per DB (a settings flag), so rejecting or deleting them sticks.
    No evidence is attached: these are Cory's intended recommendations, not measured facts.
    """
    if conn.execute("SELECT 1 FROM recall_settings WHERE key='starter_seeded'").fetchone():
        return 0
    now = _now(now)
    src = ensure_artist(conn, "Remember Sports", "starter", now)["id"]
    n = 0
    for dst_name in ("Lime Garden", "Hop Along"):
        dst = ensure_artist(conn, dst_name, "starter", now)["id"]
        cur = conn.execute(
            "INSERT OR IGNORE INTO recall_link (src_id, dst_id, origin, state, reason, created_at,"
            " updated_at) VALUES (?,?,?,?,?,?,?)",
            (src, dst, "starter", "proposed",
             "from the brewery: the one you wanted to bring up after Remember Sports came on",
             _ts(now), _ts(now)))
        n += cur.rowcount
    conn.execute("INSERT INTO recall_settings (key, value) VALUES ('starter_seeded', ?)", (_ts(now),))
    conn.commit()
    return n


def link(conn, src_name: str, dst_name: str, reason: str = "", now: Optional[datetime] = None) -> dict:
    """User link: 'when SRC comes up, I want to remember DST'. Beats any generated link."""
    now = _now(now)
    src = ensure_artist(conn, src_name, "user", now)
    dst = ensure_artist(conn, dst_name, "user", now)
    if src["id"] == dst["id"]:
        raise RecallError("an artist can't be linked to itself")
    conn.execute(
        "INSERT INTO recall_link (src_id, dst_id, origin, state, reason, created_at, updated_at)"
        " VALUES (?,?,'user','accepted',?,?,?) ON CONFLICT(src_id, dst_id) DO UPDATE SET"
        " origin='user', state='accepted', reason=COALESCE(NULLIF(excluded.reason,''), reason),"
        " confidence=NULL, updated_at=excluded.updated_at",
        (src["id"], dst["id"], reason.strip(), _ts(now), _ts(now)))
    conn.commit()
    warn = [f"created new artist {a['name']!r} — did you mean {' / '.join(a['near'])}?"
            for a in (src, dst) if a["created"] and a["near"]]
    return {"src": src["name"], "dst": dst["name"], "warnings": warn}


def _find_link(conn, src_name: str, dst_name: str):
    src, dst = find_artist(conn, src_name), find_artist(conn, dst_name)
    row = conn.execute("SELECT * FROM recall_link WHERE src_id=? AND dst_id=?",
                       (src["id"], dst["id"])).fetchone()
    if not row:
        raise RecallError(f"no link {src['name']} → {dst['name']}")
    return row, src, dst


def set_link_state(conn, src_name: str, dst_name: str, state: str,
                   now: Optional[datetime] = None) -> dict:
    """Accept or reject a suggested link. Accepting keeps its origin (so provenance stays
    honest: 'data, accepted by you'); rejecting keeps the row so refresh never re-proposes it."""
    if state not in ("accepted", "rejected"):
        raise RecallError("state must be accepted or rejected")
    row, src, dst = _find_link(conn, src_name, dst_name)
    conn.execute("UPDATE recall_link SET state=?, updated_at=? WHERE id=?", (state, _ts(_now(now)), row["id"]))
    conn.commit()
    return {"src": src["name"], "dst": dst["name"], "state": state, "origin": row["origin"]}


def set_link_state_by_id(conn, link_id: int, state: str, now: Optional[datetime] = None) -> dict:
    row = conn.execute("SELECT s.name AS s, d.name AS d FROM recall_link l JOIN recall_artist s ON"
                       " s.id=l.src_id JOIN recall_artist d ON d.id=l.dst_id WHERE l.id=?",
                       (link_id,)).fetchone()
    if not row:
        raise RecallError("no such link")
    return set_link_state(conn, row["s"], row["d"], state, now)


def unlink(conn, src_name: str, dst_name: str) -> dict:
    row, src, dst = _find_link(conn, src_name, dst_name)
    conn.execute("DELETE FROM recall_link WHERE id=?", (row["id"],))
    conn.commit()
    return {"src": src["name"], "dst": dst["name"], "deleted": True}


def set_hook(conn, name: str, hook: Optional[str], now: Optional[datetime] = None) -> dict:
    """Set (or clear, with None/'') the user's one-line hook. Creates the artist if new."""
    art = ensure_artist(conn, name, "user", now) if hook else find_artist(conn, name)
    hook = (hook or "").strip() or None
    if hook and len(hook) > 200:
        raise RecallError("keep the hook to one short line (200 characters max)")
    conn.execute("UPDATE recall_artist SET hook=? WHERE id=?", (hook, art["id"]))
    conn.commit()
    return {"artist": art["name"], "hook": hook, "leaks_name": bool(hook and _leaks(hook, [art["name"]]))}


def set_choice(conn, name: str, choice: Optional[str], now: Optional[datetime] = None,
               snooze_days: Optional[int] = None) -> dict:
    """pinned | dismissed | nope | None (clear), or snooze for N days. User-owned: refresh
    never touches these."""
    now = _now(now)
    if choice == "snooze":
        days = snooze_days or get_setting(conn, "snooze_days")
        if not 1 <= int(days) <= 365:
            raise RecallError("snooze between 1 and 365 days")
        art = find_artist(conn, name)
        until = now + timedelta(days=int(days))
        conn.execute("UPDATE recall_artist SET snooze_until=? WHERE id=?", (_ts(until), art["id"]))
        conn.commit()
        return {"artist": art["name"], "snooze_until": _ts(until)}
    if choice not in CHOICES + (None, "clear"):
        raise RecallError(f"choice must be one of {', '.join(CHOICES)}, snooze, or clear")
    art = ensure_artist(conn, name, "user", now) if choice == "pinned" else find_artist(conn, name)
    if choice == "clear":
        choice = None
        conn.execute("UPDATE recall_artist SET snooze_until=NULL WHERE id=?", (art["id"],))
    conn.execute("UPDATE recall_artist SET choice=? WHERE id=?", (choice, art["id"]))
    if choice == "pinned":  # a pin joins the active set right away, no refresh needed
        if not conn.execute("SELECT 1 FROM recall_active WHERE artist_id=? AND retired_at IS NULL",
                            (art["id"],)).fetchone():
            conn.execute("INSERT INTO recall_active (artist_id, bucket, reason, selected_at)"
                         " VALUES (?,?,?,?)", (art["id"], None, "pinned", _ts(now)))
    elif choice in ("dismissed", "nope"):
        conn.execute("UPDATE recall_active SET retired_at=? WHERE artist_id=? AND retired_at IS NULL",
                     (_ts(now), art["id"]))
    conn.commit()
    return {"artist": art["name"], "choice": choice}


def tag_context(conn, name: str, context: str, remove: bool = False,
                now: Optional[datetime] = None) -> dict:
    """File an artist under a conversational context ('new discoveries', 'interesting voices')."""
    context = re.sub(r"\s+", " ", (context or "").strip()).lower()
    if not context:
        raise RecallError("context is empty")
    if remove:
        art = find_artist(conn, name)
        conn.execute("DELETE FROM recall_context WHERE artist_id=? AND context=?", (art["id"], context))
    else:
        art = ensure_artist(conn, name, "user", now)
        conn.execute("INSERT OR IGNORE INTO recall_context (artist_id, context, created_at) VALUES (?,?,?)",
                     (art["id"], context, _ts(_now(now))))
    conn.commit()
    return {"artist": art["name"], "context": context, "removed": remove}


def add_note(conn, text: str, now: Optional[datetime] = None) -> dict:
    text = (text or "").strip()
    if not text:
        raise RecallError("note is empty")
    conn.execute("INSERT INTO recall_note (text, created_at) VALUES (?,?)", (text[:500], _ts(_now(now))))
    conn.commit()
    return {"note": text}


# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------

def _neighbors(conn, artist_id: int, limit: int = 3, include_proposed: bool = True) -> list[dict]:
    states = ("accepted", "proposed") if include_proposed else ("accepted",)
    rows = conn.execute(
        f"SELECT l.*, d.name AS dst FROM recall_link l JOIN recall_artist d ON d.id=l.dst_id"
        f" WHERE l.src_id=? AND l.state IN ({','.join('?' * len(states))})"
        f" ORDER BY l.state='accepted' DESC, l.origin='user' DESC, COALESCE(l.confidence,1) DESC",
        (artist_id, *states)).fetchall()
    return [{"link_id": r["id"], "name": r["dst"], "origin": r["origin"], "state": r["state"],
             "reason": r["reason"], "confidence": r["confidence"]} for r in rows[:limit]]


def _artist_view(conn, r, now: datetime) -> dict:
    ev = json.loads(r["evidence"] or "{}")
    until = _dt(r["snooze_until"])
    return {"id": r["id"], "name": r["name"], "origin": r["origin"], "bucket": r["bucket"],
            "listeners": r["listeners"],
            "choice": r["choice"], "snoozed_until": r["snooze_until"] if until and until > now else None,
            "hook": r["hook"], "hook_suggested": r["hook_suggested"],
            "reasons": ev.get("reasons") or [], "playlists": ev.get("playlists") or []}


def list_active(conn, now: Optional[datetime] = None) -> list[dict]:
    now = _now(now)
    rows = conn.execute("SELECT a.*, m.reason AS why, m.selected_at FROM recall_active m JOIN recall_artist a"
                        " ON a.id=m.artist_id WHERE m.retired_at IS NULL"
                        " ORDER BY a.choice='pinned' DESC, a.score DESC").fetchall()
    out = []
    for r in rows:
        v = _artist_view(conn, r, now)
        v.update(why=r["why"], selected_at=r["selected_at"], neighbors=_neighbors(conn, r["id"]))
        out.append(v)
    return out


def artist_detail(conn, name: str, now: Optional[datetime] = None) -> dict:
    now = _now(now)
    art = find_artist(conn, name)
    r = conn.execute("SELECT * FROM recall_artist WHERE id=?", (art["id"],)).fetchone()
    v = _artist_view(conn, r, now)
    v["active"] = bool(conn.execute("SELECT 1 FROM recall_active WHERE artist_id=? AND retired_at IS NULL",
                                    (r["id"],)).fetchone())
    v["links_out"] = _neighbors(conn, r["id"], limit=50)
    v["links_in"] = [{"link_id": x["id"], "name": x["src"], "origin": x["origin"], "state": x["state"],
                      "reason": x["reason"]} for x in conn.execute(
        "SELECT l.*, s.name AS src FROM recall_link l JOIN recall_artist s ON s.id=l.src_id"
        " WHERE l.dst_id=? AND l.state!='rejected'", (r["id"],))]
    v["rejected"] = [x["name"] for x in conn.execute(
        "SELECT d.name FROM recall_link l JOIN recall_artist d ON d.id=l.dst_id"
        " WHERE l.src_id=? AND l.state='rejected'", (r["id"],))]
    v["contexts"] = [x[0] for x in conn.execute("SELECT context FROM recall_context WHERE artist_id=?", (r["id"],))]
    v["aliases"] = [x[0] for x in conn.execute("SELECT alias_norm FROM recall_alias WHERE artist_id=?", (r["id"],))]
    return v


def proposed_links(conn, limit: int = 50) -> list[dict]:
    """Open suggestions — starter first (they're Cory's own words), then strongest evidence."""
    rows = conn.execute(
        "SELECT l.*, s.name AS src, d.name AS dst FROM recall_link l JOIN recall_artist s ON s.id=l.src_id"
        " JOIN recall_artist d ON d.id=l.dst_id WHERE l.state='proposed'"
        " ORDER BY l.origin='starter' DESC, l.origin='data' DESC, COALESCE(l.confidence,0) DESC LIMIT ?",
        (limit,)).fetchall()
    return [{"link_id": r["id"], "src": r["src"], "dst": r["dst"], "origin": r["origin"],
             "reason": r["reason"], "confidence": r["confidence"]} for r in rows]


# ---------------------------------------------------------------------------
# Practice items
# ---------------------------------------------------------------------------

def sync_items(conn, now: Optional[datetime] = None) -> None:
    """Make sure every practiceable relationship has an item; mark the rest not live.

    Schedules the *cue → recommendation* relationship (one item per cue with accepted links),
    not each artist in isolation — naming Lime Garden from a hook and naming her when
    Remember Sports comes up are different practice tasks.
    """
    now = _now(now)
    wanted: dict[str, dict] = {}
    for r in conn.execute("SELECT DISTINCT src_id FROM recall_link WHERE state='accepted'"):
        wanted[f"recommend:{r[0]}"] = {"kind": "recommend", "cue_id": r[0]}
    for r in conn.execute("SELECT id, name, hook FROM recall_artist WHERE hook IS NOT NULL"
                          " AND (choice IS NULL OR choice='pinned')"):
        if not _leaks(r["hook"], [r["name"]]):
            wanted[f"hook:{r['id']}"] = {"kind": "hook", "target_id": r["id"]}
    for r in conn.execute("SELECT DISTINCT context FROM recall_context"):
        wanted[f"context:{r[0]}"] = {"kind": "context", "context": r[0]}
    n_active = conn.execute("SELECT COUNT(*) FROM recall_active WHERE retired_at IS NULL").fetchone()[0]
    if n_active >= 3:
        wanted["lately"] = {"kind": "lately"}

    existing = {r["key"]: r for r in conn.execute("SELECT id, key, live FROM recall_item")}
    for key, spec in wanted.items():
        if key in existing:
            if not existing[key]["live"]:
                conn.execute("UPDATE recall_item SET live=1 WHERE id=?", (existing[key]["id"],))
        else:
            conn.execute("INSERT INTO recall_item (kind, key, cue_id, target_id, context, created_at)"
                         " VALUES (?,?,?,?,?,?)", (spec["kind"], key, spec.get("cue_id"),
                                                    spec.get("target_id"), spec.get("context"), _ts(now)))
    for key, r in existing.items():
        if key not in wanted and r["live"]:
            conn.execute("UPDATE recall_item SET live=0 WHERE id=?", (r["id"],))
    conn.commit()


def _build_prompt(conn, item, now: datetime) -> tuple[str, list[dict]]:
    """(prompt text, expected answers). Expected never goes into the public view."""
    kind = item["kind"]

    def entry(r, reason):
        ev = json.loads(r["evidence"] or "{}")
        return {"id": r["id"], "name": r["name"], "reason": reason, "hook": r["hook"],
                "hook_suggested": r["hook_suggested"], "playlists": ev.get("playlists") or []}

    if kind == "recommend":
        cue = conn.execute("SELECT name FROM recall_artist WHERE id=?", (item["cue_id"],)).fetchone()
        rows = conn.execute("SELECT a.*, l.reason AS lreason, l.origin AS lorigin FROM recall_link l"
                            " JOIN recall_artist a ON a.id=l.dst_id WHERE l.src_id=? AND l.state='accepted'",
                            (item["cue_id"],)).fetchall()
        expected = [entry(r, r["lreason"] or f"your {r['lorigin']} link") for r in rows]
        k = "two artists" if len(expected) >= 2 else "an artist"
        return f"Someone says they like {cue['name']}. Name {k} you'd recommend.", expected
    if kind == "lately":
        rows = conn.execute("SELECT a.*, m.reason AS why FROM recall_active m JOIN recall_artist a"
                            " ON a.id=m.artist_id WHERE m.retired_at IS NULL").fetchall()
        rows = [r for r in rows if _eligible_now(r, now)]
        recent = [r for r in rows if r["bucket"] == "recent"] or rows
        return ("Name three artists you've been excited about lately.",
                [entry(r, r["why"]) for r in sorted(recent, key=lambda r: -(r["score"] or 0))])
    if kind == "context":
        rows = conn.execute("SELECT a.* FROM recall_context c JOIN recall_artist a ON a.id=c.artist_id"
                            " WHERE c.context=?", (item["context"],)).fetchall()
        return (f"You're talking with someone about {item['context']}. Which band would you bring up,"
                f" and what would you say about it?",
                [entry(r, f"you filed them under “{item['context']}”") for r in rows])
    r = conn.execute("SELECT * FROM recall_artist WHERE id=?", (item["target_id"],)).fetchone()
    return f"What artist belongs with this hook? “{r['hook']}”", [entry(r, "your hook")]


def _item_ok(conn, item, now: datetime) -> bool:
    if item["kind"] == "hook":
        r = conn.execute("SELECT * FROM recall_artist WHERE id=?", (item["target_id"],)).fetchone()
        return r is not None and _eligible_now(r, now)
    return True


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------

def plan_session(conn, now: Optional[datetime] = None, prompts: Optional[int] = None,
                 fresh: bool = False) -> dict:
    """Pick up to N items: due first (oldest due), plus a little new material.

    Capped on purpose — an overdue pile stays a pile, it never becomes a longer session.
    At most one hook prompt per session (they're the most trivia-like). ``fresh`` = nothing
    due, practice anyway: take the soonest-due items early.
    """
    now = _now(now)
    sync_items(conn, now)
    n = max(1, min(prompts or get_setting(conn, "session_prompts"), 5))
    new_cap = get_setting(conn, "new_per_session")
    items = [i for i in conn.execute("SELECT * FROM recall_item WHERE live=1").fetchall() if _item_ok(conn, i, now)]
    due = sorted((i for i in items if i["due_at"] and _dt(i["due_at"]) <= now), key=lambda i: i["due_at"])
    order = {"recommend": 0, "context": 1, "lately": 2, "hook": 3}
    new = sorted((i for i in items if not i["due_at"]), key=lambda i: (order[i["kind"]], i["id"]))

    queue, hooks = [], 0

    def take(i):
        nonlocal hooks
        if i["kind"] == "hook":
            if hooks:
                return
            hooks += 1
        queue.append(i["id"])

    new_take = new[:new_cap]
    for i in due:
        if len(queue) >= n - (1 if new_take else 0):
            break
        take(i)
    for i in new_take:
        if len(queue) < n:
            take(i)
    if not queue and fresh:
        later = sorted((i for i in items if i["due_at"]), key=lambda i: i["due_at"])
        for i in later:
            if len(queue) >= n:
                break
            take(i)
    nxt = min((i["due_at"] for i in items if i["due_at"] and _dt(i["due_at"]) > now), default=None)
    return {"queue": queue, "nothing_due": not queue, "next_due": nxt, "has_items": bool(items)}


def start_session(conn, now: Optional[datetime] = None, prompts: Optional[int] = None,
                  fresh: bool = False) -> dict:
    """Plan and persist a session. Returns {"session_id": None, ...} when there's nothing to do."""
    now = _now(now)
    plan = plan_session(conn, now, prompts, fresh)
    if not plan["queue"]:
        plan["session_id"] = None
        return plan
    cur = conn.execute("INSERT INTO recall_session (started_at, queue, fresh) VALUES (?,?,?)",
                       (_ts(now), json.dumps(plan["queue"]), int(fresh)))
    conn.commit()
    plan["session_id"] = cur.lastrowid
    return plan


def _attempt(conn, attempt_id: int):
    a = conn.execute("SELECT * FROM recall_attempt WHERE id=?", (attempt_id,)).fetchone()
    if not a:
        raise RecallError("no such attempt")
    return a


def public_view(conn, a) -> dict:
    """What the prompt screen may show. Deliberately contains NO expected names."""
    s = conn.execute("SELECT queue FROM recall_session WHERE id=?", (a["session_id"],)).fetchone()
    queue = json.loads(s["queue"])
    pos = conn.execute("SELECT COUNT(*) FROM recall_attempt WHERE session_id=? AND id<=?",
                       (a["session_id"], a["id"])).fetchone()[0]
    return {"attempt_id": a["id"], "session_id": a["session_id"], "kind": a["kind"], "prompt": a["prompt"],
            "position": pos, "total": len(queue), "hint_level": a["hint_level"],
            "answered": a["answered_at"] is not None or bool(a["revealed"]), "rated": a["rating"] is not None}


def next_prompt(conn, session_id: int, now: Optional[datetime] = None) -> Optional[dict]:
    """The open attempt, or a new one for the next queued item, or None when done/ended."""
    now = _now(now)
    s = conn.execute("SELECT * FROM recall_session WHERE id=?", (session_id,)).fetchone()
    if not s:
        raise RecallError("no such session")
    if s["ended_at"]:
        return None
    open_a = conn.execute("SELECT * FROM recall_attempt WHERE session_id=? AND rating IS NULL"
                          " ORDER BY id LIMIT 1", (session_id,)).fetchone()
    if open_a:
        return public_view(conn, open_a)
    done = {r[0] for r in conn.execute("SELECT item_id FROM recall_attempt WHERE session_id=?", (session_id,))}
    for item_id in json.loads(s["queue"]):
        if item_id in done:
            continue
        item = conn.execute("SELECT * FROM recall_item WHERE id=?", (item_id,)).fetchone()
        if not item or not item["live"]:
            continue
        prompt, expected = _build_prompt(conn, item, now)
        if not expected:
            continue
        cur = conn.execute("INSERT INTO recall_attempt (session_id, item_id, kind, prompt, expected, shown_at)"
                           " VALUES (?,?,?,?,?,?)", (session_id, item_id, item["kind"], prompt,
                                                     json.dumps(expected, ensure_ascii=False), _ts(now)))
        conn.commit()
        return public_view(conn, _attempt(conn, cur.lastrowid))
    return None


def hint(conn, attempt_id: int) -> dict:
    """Graduated hint. Level 1: a hook or playlist context for an expected answer not yet
    named (skipping any text that would spell the name). Level 2: first letters."""
    a = _attempt(conn, attempt_id)
    if a["rating"] or a["answered_at"] or a["revealed"]:
        raise RecallError("hints are for before you answer")
    expected = json.loads(a["expected"])
    names = [e["name"] for e in expected]
    level = a["hint_level"] + 1
    text = None
    if level == 1:
        for e in expected[:3]:
            if e.get("hook") and not _leaks(e["hook"], names) and a["kind"] != "hook":
                text = f"Your hook: “{e['hook']}”"
                break
            safe = [p for p in e.get("playlists") or [] if not _leaks(p, names)]
            if safe:
                text = f"One of them is in your playlist “{safe[0]}”"
                break
            if e.get("hook_suggested") and not _leaks(e["hook_suggested"], names):
                text = f"From your listening — {e['hook_suggested']}"
                break
        if text is None:
            level = 2
    if level >= 2:
        level = 2
        letters = [e["name"][0].upper() + "…" for e in expected[:3]]
        text = "Starts with: " + ", ".join(letters)
    conn.execute("UPDATE recall_attempt SET hint_level=? WHERE id=?", (level, attempt_id))
    conn.commit()
    return {"hint_level": level, "hint": text}


def _feedback(conn, a) -> dict:
    """The reveal: expected answers (with reasons), how each answer resolved, other
    neighbors as examples (not an answer key), and a ready-to-say sentence."""
    expected = json.loads(a["expected"])
    resolved = json.loads(a["resolved"] or "[]")
    exp_ids = {e["id"] for e in expected}
    hit_ids = {r["artist_id"] for r in resolved if r["artist_id"] in exp_ids}
    active_ids = {r[0] for r in conn.execute("SELECT artist_id FROM recall_active WHERE retired_at IS NULL")}
    for r in resolved:
        r["expected"] = r["artist_id"] in exp_ids
        r["in_active_set"] = r["artist_id"] in active_ids
    others = []
    item = conn.execute("SELECT * FROM recall_item WHERE id=?", (a["item_id"],)).fetchone()
    cue = None
    if item and item["kind"] == "recommend":
        cue = conn.execute("SELECT id, name FROM recall_artist WHERE id=?", (item["cue_id"],)).fetchone()
        others = [n for n in _neighbors(conn, item["cue_id"], limit=6) if n["state"] == "proposed"]
    sentence = None
    if cue and expected:
        picks = [e["name"] for e in expected if e["id"] in hit_ids] + \
                [e["name"] for e in expected if e["id"] not in hit_ids]
        sentence = f"You like {cue['name']}? Have you heard {' or '.join(picks[:2])}?"
    elif expected and a["kind"] in ("lately", "context"):
        picks = [r["name"] for r in resolved if r["artist_id"]] or [e["name"] for e in expected]
        sentence = f"Lately I've been into {', '.join(picks[:3])}."
    return {"attempt_id": a["id"], "prompt": a["prompt"], "kind": a["kind"], "answer": a["answer"],
            "cue": cue["name"] if cue else None, "cue_id": cue["id"] if cue else None,
            "expected": [dict(e, hit=e["id"] in hit_ids) for e in expected],
            "resolved": resolved, "suggestions": others, "sentence": sentence,
            "hint_level": a["hint_level"], "revealed_early": bool(a["revealed"])}


def submit(conn, attempt_id: int, text: str, now: Optional[datetime] = None) -> dict:
    """Record a free-text answer (empty = skip) and return the reveal."""
    now = _now(now)
    a = _attempt(conn, attempt_id)
    if a["rating"]:
        raise RecallError("this prompt is already rated")
    if a["answered_at"]:
        return _feedback(conn, a)
    expected = json.loads(a["expected"])
    resolved = resolve_answer(conn, text or "", tuple(e["id"] for e in expected))
    elapsed = (now - _dt(a["shown_at"])).total_seconds()
    conn.execute("UPDATE recall_attempt SET answer=?, resolved=?, answered_at=?, elapsed_s=? WHERE id=?",
                 ((text or "").strip(), json.dumps(resolved, ensure_ascii=False), _ts(now),
                  round(elapsed, 1), attempt_id))
    conn.commit()
    return _feedback(conn, _attempt(conn, attempt_id))


def reveal(conn, attempt_id: int, now: Optional[datetime] = None) -> dict:
    """Show the answers. Before any answer, that counts as a reveal (scheduled as a miss)."""
    a = _attempt(conn, attempt_id)
    if not a["answered_at"] and not a["revealed"]:
        conn.execute("UPDATE recall_attempt SET revealed=1, answered_at=?, answer='', resolved='[]' WHERE id=?",
                     (_ts(_now(now)), attempt_id))
        conn.commit()
        a = _attempt(conn, attempt_id)
    return _feedback(conn, a)


def schedule(item, rating: str, hint_level: int, revealed_early: bool, ladder: list[int],
             relearn_days: int, now: datetime) -> dict:
    """The review rule — plain and predictable, not a claim of optimality.

    couldn't / revealed early → back in ``relearn_days``, ladder resets.
    hinted success           → half the current step, no promotion.
    'took a moment'          → the current step again.
    'came easily', no help   → the current step, then promote (3 → 7 → 14 → 30 → 30).
    """
    step = item["step"] or 0
    step = min(step, len(ladder) - 1)
    lapses = item["lapses"] or 0
    if rating == "couldnt" or revealed_early:
        interval, step, lapses = float(relearn_days), 0, lapses + 1
    elif hint_level > 0:
        interval = max(float(relearn_days), ladder[step] / 2)
    elif rating == "moment":
        interval = float(ladder[step])
    else:
        interval = float(ladder[step])
        step = min(step + 1, len(ladder) - 1)
    return {"step": step, "interval_days": interval, "lapses": lapses,
            "due_at": _ts(now + timedelta(days=interval))}


def rate(conn, attempt_id: int, rating: str, now: Optional[datetime] = None) -> dict:
    now = _now(now)
    if rating not in RATINGS:
        raise RecallError(f"rating must be one of {', '.join(RATINGS)}")
    a = _attempt(conn, attempt_id)
    if a["rating"]:
        raise RecallError("already rated")
    if not a["answered_at"]:
        raise RecallError("answer, skip, or reveal before rating")
    item = conn.execute("SELECT * FROM recall_item WHERE id=?", (a["item_id"],)).fetchone()
    s = schedule(item, rating, a["hint_level"], bool(a["revealed"]), get_setting(conn, "ladder"),
                 get_setting(conn, "relearn_days"), now)
    conn.execute("UPDATE recall_item SET step=?, interval_days=?, lapses=?, due_at=?, reps=reps+1,"
                 " last_rating=?, last_seen_at=? WHERE id=?",
                 (s["step"], s["interval_days"], s["lapses"], s["due_at"], rating, _ts(now), item["id"]))
    conn.execute("UPDATE recall_attempt SET rating=?, rated_at=? WHERE id=?", (rating, _ts(now), attempt_id))
    conn.commit()
    return {"rating": rating, "next_due": s["due_at"], "interval_days": s["interval_days"]}


def end_session(conn, session_id: int, now: Optional[datetime] = None) -> dict:
    """Close the session (safe mid-way: rated attempts are kept, unrated ones leave their
    item's schedule untouched) and return a compact recap."""
    now = _now(now)
    s = conn.execute("SELECT * FROM recall_session WHERE id=?", (session_id,)).fetchone()
    if not s:
        raise RecallError("no such session")
    if not s["ended_at"]:
        conn.execute("UPDATE recall_session SET ended_at=? WHERE id=?", (_ts(now), session_id))
        conn.commit()
    attempts = conn.execute("SELECT * FROM recall_attempt WHERE session_id=? ORDER BY id", (session_id,)).fetchall()
    recalled, revisit, sentence = [], [], None
    for a in attempts:
        fb = _feedback(conn, a)
        if not a["revealed"]:
            for r in fb["resolved"]:
                if r["name"] and r["name"] not in recalled and (r["expected"] or r["in_active_set"]):
                    recalled.append(r["name"])
        if a["rating"] == "couldnt" or a["revealed"] or a["hint_level"]:
            label = (f"{fb['cue']} → " if fb["cue"] else "") + ", ".join(e["name"] for e in fb["expected"][:3])
            revisit.append(label)
        if sentence is None and fb["sentence"] and fb["cue"]:
            sentence = fb["sentence"]
    return {"session_id": session_id, "prompts": len(attempts),
            "rated": sum(1 for a in attempts if a["rating"]),
            "recalled": recalled, "revisit": revisit[:2], "sentence": sentence}


# ---------------------------------------------------------------------------
# Stats — modest by design: no scores, no streaks
# ---------------------------------------------------------------------------

def stats(conn, now: Optional[datetime] = None, days: int = 14) -> dict:
    now = _now(now)
    since = _ts(now - timedelta(days=days))
    unassisted: dict[str, int] = {}
    for a in conn.execute("SELECT * FROM recall_attempt WHERE rated_at >= ? AND rating IN ('easy','moment')"
                          " AND hint_level=0 AND revealed=0", (since,)):
        exp_ids = {e["id"] for e in json.loads(a["expected"])}
        for r in json.loads(a["resolved"] or "[]"):
            if r.get("artist_id") in exp_ids:
                unassisted[r["name"]] = unassisted.get(r["name"], 0) + 1
    difficult = []
    for i in conn.execute("SELECT * FROM recall_item WHERE live=1 AND (last_rating='couldnt' OR lapses>=2)"
                          " ORDER BY lapses DESC, last_seen_at DESC LIMIT 5"):
        last = conn.execute("SELECT prompt FROM recall_attempt WHERE item_id=? ORDER BY id DESC LIMIT 1",
                            (i["id"],)).fetchone()
        difficult.append({"prompt": last["prompt"] if last else i["key"], "lapses": i["lapses"],
                          "next_due": i["due_at"]})
    items = conn.execute("SELECT due_at FROM recall_item WHERE live=1").fetchall()
    return {
        "window_days": days,
        "unassisted_recalls": sorted(unassisted.items(), key=lambda kv: -kv[1]),
        "difficult": difficult,
        "active_set": conn.execute("SELECT COUNT(*) FROM recall_active WHERE retired_at IS NULL").fetchone()[0],
        "recommendations": conn.execute("SELECT COUNT(*) FROM recall_link WHERE state='accepted'").fetchone()[0],
        "proposed": conn.execute("SELECT COUNT(*) FROM recall_link WHERE state='proposed'").fetchone()[0],
        "due_now": sum(1 for i in items if i["due_at"] and _dt(i["due_at"]) <= now),
        "new_items": sum(1 for i in items if not i["due_at"]),
        "notes": [dict(r) for r in conn.execute("SELECT text, created_at FROM recall_note"
                                                " ORDER BY id DESC LIMIT 5")],
    }
