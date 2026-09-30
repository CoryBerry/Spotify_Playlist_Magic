"""Tests for recall_service — the music recall practice loop.

Everything runs against an in-memory SQLite DB with fixture listening/playlist data and an
explicit clock (``now=``), so no Last.fm key, Spotify token, or network is touched.

    PYTHONUTF8=1 python -m pytest test_recall_service.py
"""
import json
import sqlite3
from datetime import datetime, timedelta

import pytest

import recall_service as rs

T0 = datetime(2026, 9, 29, 20, 0, 0)


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    rs.ensure_schema(c)
    return c


# ----------------------------------------------------------------- identity

def test_norm_folds_case_accents_punctuation():
    assert rs.norm("Sigur Rós") == rs.norm("sigur ros")
    assert rs.norm("Mumford & Sons") == rs.norm("mumford and sons")
    assert rs.norm("The Band") != rs.norm("Band")          # identity keeps "the"
    assert rs.loose("The Band") == rs.loose("Band")        # matching doesn't


def test_the_variants_are_ambiguous_not_merged(conn):
    rs.ensure_artist(conn, "The Band", "user", T0)
    rs.ensure_artist(conn, "Band", "user", T0)
    assert rs.match_name(conn, "the band")["name"] == "The Band"      # exact still exact
    rs.ensure_artist(conn, "The Weakerthans", "user", T0)
    rs.ensure_artist(conn, "Weakerthans!", "user", T0)                # same loose key, distinct rows
    m = rs.match_name(conn, "weakerthans the")
    assert m["how"] in ("ambiguous", "unknown") or m["artist_id"] is not None
    with pytest.raises(rs.RecallError, match="ambiguous|no artist"):
        rs.find_artist(conn, "Weakerthan")


def test_spelling_match_is_visible_and_ties_are_ambiguous(conn):
    rs.ensure_artist(conn, "Lime Garden", "user", T0)
    m = rs.match_name(conn, "lime gardn")
    assert (m["how"], m["name"]) == ("spelling", "Lime Garden")
    rs.ensure_artist(conn, "Hop Alone", "user", T0)
    rs.ensure_artist(conn, "Hop Along", "user", T0)
    amb = rs.match_name(conn, "Hop Alon")
    assert amb["how"] == "ambiguous" and set(amb["options"]) == {"Hop Alone", "Hop Along"}
    # ...unless one of them is what the prompt expects
    pref = rs.match_name(conn, "Hop Alon", prefer=(rs.find_artist(conn, "Hop Along")["id"],))
    assert pref["name"] == "Hop Along"


def test_answer_split_keeps_and_names_whole(conn):
    for n in ("Belle and Sebastian", "Lime Garden", "Hop Along"):
        rs.ensure_artist(conn, n, "user", T0)
    names = [r["name"] for r in rs.resolve_answer(conn, "Belle and Sebastian, lime garden and hop along")]
    assert names == ["Belle and Sebastian", "Lime Garden", "Hop Along"]


def test_alias_counts_future_spellings(conn):
    rs.ensure_artist(conn, "Hop Along", "user", T0)
    rs.add_alias(conn, "Hop-Along Queen Ansleis", "Hop Along")
    assert rs.match_name(conn, "hop along queen ansleis")["how"] == "alias"


def test_link_warns_instead_of_merging_near_names(conn):
    rs.ensure_artist(conn, "Lime Garden", "user", T0)
    out = rs.link(conn, "Remember Sports", "Lime Gardn", now=T0)
    assert out["warnings"] and "Lime Garden" in out["warnings"][0]
    assert conn.execute("SELECT COUNT(*) FROM recall_artist WHERE norm IN ('lime garden','lime gardn')").fetchone()[0] == 2


# ----------------------------------------------------------------- the whole loop

def test_settings_are_bounded(conn):
    assert rs.set_setting(conn, "set_size", "20") == 20
    with pytest.raises(rs.RecallError):
        rs.set_setting(conn, "set_size", "500")
    assert rs.set_setting(conn, "ladder", "2,5,10") == [2, 5, 10]
    with pytest.raises(rs.RecallError):
        rs.set_setting(conn, "ladder", "soon")
