"""Tests for `find-artists` — the offline sweep of already-pulled pools (issue #16).

The command's whole value is that it costs nothing: no Spotify calls, every cached
pool searched at once. So `_client` is monkeypatched to blow up — any API call
fails the test rather than quietly working.

    PYTHONUTF8=1 python -m pytest .claude/skills/mix/test_find_artists.py
"""
import argparse
import importlib
import json

import pytest

mh = importlib.import_module("mix_helper")


@pytest.fixture(autouse=True)
def _offline(tmp_path, monkeypatch):
    """Throwaway cache dir, known pool names, and a client that must never be used."""
    monkeypatch.setattr(mh, "MIX_CACHE_DIR", str(tmp_path / ".mix_cache"))
    monkeypatch.setattr(mh, "_read_playlist_cache", lambda: [
        {"id": "p1", "name": "Albums - Dance-Punk I"},
        {"id": "p2", "name": "2000s Albums"},
    ])

    def _boom():
        raise AssertionError("find-artists must not touch Spotify")

    monkeypatch.setattr(mh, "_client", _boom)


def _track(name, artist, pop=50, **extra):
    t = {"uri": f"spotify:track:{name.lower().replace(' ', '')}", "name": name,
         "artist": artist, "album_id": "alb", "album": "An Album", "pop": pop,
         "duration_ms": 210_000, "year": 2004, "explicit": False}
    t.update(extra)
    return t


def _pool(pid, tracks, version=mh.MIX_CACHE_VERSION):
    mh._write_cache(pid, f"snap-{pid}", tracks)
    if version != mh.MIX_CACHE_VERSION:  # simulate a pre-v2 blob
        path = mh._cache_file(pid)
        with open(path, encoding="utf-8") as fh:
            blob = json.load(fh)
        del blob["version"]
        for t in blob["tracks"]:
            t.pop("duration_ms", None)
            t.pop("year", None)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(blob, fh)


def _args(names=(), **over):
    base = dict(names=list(names), file=None, top=3, missing=False, json=False)
    base.update(over)
    return argparse.Namespace(**base)


# ---------------------------------------------------------------- matching

def test_lists_hits_grouped_by_artist_with_source_pools(capsys):
    _pool("p1", [_track("House Of Jealous Lovers", "The Rapture", pop=50)])
    _pool("p2", [_track("All My Friends", "LCD Soundsystem", pop=69)])

    mh.cmd_find_artists(_args(["LCD Soundsystem", "The Rapture"]))
    out, err = capsys.readouterr()

    assert "LCD Soundsystem — 1 track(s), 1 pool(s)" in out
    assert "The Rapture — 1 track(s), 1 pool(s)" in out
    assert "All My Friends" in out and "2000s Albums" in out
    assert "House Of Jealous Lovers" in out and "Albums - Dance-Punk I" in out
    assert "2/2 name(s) found across 2 cached pool(s)" in err


@pytest.mark.parametrize("query, expect", [
    ("!!!", "Must Be The Moon"),     # punctuation name
    ("CSS", "Hot Hot Sex"),          # acronym name
    ("css", "Hot Hot Sex"),          # query case ignored
    ("OUTKAST", "Jazzy Belle"),      # cached-artist case ignored
    ("outkas", "Jazzy Belle"),       # partial substring
])
def test_matching_is_case_insensitive_substring(query, expect, capsys):
    """Substring-on-the-cache is the point: punctuation/acronym names like '!!!'
    and 'CSS' resolve badly through Spotify's artist search — '!!!' returns R.E.M.
    and 'CSS' returns RAC."""
    _pool("p1", [_track("Must Be The Moon", "!!!"),
                 _track("Hot Hot Sex", "CSS"),
                 _track("Jazzy Belle", "Outkast")])

    mh.cmd_find_artists(_args([query], json=True))
    (row,) = json.loads(capsys.readouterr().out)
    assert row["hits"] == 1
    assert row["tracks"][0]["name"] == expect


def test_a_query_matching_nothing_scores_zero_hits(capsys):
    _pool("p1", [_track("Must Be The Moon", "!!!")])
    mh.cmd_find_artists(_args(["R.E.M."], json=True))
    (row,) = json.loads(capsys.readouterr().out)
    assert row["hits"] == 0 and row["tracks"] == []


def test_substring_matches_a_featured_credit(capsys):
    _pool("p1", [_track("Dynamite", "The Roots, Rehani Sayed")])
    mh.cmd_find_artists(_args(["Rehani"], json=True))
    (row,) = json.loads(capsys.readouterr().out)
    assert row["hits"] == 1
    assert row["tracks"][0]["artist"] == "The Roots, Rehani Sayed"


def test_ranks_by_popularity_and_honours_top(capsys):
    _pool("p1", [_track("Low", "Band", pop=10),
                 _track("High", "Band", pop=90),
                 _track("Mid", "Band", pop=50)])

    mh.cmd_find_artists(_args(["Band"], top=2, json=True))
    (row,) = json.loads(capsys.readouterr().out)
    assert row["hits"] == 3                      # full count reported...
    assert [t["name"] for t in row["tracks"]] == ["High", "Mid"]  # ...top 2 shown


