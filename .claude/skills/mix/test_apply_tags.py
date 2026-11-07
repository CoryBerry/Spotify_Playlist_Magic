"""Tests for create/replace --tag (the _apply_tags seam).

Album pools are titled by scene ("Texas Songwriters"), so a genre tag is what makes
them findable. Runs against an in-memory playlist_tag with the app's unique key.

    PYTHONUTF8=1 python -m pytest .claude/skills/mix/test_apply_tags.py
"""
import importlib
import sqlite3

mh = importlib.import_module("mix_helper")


def _db():
    db = sqlite3.connect(":memory:")
    db.execute(
        "CREATE TABLE playlist_tag (id INTEGER PRIMARY KEY, user_id TEXT NOT NULL, "
        "playlist_id TEXT NOT NULL, tag TEXT NOT NULL, created_at TEXT, "
        "UNIQUE (user_id, playlist_id, tag))"
    )
    return db


def _tags(db, pid):
    return [r[0] for r in db.execute(
        "SELECT tag FROM playlist_tag WHERE playlist_id=? ORDER BY id", (pid,))]


def test_tags_attach_normalized():
    db = _db()
    assert mh._apply_tags(db, "p1", ["pool", " Country "]) == ["pool", "country"]
    assert _tags(db, "p1") == ["pool", "country"]


def test_idempotent_rerun_adds_nothing():
    db = _db()
    mh._apply_tags(db, "p1", ["pool", "country"])
    assert mh._apply_tags(db, "p1", ["country", "folk"]) == ["folk"]
    assert _tags(db, "p1") == ["pool", "country", "folk"]


def test_folder_prefix_and_blanks_refused():
    db = _db()
    assert mh._apply_tags(db, "p1", ["folder:Country", "", "  "]) == []
    assert _tags(db, "p1") == []


def test_no_tags_is_noop():
    db = _db()
    assert mh._apply_tags(db, "p1", None) == []
