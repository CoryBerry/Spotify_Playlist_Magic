"""Tests for the snapshot-keyed disk cache in mix_helper (issue #3).

Runs offline against a fake Spotify client that counts calls, so we can assert
the cache actually skips `playlist_items` on an unchanged playlist and re-pulls
when the playlist's snapshot_id changes.

    PYTHONUTF8=1 python -m pytest .claude/skills/mix/test_mix_cache.py
"""
import importlib

import pytest

mh = importlib.import_module("mix_helper")


class FakeSpotify:
    """Minimal stand-in for a Spotipy client used by the fetch helpers.

    `snapshot` is mutable so a test can simulate editing the playlist. Every
    `playlist_items` page pull is counted so we can prove cache hits skip them.
    """

    def __init__(self, snapshot, tracks):
        self.snapshot = snapshot
        self._tracks = tracks
        self.items_calls = 0
        self.snapshot_calls = 0

    def playlist(self, pid, fields=None):
        self.snapshot_calls += 1
        return {"snapshot_id": self.snapshot}

    def playlist_items(self, pid, additional_types=None, limit=100):
        self.items_calls += 1
        items = [{"track": t} for t in self._tracks]
        return {"items": items, "next": None}

    def next(self, res):
        return None


def _raw_track(tid, pop=50):
    return {
        "id": tid,
        "uri": f"spotify:track:{tid}",
        "name": f"Song {tid}",
        "artists": [{"name": "Artist"}],
        "album": {"id": f"alb-{tid}", "name": f"Album {tid}"},
        "popularity": pop,
    }


@pytest.fixture(autouse=True)
def _tmp_cache(tmp_path, monkeypatch):
    """Point the module's cache dir at a throwaway temp dir for every test."""
    monkeypatch.setattr(mh, "MIX_CACHE_DIR", str(tmp_path / ".mix_cache"))


def test_second_run_serves_from_cache_no_item_calls():
    sp = FakeSpotify("snap-1", [_raw_track("a"), _raw_track("b")])

    first = mh._fetch_tracks_rich_cached(sp, "pid")
    assert sp.items_calls == 1
    assert [t["uri"] for t in first] == ["spotify:track:a", "spotify:track:b"]

    second = mh._fetch_tracks_rich_cached(sp, "pid")
    assert sp.items_calls == 1  # no new playlist_items pull — served from disk
    assert second == first


def test_snapshot_change_triggers_repull():
    sp = FakeSpotify("snap-1", [_raw_track("a")])
    mh._fetch_tracks_rich_cached(sp, "pid")
    assert sp.items_calls == 1

    # simulate an edit: playlist gains a track and its snapshot flips
    sp.snapshot = "snap-2"
    sp._tracks = [_raw_track("a"), _raw_track("c")]
    out = mh._fetch_tracks_rich_cached(sp, "pid")
    assert sp.items_calls == 2  # re-pulled
    assert [t["uri"] for t in out] == ["spotify:track:a", "spotify:track:c"]


def test_no_cache_forces_live_pull_and_refreshes():
    sp = FakeSpotify("snap-1", [_raw_track("a")])
    mh._fetch_tracks_rich_cached(sp, "pid")
    assert sp.items_calls == 1

    # --no-cache: pull live even though snapshot is unchanged
    mh._fetch_tracks_rich_cached(sp, "pid", use_cache=False)
    assert sp.items_calls == 2

    # the forced pull should have refreshed the cache, so a normal run is a hit
    mh._fetch_tracks_rich_cached(sp, "pid", use_cache=True)
    assert sp.items_calls == 2


def test_cache_preserves_roster_fields():
    """AC4: roster band-selection ranks on album_id/album/pop — those must survive
    the JSON round-trip so roster output is identical whether live or cached."""
    sp = FakeSpotify("snap-1", [_raw_track("a", pop=71), _raw_track("b", pop=12)])
    live = mh._fetch_tracks_rich_cached(sp, "pid", use_cache=False)  # fresh pull
    cached = mh._fetch_tracks_rich_cached(sp, "pid", use_cache=True)  # from disk
    assert cached == live
    for t in cached:
        assert set(t) >= {"uri", "name", "artist", "album_id", "album", "pop"}
    assert [t["pop"] for t in cached] == [71, 12]
    assert [t["album_id"] for t in cached] == ["alb-a", "alb-b"]


def test_tuple_fetch_derives_from_same_cache():
    sp = FakeSpotify("snap-1", [_raw_track("a"), _raw_track("b")])
    # populate cache via the rich path...
    mh._fetch_tracks_rich_cached(sp, "pid")
    assert sp.items_calls == 1
    # ...then the tuple path reuses it (no new item calls) and shapes 3-tuples
    tuples = mh._fetch_tracks_cached(sp, "pid")
    assert sp.items_calls == 1
    assert tuples == [
        ("spotify:track:a", "Song a", "Artist"),
        ("spotify:track:b", "Song b", "Artist"),
    ]
