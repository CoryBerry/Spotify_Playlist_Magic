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


