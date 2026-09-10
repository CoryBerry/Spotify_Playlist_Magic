"""Command-line interface for building Spotify playlists — the prompt-free hand-off.

This is what Claude (or you) calls *after* a tracklist has been chosen. The judgment —
mood, tempo, taste, sequencing — happens upstream when the list is written; this CLI's
only job is to resolve those lines to real Spotify tracks and create the playlist, with
no interactive prompts of any kind. Ambiguous matches are resolved by policy (a confidence
threshold) and anything that doesn't clear it is *reported as a miss*, never asked about.

It wraps ``spotify_service`` (the same headless client + resolver), which imports no Flask
and reads no session — so this runs without starting the web app.

Input: a list of ``Artist - Title`` lines, one per line (blank lines and ``#`` comments
ignored), OR a JSON array of strings. From a file (``--from tracks.txt``) or stdin.

Usage:

    python cli.py login
    python cli.py resolve --from tracks.txt                 # dry run — show matches, create nothing
    python cli.py create --name "Rainy Sunday" --from tracks.txt --description "mellow 80-100bpm"
    cat tracks.txt | python cli.py create --name "Focus" -
    python cli.py backup                                    # dump the DB to profile/backups/

Pass ``--json`` on any command for machine-readable output (what an agent parses).
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from typing import Optional

from dotenv import load_dotenv

from spotify_service import (
    SpotifyAuthError,
    create_playlist_from_lines,
    get_headless_client,
    load_resolve_cache,
    resolve_tracks,
    save_resolve_cache,
)

load_dotenv()


# --- input helpers ---------------------------------------------------------

def _read_source(source: Optional[str]) -> str:
    """Read raw text from a file path, or from stdin when source is '-' or None."""
    if source in (None, "-"):
        if sys.stdin.isatty():
            raise SystemExit("error: no input — pass a file with --from, or pipe a tracklist on stdin")
        return sys.stdin.read()
    with open(source, "r", encoding="utf-8") as fh:
        return fh.read()


def _parse_lines(raw: str) -> list[str]:
    """Accept either a JSON array of strings or newline-delimited lines."""
    stripped = raw.strip()
    if stripped.startswith("["):
        try:
            data = json.loads(stripped)
            if isinstance(data, list):
                return [str(x).strip() for x in data if str(x).strip()]
        except json.JSONDecodeError:
            pass  # fall through to line parsing
    return [ln for ln in raw.splitlines()]


# --- output helpers --------------------------------------------------------

def _emit(payload: dict, as_json: bool, human) -> None:
    if as_json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        human(payload)


def _fail(msg: str, as_json: bool, code: int = 1) -> int:
    if as_json:
        print(json.dumps({"error": msg}, indent=2))
    else:
        print(f"error: {msg}", file=sys.stderr)
    return code


# --- commands --------------------------------------------------------------

def cmd_login(args) -> int:
    try:
        sp = get_headless_client()
        me = sp.me()
    except SpotifyAuthError as exc:
        return _fail(str(exc), args.json)
    payload = {"authenticated": True, "user_id": me["id"], "display_name": me.get("display_name")}
    _emit(payload, args.json, lambda p: print(f"Authenticated as {p['display_name']} ({p['user_id']})"))
    return 0


def cmd_resolve(args) -> int:
    lines = _parse_lines(_read_source(args.from_file or args.source))
    if not any(l.strip() and not l.strip().startswith("#") for l in lines):
        return _fail("no track lines to resolve", args.json)
    try:
        sp = get_headless_client()
    except SpotifyAuthError as exc:
        return _fail(str(exc), args.json)

    cache = {} if args.no_cache else load_resolve_cache()
    resolved, misses = resolve_tracks(sp, lines, threshold=args.threshold, cache=cache)
    if not args.no_cache:
        save_resolve_cache(cache)

    payload = {"resolved": resolved, "missed": misses,
               "counts": {"resolved": len(resolved), "missed": len(misses)}}

    def _human(p):
        for r in p["resolved"]:
            mark = "~" if r["source"] == "cache" else " "
            print(f"  [{mark}] {r['artist']} - {r['name']}  ({r['score']})")
        for m in p["missed"]:
            hint = f" — closest: {m['best']['artist']} - {m['best']['name']} ({m['best']['score']})" if m["best"] else ""
            print(f"  [MISS] {m['line']}{hint}")
        print(f"\n{len(p['resolved'])} resolved, {len(p['missed'])} missed (nothing created — dry run)")

    _emit(payload, args.json, _human)
    return 0


def cmd_create(args) -> int:
    lines = _parse_lines(_read_source(args.from_file or args.source))
    if not any(l.strip() and not l.strip().startswith("#") for l in lines):
        return _fail("no track lines to build from", args.json)
    try:
        sp = get_headless_client()
    except SpotifyAuthError as exc:
        return _fail(str(exc), args.json)

    cache = {} if args.no_cache else load_resolve_cache()
    result = create_playlist_from_lines(
        sp,
        name=args.name,
        lines=lines,
        description=args.description or "",
        public=args.public,
        threshold=args.threshold,
        cache=cache,
    )
    if not args.no_cache:
        save_resolve_cache(cache)

    def _human(p):
        if not p["created"]:
            print(f"Nothing created ({p['reason']}). {len(p['missed'])} lines missed.")
            return
        print(f"Created: {p['name']}")
        print(f"  {p['playlist_url']}")
        print(f"  {p['track_count']} tracks in {p['gen_seconds']}s, {len(p['missed'])} missed")
        for m in p["missed"]:
            print(f"    [MISS] {m['line']}")

    _emit(result, args.json, _human)
    # Exit non-zero if nothing was created, so callers can detect total failure.
    return 0 if result["created"] else 2


# --- backup ----------------------------------------------------------------

# Regenerable tables, skipped so the dump stays small and its diffs stay readable.
# playlist_cache is a 1-hour mirror of the Spotify playlist list — it refills itself on the
# next page load, and including it would churn the whole file on every single backup.
BACKUP_SKIP_TABLES = {"playlist_cache"}

DEFAULT_DB   = os.path.join("instance", "spotify_tools.db")
DEFAULT_DUMP = os.path.join("profile", "backups", "spotify_tools.sql")


def dump_db(db_path: str, out_path: str, skip: set[str] = BACKUP_SKIP_TABLES) -> dict:
    """Write a git-friendly SQL text dump of the DB, minus the regenerable cache tables.

    Text rather than a binary copy on purpose: the overlay repo is a git repo, so a dump
    that diffs line-by-line keeps its history browsable and its packfiles small.
    """
    if not os.path.exists(db_path):
        raise FileNotFoundError(db_path)

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            " ORDER BY name")]
        kept = [t for t in tables if t not in skip]
        counts = {t: conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0] for t in kept}

        # iterdump() emits the whole DB; filter to the statements for the tables we keep.
        # Tracking the current table across lines handles multi-line CREATE statements.
        lines, current, emitting = [], None, True
        for stmt in conn.iterdump():
            head = stmt.lstrip()
            for kind in ("CREATE TABLE", "INSERT INTO", "CREATE INDEX", "CREATE UNIQUE INDEX"):
                if head.upper().startswith(kind):
                    current = _stmt_table(head, kind)
                    emitting = current not in skip
                    break
            else:
                if head.upper().startswith(("BEGIN", "COMMIT", "PRAGMA")):
                    emitting = True
            if emitting:
                lines.append(stmt)
    finally:
        conn.close()

    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(lines) + "\n")

    return {"db": db_path, "out": out_path, "tables": counts,
            "skipped": sorted(t for t in tables if t in skip),
            "rows": sum(counts.values())}


def _stmt_table(head: str, kind: str) -> str:
    """Pull the table name out of a CREATE/INSERT statement, unquoted."""
    rest = head[len(kind):].strip()
    if kind.startswith("CREATE INDEX") or kind.startswith("CREATE UNIQUE INDEX"):
        # "<index> ON <table> (...)" — the table is what follows ON.
        parts = rest.split(" ON ", 1)
        rest = parts[1] if len(parts) > 1 else rest
    name = rest.split("(")[0].split()[0]
    return name.strip('''"'`[]''')


def cmd_backup(args) -> int:
    try:
        result = dump_db(args.db, args.out)
    except FileNotFoundError as e:
        print(f"error: no database at {e}", file=sys.stderr)
        return 1

    def _human(r):
        print(f"wrote {r['out']}  ({r['rows']} rows across {len(r['tables'])} tables)")
        for t, n in sorted(r["tables"].items()):
            print(f"  {t:<20} {n}")
        if r["skipped"]:
            print(f"  (skipped regenerable: {', '.join(r['skipped'])})")

    _emit(result, args.json, _human)
    return 0


# --- argparse wiring -------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="spotify-cli", description="Build Spotify playlists, prompt-free.")
    sub = p.add_subparsers(dest="command", required=True)

    # Shared flag, applied to every subcommand so it can appear after the command name
    # (e.g. `create --json`). Defining it once via a parent avoids the before/after ambiguity.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", help="machine-readable JSON output")

    sp_login = sub.add_parser("login", parents=[common], help="verify the cached Spotify token works")
    sp_login.set_defaults(func=cmd_login)

    def _add_io(sp):
        sp.add_argument("source", nargs="?", default=None,
                        help="path to a tracklist file, or '-'/omit for stdin")
        # Separate dest from the positional: sharing one lets the absent positional's
        # default clobber --from, silently falling back to a stdin read that hangs.
        sp.add_argument("--from", dest="from_file", default=None,
                        help="path to a tracklist file (alias for the positional arg)")
        sp.add_argument("--threshold", type=float, default=0.6,
                        help="min match confidence to auto-accept (0-1, default 0.6)")
        sp.add_argument("--no-cache", action="store_true",
                        help="ignore and don't write the line->URI resolution cache")

    sp_resolve = sub.add_parser("resolve", parents=[common],
                                help="dry run: show what would be added, create nothing")
    _add_io(sp_resolve)
    sp_resolve.set_defaults(func=cmd_resolve)

    sp_create = sub.add_parser("create", parents=[common],
                               help="resolve lines and create the playlist")
    _add_io(sp_create)
    sp_create.add_argument("--name", required=True, help="playlist name")
    sp_create.add_argument("--description", default="", help="playlist description")
    sp_create.add_argument("--public", action="store_true", help="make the playlist public (default private)")
    sp_create.set_defaults(func=cmd_create)

    sp_backup = sub.add_parser("backup", parents=[common],
                               help="dump the DB to a git-friendly .sql file in the profile overlay")
    sp_backup.add_argument("--db", default=DEFAULT_DB, help=f"path to the SQLite DB (default {DEFAULT_DB})")
    sp_backup.add_argument("--out", default=DEFAULT_DUMP, help=f"path to write (default {DEFAULT_DUMP})")
    sp_backup.set_defaults(func=cmd_backup)

    return p


def main(argv: Optional[list[str]] = None) -> int:
    # Track/artist names are full of non-ASCII; keep the Windows console from mangling them.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