def test_top_zero_returns_every_hit(capsys):
    _pool("p1", [_track(f"S{i}", "Band", pop=i) for i in range(5)])
    mh.cmd_find_artists(_args(["Band"], top=0, json=True))
    (row,) = json.loads(capsys.readouterr().out)
    assert len(row["tracks"]) == 5


def test_a_track_in_several_pools_is_deduped_but_keeps_both_pools(capsys):
    shared = _track("All My Friends", "LCD Soundsystem", pop=69)
    _pool("p1", [shared])
    _pool("p2", [dict(shared)])

    mh.cmd_find_artists(_args(["LCD Soundsystem"], json=True))
    (row,) = json.loads(capsys.readouterr().out)
    assert row["hits"] == 1
    assert sorted(row["tracks"][0]["pools"]) == ["2000s Albums", "Albums - Dance-Punk I"]


def test_duplicate_query_names_are_collapsed(capsys):
    _pool("p1", [_track("Song", "Band")])
    mh.cmd_find_artists(_args(["Band", "Band"], json=True))
    assert len(json.loads(capsys.readouterr().out)) == 1


# ---------------------------------------------------------------- --missing

def test_missing_lists_only_the_absent_names(capsys):
    _pool("p1", [_track("Song", "The Rapture")])

    mh.cmd_find_artists(_args(["The Rapture", "Nonexistent Band"], missing=True))
    out, err = capsys.readouterr()

    assert out.strip() == "Nonexistent Band"
    assert "1/2 name(s) absent" in err


def test_missing_json_is_a_plain_list(capsys):
    _pool("p1", [_track("Song", "The Rapture")])
    mh.cmd_find_artists(_args(["The Rapture", "Nope"], missing=True, json=True))
    assert json.loads(capsys.readouterr().out) == ["Nope"]


def test_missing_names_are_also_reported_on_a_normal_run(capsys):
    _pool("p1", [_track("Song", "The Rapture")])
    mh.cmd_find_artists(_args(["The Rapture", "Nope"]))
    out, err = capsys.readouterr()
    assert "Nope — no hits" in out
    assert "# missing: Nope" in err


# ---------------------------------------------------------------- --file

def test_file_accepts_one_name_per_line_and_skips_comments(tmp_path, capsys):
    _pool("p1", [_track("A", "The Rapture"), _track("B", "LCD Soundsystem")])
    roster = tmp_path / "canon.txt"
    roster.write_text("# dance-punk canon\nThe Rapture\n\nLCD Soundsystem\n",
                      encoding="utf-8")

    mh.cmd_find_artists(_args(file=str(roster), json=True))
    rows = json.loads(capsys.readouterr().out)
    assert [r["name"] for r in rows] == ["The Rapture", "LCD Soundsystem"]


def test_file_and_positional_names_combine(tmp_path, capsys):
    _pool("p1", [_track("A", "The Rapture"), _track("B", "CSS")])
    roster = tmp_path / "r.txt"
    roster.write_text("CSS\n", encoding="utf-8")

    mh.cmd_find_artists(_args(["The Rapture"], file=str(roster), json=True))
    rows = json.loads(capsys.readouterr().out)
    assert [r["name"] for r in rows] == ["The Rapture", "CSS"]


def test_no_names_at_all_errors():
    with pytest.raises(SystemExit) as exc:
        mh.cmd_find_artists(_args())
    assert "--file" in str(exc.value)


# ---------------------------------------------------------------- edges

def test_empty_cache_dir_reports_nil_without_crashing(capsys):
    mh.cmd_find_artists(_args(["Anyone"]))
    out, err = capsys.readouterr()
    assert "Anyone — no hits" in out
    assert "0 cached pool(s)" in err


def test_unreadable_pool_file_is_skipped(capsys):
    _pool("p1", [_track("Song", "The Rapture")])
    import os
    with open(os.path.join(mh.MIX_CACHE_DIR, "broken.json"), "w", encoding="utf-8") as fh:
        fh.write("{not json")

    mh.cmd_find_artists(_args(["The Rapture"], json=True))
    (row,) = json.loads(capsys.readouterr().out)
    assert row["hits"] == 1


def test_pool_with_no_cached_name_falls_back_to_its_id(capsys):
    _pool("unknown-pid", [_track("Song", "The Rapture")])
    mh.cmd_find_artists(_args(["The Rapture"], json=True))
    (row,) = json.loads(capsys.readouterr().out)
    assert row["tracks"][0]["pools"] == ["unknown-pid"]


def test_stale_schema_pools_are_searched_and_flagged(capsys):
    """A pre-v2 blob is read as-is rather than re-pulled (that would need the API),
    so its rows have no runtime/year — say so instead of printing a fake 0:00."""
    _pool("p1", [_track("Song", "The Rapture")], version=1)

    mh.cmd_find_artists(_args(["The Rapture"]))
    out, err = capsys.readouterr()

    assert "Song — The Rapture" in out       # still found
    assert "0:00" not in out and "—  spotify:track:" in out
    assert "1 pool(s) still on an older cache schema" in err


def test_fresh_schema_pools_show_runtime_and_year(capsys):
    _pool("p1", [_track("Song", "The Rapture")])
    mh.cmd_find_artists(_args(["The Rapture"]))
    out, err = capsys.readouterr()
    assert "3:30" in out and "2004" in out
    assert "older cache schema" not in err
