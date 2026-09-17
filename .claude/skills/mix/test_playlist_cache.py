"""Tests for the skill writing the app's playlist_cache blob (issue #13).

Before this, mix_helper only ever *read* `playlist_cache` — so a playlist the
skill created was unresolvable by `roster` / `tracks` / `create --source` until
someone opened the web app. These run offline against a throwaway SQLite file
and a fake Spotify client.

    PYTHONUTF8=1 python -m pytest .claude/skills/mix/test_playlist_cache.py
"""
import argparse
import importlib
import json
import sqlite3

import pytest

mh = importlib.import_module("mix_helper")

USER = "wirefunk"


@pytest.fixture(autouse=True)
def _tmp_db(tmp_path, monkeypatch):
    """Point the module at a throwaway DB holding just the playlist_cache table."""
    path = str(tmp_path / "spotify_tools.db")
    db = sqlite3.connect(path)
    db.execute(
        "CREATE TABLE playlist_cache ("
        " id INTEGER PRIMARY KEY,"
        " user_id VARCHAR(100) NOT NULL UNIQUE,"
        " data TEXT NOT NULL,"
        " updated_at DATETIME)"
    )
    db.commit()
    db.close()
    monkeypatch.setattr(mh, "DB_PATH", path)
    return path


def _pl(pid, name, total=0):
    return {"id": pid, "name": name, "tracks": {"total": total},
            "external_urls": {"spotify": f"https://open.spotify.com/playlist/{pid}"}}


def _seed(playlists, user_id=USER):
    mh._write_playlist_cache(playlists, user_id)


def _rows():
    db = sqlite3.connect(mh.DB_PATH)
    return db.execute("SELECT user_id, data, updated_at FROM playlist_cache").fetchall()


# ---------------------------------------------------------------- read / write

def test_read_returns_none_when_row_absent():
    assert mh._read_playlist_cache() is None


def test_cached_playlists_error_points_at_refresh_cache():
    with pytest.raises(SystemExit) as exc:
        mh._cached_playlists()
    assert "refresh-cache" in str(exc.value)


def test_read_returns_none_on_corrupt_blob():
    db = sqlite3.connect(mh.DB_PATH)
    db.execute("INSERT INTO playlist_cache (user_id, data) VALUES (?, ?)",
               (USER, "{not json"))
    db.commit()
    assert mh._read_playlist_cache() is None


def test_write_inserts_then_updates_one_row():
    _seed([_pl("p1", "One")])
    assert len(_rows()) == 1

    _seed([_pl("p1", "One"), _pl("p2", "Two")])
    rows = _rows()
    assert len(rows) == 1  # still a single row, as the app expects
    assert [p["name"] for p in json.loads(rows[0][1])] == ["One", "Two"]


def test_write_rekeys_a_row_belonging_to_another_user_id():
    """A user_id mismatch must not leave two rows for LIMIT 1 to choose between."""
    _seed([_pl("p1", "One")], user_id="someone-else")
    mh._write_playlist_cache([_pl("p1", "One"), _pl("p2", "Two")], USER)
    rows = _rows()
    assert len(rows) == 1
    assert rows[0][0] == USER


def test_write_bumps_updated_at():
    _seed([_pl("p1", "One")])
    before = _rows()[0][2]
    _seed([_pl("p1", "One"), _pl("p2", "Two")])
    assert _rows()[0][2] >= before
    assert before is not None


# ---------------------------------------------------------------- upsert

def test_upsert_prepends_and_is_resolvable_immediately():
    """AC1: a playlist the skill just made resolves by name AND by id right away."""
    _seed([_pl("old", "Old Pool", total=12)])
    n = mh._cache_upsert_playlist(_pl("new", "Sunday Slow Burn"), USER, track_total=24)
    assert n == 2

    pls = mh._cached_playlists()
    assert [p["id"] for p in pls] == ["new", "old"]  # newest first, like Spotify

    by_id = {p["id"]: p["name"] for p in pls}
    by_name = {p["name"]: p["id"] for p in pls}
    assert mh._resolve("new", by_id, by_name) == ("Sunday Slow Burn", "new")
    assert mh._resolve("slow burn", by_id, by_name) == ("Sunday Slow Burn", "new")


def test_upsert_overrides_the_zero_track_total_from_create():
    """user_playlist_create reports 0 tracks (they're added after), which would
    otherwise show up in `sources` as an empty playlist."""
    mh._cache_upsert_playlist(_pl("new", "Fresh", total=0), USER, track_total=42)
    (entry,) = mh._cached_playlists()
    assert entry["tracks"]["total"] == 42


def test_upsert_replaces_rather_than_duplicates_an_existing_id():
    _seed([_pl("p1", "Old Name", total=5), _pl("p2", "Other")])
    mh._cache_upsert_playlist(_pl("p1", "New Name"), USER, track_total=9)
    pls = mh._cached_playlists()
    assert [p["id"] for p in pls] == ["p1", "p2"]
    assert pls[0]["name"] == "New Name"
    assert pls[0]["tracks"]["total"] == 9


def test_upsert_works_from_an_empty_cache():
    assert mh._cache_upsert_playlist(_pl("new", "First"), USER, track_total=3) == 1
    assert mh._cached_playlists()[0]["name"] == "First"


def test_upsert_without_track_total_keeps_the_objects_own_count():
    mh._cache_upsert_playlist(_pl("new", "Fresh", total=7), USER)
    assert mh._cached_playlists()[0]["tracks"]["total"] == 7


# ---------------------------------------------------------------- refresh-cache

class FakeSpotify:
    """Paginating stand-in for the two calls cmd_refresh_cache makes."""

    def __init__(self, pages, uid=USER):
        self._pages = pages
        self._uid = uid
        self.page_calls = 0

    def me(self):
        return {"id": self._uid}

    def current_user_playlists(self, limit=50):
        self.page_calls += 1
        return self._page(0)

    def _page(self, idx):
        return {"items": self._pages[idx],
                "next": "more" if idx + 1 < len(self._pages) else None,
                "_idx": idx}

    def next(self, res):
        self.page_calls += 1
        return self._page(res["_idx"] + 1)


def test_refresh_cache_paginates_and_reports_the_count(monkeypatch, capsys):
    sp = FakeSpotify([[_pl("p1", "One"), _pl("p2", "Two")], [_pl("p3", "Three")]])
    monkeypatch.setattr(mh, "_client", lambda: sp)

    mh.cmd_refresh_cache(argparse.Namespace())

    assert sp.page_calls == 2  # followed the `next` link
    out = capsys.readouterr().out
    assert "3 playlists" in out and USER in out
    assert [p["id"] for p in mh._cached_playlists()] == ["p1", "p2", "p3"]


def test_refresh_cache_overwrites_a_stale_blob(monkeypatch):
    _seed([_pl("gone", "Deleted Pool")])
    sp = FakeSpotify([[_pl("p1", "One")]])
    monkeypatch.setattr(mh, "_client", lambda: sp)

    mh.cmd_refresh_cache(argparse.Namespace())
    assert [p["id"] for p in mh._cached_playlists()] == ["p1"]


def test_refresh_cache_keeps_the_old_blob_when_spotify_returns_nothing(monkeypatch):
    """An empty response is far more likely a transient API blip than an emptied
    library — clobbering a good cache with [] would break every later resolve."""
    _seed([_pl("p1", "One")])
    monkeypatch.setattr(mh, "_client", lambda: FakeSpotify([[]]))

    with pytest.raises(SystemExit):
        mh.cmd_refresh_cache(argparse.Namespace())
    assert [p["id"] for p in mh._cached_playlists()] == ["p1"]
