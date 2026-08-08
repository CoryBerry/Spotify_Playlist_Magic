"""Tests for roster --tags annotation in mix_helper (issue #4).

Exercises the pure _annotate_tags seam with an injected lookup, so no Last.fm
key or network is touched: we assert in-run dedupe (one lookup per distinct lead
artist), lead-artist extraction, and the resolved/unknown tally.

    PYTHONUTF8=1 python -m pytest .claude/skills/mix/test_roster_tags.py
"""
import importlib

mh = importlib.import_module("mix_helper")


def _rows(*artists):
    return [{"uri": f"spotify:track:{i}", "name": f"S{i}", "artist": a}
            for i, a in enumerate(artists)]


def test_each_distinct_lead_artist_looked_up_once():
    rows = _rows("Bonobo", "Bonobo", "Tycho")
    calls = []

    def lookup(artist):
        calls.append(artist)
        return {"Bonobo": ["downtempo", "electronic"], "Tycho": ["chillwave"]}[artist]

    resolved, total, unknown = mh._annotate_tags(rows, lookup)
    assert calls == ["Bonobo", "Tycho"]  # deduped — Bonobo fetched once
    assert rows[0]["tags"] == ["downtempo", "electronic"]
    assert rows[1]["tags"] == ["downtempo", "electronic"]  # reused
    assert rows[2]["tags"] == ["chillwave"]
    assert (resolved, total, unknown) == (2, 2, 0)


def test_lead_artist_only_from_comma_joined_field():
    rows = _rows("Emancipator, Madelyn Grant")
    seen = []

    def lookup(artist):
        seen.append(artist)
        return ["downtempo"]

    mh._annotate_tags(rows, lookup)
    assert seen == ["Emancipator"]  # only the lead artist, trimmed
    assert rows[0]["tags"] == ["downtempo"]


def test_unknown_artists_counted_and_yield_empty_tags():
    rows = _rows("Bonobo", "Nobody Knows Me")

    def lookup(artist):
        return ["downtempo"] if artist == "Bonobo" else []

    resolved, total, unknown = mh._annotate_tags(rows, lookup)
    assert rows[1]["tags"] == []
    assert (resolved, total, unknown) == (1, 2, 1)


def test_dedupe_is_case_insensitive():
    rows = _rows("Tycho", "tycho")
    calls = []

    def lookup(artist):
        calls.append(artist)
        return ["chillwave"]

    mh._annotate_tags(rows, lookup)
    assert len(calls) == 1  # "Tycho" and "tycho" collapse to one lookup
    assert rows[0]["tags"] == rows[1]["tags"] == ["chillwave"]
