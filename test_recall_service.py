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


def _weeks(n, plays_by_week):
    """n chart weeks, oldest first; plays_by_week(i) -> {artist: plays}."""
    return [{"from": i, "to": i + 1, "artists": plays_by_week(i)} for i in range(n)]


def _listening():
    def week(i):
        w = {"Remember Sports": 4, "Lime Garden": 2}
        if i >= 9:                       # last 4 weeks
            w["Hop Along"] = 5
            w["Wednesday"] = 3
        if i % 4 == 0:
            w["Waxahatchee"] = 2          # weeks 0,4,8,12 → 4 of 13, only 1 recent
        if i == 12:
            w["Binge Band"] = 250         # one huge week — must not qualify
        return w
    return rs.Listening(
        weeks=_weeks(13, week),
        top_12m=[{"artist": "Remember Sports", "playcount": 80}, {"artist": "Hop Along", "playcount": 40}],
        top_overall=[{"artist": "Modest Mouse", "playcount": 900}, {"artist": "Remember Sports", "playcount": 120}],
        loved=[{"artist": "Lime Garden", "title": "Clockwork"}],
        top_tracks=[{"artist": "Hop Along", "title": "Tibetan Pop Stars", "playcount": 31}],
    )


def _playlists():
    return rs.Playlists(pools={
        "p1": {"name": "Indie SELECTS", "artists": ["Remember Sports", "Lime Garden", "Hop Along"]},
        "p2": {"name": "Chill Indie", "artists": ["Remember Sports, Lime Garden", "Waxahatchee"]},
        "p3": {"name": "Road Trip", "artists": ["Remember Sports", "Lime Garden", "Modest Mouse"]},
    })


def _set_size(conn, n):
    # bypass the 15..50 bound so fixtures can stay tiny
    conn.execute("INSERT OR REPLACE INTO recall_settings (key, value) VALUES ('set_size', ?)", (str(n),))


# ----------------------------------------------------------------- identity

def test_norm_folds_case_accents_punctuation():
    assert rs.norm("Sigur Rós") == rs.norm("sigur ros")
    assert rs.norm("Mumford & Sons") == rs.norm("mumford and sons")
    assert rs.norm("The Band") != rs.norm("Band")          # identity keeps "the"
    assert rs.loose("The Band") == rs.loose("Band")        # matching doesn't


def test_credited_artists_keeps_comma_names_whole():
    known = {rs.norm(n): n for n in ("Tyler, The Creator", "Kali Uchis", "Remember Sports")}
    got = rs.credited_artists("Tyler, The Creator, Kali Uchis", known)
    assert got == {rs.norm("Tyler, The Creator"), rs.norm("Kali Uchis")}


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


# ----------------------------------------------------------------- candidates + refresh

def test_candidates_need_spread_and_carry_real_counts():
    cfg = {k: v[0] for k, v in rs.SETTINGS.items()}
    c = rs.compute_candidates(_listening(), _playlists(), cfg)
    assert "binge band" not in c                           # one intense week ≠ familiarity
    assert c["hop along"]["bucket"] == "recent"
    assert c["hop along"]["counts"]["weeks_recent"] == 4
    assert any("4 of the last 4 weeks (20 plays)" in r for r in c["hop along"]["reasons"])
    assert c["waxahatchee"]["bucket"] == "developing"
    assert c["modest mouse"]["bucket"] == "anchor"
    assert c["remember sports"]["playlists"] == ["Chill Indie", "Indie SELECTS", "Road Trip"]
    assert "Tibetan Pop Stars" in c["hop along"]["hook_suggested"]


def test_offline_refresh_uses_playlists_and_explains(conn):
    rep = rs.refresh(conn, None, _playlists(), T0, listening_note="Last.fm unavailable: no key")
    assert rep["listening"] == {"unavailable": "Last.fm unavailable: no key"}
    names = {a["name"] for a in rs.list_active(conn, T0)}
    assert names == {"Remember Sports", "Lime Garden"}      # the two in 3+ own playlists

    empty = sqlite3.connect(":memory:")
    empty.row_factory = sqlite3.Row
    rs.ensure_schema(empty)
    rep = rs.refresh(empty, None, rs.Playlists(note="no playlist list cached"), T0, listening_note="no key")
    assert rep["candidates"] == 0 and "no key" in rep["explain"] and "no playlist" in rep["explain"]


def test_refresh_is_idempotent_and_preserves_user_choices(conn):
    _set_size(conn, 4)
    rs.refresh(conn, _listening(), _playlists(), T0)
    rs.set_hook(conn, "Hop Along", "the voice that cracks on purpose", T0)
    rs.set_choice(conn, "Modest Mouse", "nope", T0)
    rs.set_choice(conn, "Waxahatchee", "snooze", T0, snooze_days=10)
    rs.link(conn, "Remember Sports", "Hop Along", "brewery", T0)
    counts = lambda: [conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                      for t in ("recall_artist", "recall_link", "recall_active")]
    before = counts()
    rs.refresh(conn, _listening(), _playlists(), T0 + timedelta(hours=1))
    assert counts() == before
    hop = rs.artist_detail(conn, "Hop Along", T0)
    assert hop["hook"] == "the voice that cracks on purpose"
    assert rs.artist_detail(conn, "Modest Mouse", T0)["choice"] == "nope"
    assert rs.artist_detail(conn, "Waxahatchee", T0)["snoozed_until"]
    active = {a["name"] for a in rs.list_active(conn, T0)}
    assert "Modest Mouse" not in active and "Waxahatchee" not in active
    link = conn.execute("SELECT l.origin, l.state, l.reason FROM recall_link l JOIN recall_artist d ON d.id=l.dst_id"
                        " WHERE d.name='Hop Along'").fetchone()
    assert tuple(link) == ("user", "accepted", "brewery")


def test_rejected_suggestions_stay_rejected(conn):
    _set_size(conn, 4)
    rs.refresh(conn, _listening(), _playlists(), T0)
    prop = [p for p in rs.proposed_links(conn) if p["origin"] == "data"]
    assert prop, "shared playlists should propose at least one neighbor"
    rs.set_link_state_by_id(conn, prop[0]["link_id"], "rejected", T0)
    rs.refresh(conn, _listening(), _playlists(), T0 + timedelta(days=1))
    row = conn.execute("SELECT state FROM recall_link WHERE id=?", (prop[0]["link_id"],)).fetchone()
    assert row["state"] == "rejected"


def test_data_link_reason_names_the_shared_playlists(conn):
    _set_size(conn, 4)
    rs.refresh(conn, _listening(), _playlists(), T0)
    d = [p for p in rs.proposed_links(conn) if p["origin"] == "data"
         and {p["src"], p["dst"]} == {"Remember Sports", "Lime Garden"}]
    assert d and "3 of your playlists" in d[0]["reason"]


def test_inferred_links_are_labeled_and_limited_to_known_artists(conn):
    _set_size(conn, 4)
    sim = lambda name: [{"name": "Some Stranger", "match": 0.9}, {"name": "Wednesday", "match": 0.5}]
    rs.refresh(conn, _listening(), _playlists(), T0, similar_fn=sim)
    inferred = [p for p in rs.proposed_links(conn) if p["origin"] == "inferred"]
    assert inferred and all(p["dst"] != "Some Stranger" for p in inferred)
    assert all("suggestion" in p["reason"] for p in inferred)


def test_pins_survive_rotation_and_others_rotate_out(conn):
    _set_size(conn, 4)
    rs.refresh(conn, _listening(), _playlists(), T0)
    rs.set_choice(conn, "Lime Garden", "pinned", T0)
    later = T0 + timedelta(days=60)
    rep = rs.refresh(conn, _listening(), _playlists(), later)
    assert "Lime Garden" in {a["name"] for a in rs.list_active(conn, later)}
    assert rep["active"]["retired"], "aged, unpinned members should rotate"


# ----------------------------------------------------------------- the whole loop

def test_settings_are_bounded(conn):
    assert rs.set_setting(conn, "set_size", "20") == 20
    with pytest.raises(rs.RecallError):
        rs.set_setting(conn, "set_size", "500")
    assert rs.set_setting(conn, "ladder", "2,5,10") == [2, 5, 10]
    with pytest.raises(rs.RecallError):
        rs.set_setting(conn, "ladder", "soon")


def test_household_names_skipped_unless_pinned(conn):
    _set_size(conn, 4)
    fame = {"Modest Mouse": 3_000_000}
    rep = rs.refresh(conn, _listening(), _playlists(), T0, listeners_fn=lambda n: fame.get(n, 50_000))
    active = {a["name"] for a in rs.list_active(conn, T0)}
    assert "Modest Mouse" not in active and "Modest Mouse" in rep["active"]["skipped_famous"]
    rs.set_choice(conn, "Modest Mouse", "pinned", T0)
    assert "Modest Mouse" in {a["name"] for a in rs.list_active(conn, T0)}


def test_rotated_out_artists_sit_out_before_returning(conn):
    _set_size(conn, 4)
    rs.refresh(conn, _listening(), _playlists(), T0)
    rep = rs.refresh(conn, _listening(), _playlists(), T0 + timedelta(days=60))
    out = set(rep["active"]["retired"])
    assert out and not out & set(rep["active"]["added"])
    rep2 = rs.refresh(conn, _listening(), _playlists(), T0 + timedelta(days=61))
    assert not out & set(rep2["active"]["added"])
