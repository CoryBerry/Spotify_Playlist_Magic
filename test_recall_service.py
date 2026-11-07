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
    assert any("into them lately: played in 4 of the last 4 weeks (20 plays, new to you)" in r
               for r in c["hop along"]["reasons"])
    assert c["remember sports"]["bucket"] == "developing"  # every week, but no lift: steady, not lately
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


# ----------------------------------------------------------------- starter set

def test_starter_links_are_offered_not_assumed(conn):
    assert rs.seed_starter(conn, T0) == 2
    assert rs.seed_starter(conn, T0) == 0                    # once only
    prop = rs.proposed_links(conn)
    assert {(p["src"], p["dst"]) for p in prop} == {("Remember Sports", "Lime Garden"),
                                                    ("Remember Sports", "Hop Along")}
    ev = conn.execute("SELECT evidence, bucket FROM recall_artist WHERE name='Lime Garden'").fetchone()
    assert ev["evidence"] is None and ev["bucket"] is None   # no fabricated play history
    assert rs.start_session(conn, T0)["session_id"] is None  # nothing practiceable until accepted
    rs.set_link_state(conn, "Remember Sports", "Lime Garden", "rejected", T0)
    rs.seed_starter(conn, T0)
    assert conn.execute("SELECT state FROM recall_link WHERE id=1").fetchone()[0] == "rejected"


# ----------------------------------------------------------------- practice

def _curate(conn):
    rs.seed_starter(conn, T0)
    rs.set_link_state(conn, "Remember Sports", "Lime Garden", "accepted", T0)
    rs.set_link_state(conn, "Remember Sports", "Hop Along", "accepted", T0)
    rs.set_hook(conn, "Lime Garden", "a newer discovery I want to bring up", T0)


def _first_recommend(conn, now):
    s = rs.start_session(conn, now)
    p = rs.next_prompt(conn, s["session_id"], now)
    assert p["kind"] == "recommend"
    return s["session_id"], p


def test_prompt_view_and_hints_never_leak_answers(conn):
    _curate(conn)
    sid, p = _first_recommend(conn, T0)
    blob = json.dumps(p).lower()
    assert "remember sports" in blob
    assert "lime garden" not in blob and "hop along" not in blob
    h1 = rs.hint(conn, p["attempt_id"])
    assert h1["hint_level"] == 1 and "lime garden" not in h1["hint"].lower()
    assert "newer discovery" in h1["hint"]
    h2 = rs.hint(conn, p["attempt_id"])
    assert h2["hint_level"] == 2 and h2["hint"] == "Starts with: L…, H…"


def test_hint_skips_playlist_names_that_spell_the_answer(conn):
    rs.link(conn, "Remember Sports", "Hop Along", now=T0)
    conn.execute("UPDATE recall_artist SET evidence=? WHERE name='Hop Along'",
                 (json.dumps({"playlists": ["Albums - Hop Along", "Indie SELECTS"]}),))
    sid, p = _first_recommend(conn, T0)
    assert rs.hint(conn, p["attempt_id"])["hint"] == "One of them is in your playlist “Indie SELECTS”"


def test_submit_resolves_alternatives_spelling_and_unknowns(conn):
    _curate(conn)
    rs.ensure_artist(conn, "Wednesday", "user", T0)
    sid, p = _first_recommend(conn, T0)
    fb = rs.submit(conn, p["attempt_id"], "lime gardn, Wednesday, Totally New Band", T0 + timedelta(seconds=40))
    by = {r["text"]: r for r in fb["resolved"]}
    assert by["lime gardn"]["how"] == "spelling" and by["lime gardn"]["expected"]
    assert by["Wednesday"]["how"] == "exact" and not by["Wednesday"]["expected"]   # valid alt, not "wrong"
    assert by["Totally New Band"]["how"] == "unknown"
    assert [e["hit"] for e in fb["expected"]] == [True, False]
    assert fb["sentence"] == "You like Remember Sports? Have you heard Lime Garden or Hop Along?"
    # Cory endorses his alternative → it becomes an accepted answer next time
    rs.link(conn, "Remember Sports", "Wednesday", "came to mind in practice", T0)
    _, expected = rs._build_prompt(conn, conn.execute("SELECT * FROM recall_item WHERE kind='recommend'").fetchone(), T0)
    assert "Wednesday" in {e["name"] for e in expected}


def test_schedule_ladder_hints_and_misses():
    ladder = [3, 7, 14, 30]
    item = {"step": 0, "lapses": 0}
    s = rs.schedule(item, "easy", 0, False, ladder, 1, T0)
    assert (s["interval_days"], s["step"]) == (3, 1)
    s2 = rs.schedule({"step": 1, "lapses": 0}, "easy", 0, False, ladder, 1, T0)
    assert s2["interval_days"] == 7
    assert rs.schedule({"step": 1, "lapses": 0}, "moment", 0, False, ladder, 1, T0)["step"] == 1
    hinted = rs.schedule({"step": 2, "lapses": 0}, "easy", 1, False, ladder, 1, T0)
    assert hinted["interval_days"] == 7 and hinted["step"] == 2          # shorter, no promotion
    miss = rs.schedule({"step": 3, "lapses": 0}, "couldnt", 0, False, ladder, 1, T0)
    assert (miss["interval_days"], miss["step"], miss["lapses"]) == (1, 0, 1)
    early = rs.schedule({"step": 3, "lapses": 0}, "easy", 0, True, ladder, 1, T0)
    assert early["interval_days"] == 1                                   # revealed before answering
    top = rs.schedule({"step": 3, "lapses": 0}, "easy", 0, False, ladder, 1, T0)
    assert (top["interval_days"], top["step"]) == (30, 3)


def test_rating_requires_an_attempt_and_happens_once(conn):
    _curate(conn)
    sid, p = _first_recommend(conn, T0)
    with pytest.raises(rs.RecallError, match="before rating"):
        rs.rate(conn, p["attempt_id"], "easy", T0)
    rs.submit(conn, p["attempt_id"], "Lime Garden", T0)
    rs.rate(conn, p["attempt_id"], "easy", T0)
    with pytest.raises(rs.RecallError, match="already rated"):
        rs.rate(conn, p["attempt_id"], "easy", T0)
    with pytest.raises(rs.RecallError):
        rs.rate(conn, p["attempt_id"], "great", T0)


def test_interrupted_session_keeps_work_and_resumes(conn):
    _curate(conn)
    rs.tag_context(conn, "Lime Garden", "new discoveries", now=T0)
    s = rs.start_session(conn, T0)
    assert len(s["queue"]) == 1                    # new_per_session=1: new material in small doses
    p = rs.next_prompt(conn, s["session_id"], T0)
    rs.submit(conn, p["attempt_id"], "Hop Along", T0)
    # walk away before rating — the answer is saved, the schedule isn't touched
    again = rs.next_prompt(conn, s["session_id"], T0 + timedelta(minutes=5))
    assert again["attempt_id"] == p["attempt_id"] and again["answered"]
    recap = rs.end_session(conn, s["session_id"], T0 + timedelta(minutes=6))
    assert recap["prompts"] == 1 and recap["rated"] == 0
    item = conn.execute("SELECT due_at, reps FROM recall_item WHERE kind='recommend'").fetchone()
    assert item["due_at"] is None and item["reps"] == 0
    assert rs.next_prompt(conn, s["session_id"], T0) is None           # ended
    s2 = rs.start_session(conn, T0 + timedelta(minutes=10))
    assert s2["session_id"] and s2["queue"]                            # still practiceable


def test_nothing_due_offers_fresh(conn):
    _curate(conn)
    sid, p = _first_recommend(conn, T0)
    rs.submit(conn, p["attempt_id"], "Lime Garden, Hop Along", T0)
    rs.rate(conn, p["attempt_id"], "easy", T0)
    conn.execute("UPDATE recall_item SET due_at=? WHERE due_at IS NULL", (rs._ts(T0 + timedelta(days=5)),))
    conn.commit()
    plan = rs.start_session(conn, T0 + timedelta(hours=1))
    assert plan["session_id"] is None and plan["nothing_due"] and plan["next_due"]
    fresh = rs.start_session(conn, T0 + timedelta(hours=1), fresh=True)
    assert fresh["session_id"] and fresh["queue"]


def test_all_snoozed_hides_hook_prompts(conn):
    rs.set_hook(conn, "Lime Garden", "the one with the drum machine", T0)
    rs.set_choice(conn, "Lime Garden", "snooze", T0, snooze_days=7)
    assert rs.start_session(conn, T0)["session_id"] is None
    assert rs.start_session(conn, T0 + timedelta(days=8))["session_id"]


def test_hook_that_names_the_artist_is_not_a_prompt(conn):
    out = rs.set_hook(conn, "Hop Along", "Hop Along's singer screams beautifully", T0)
    assert out["leaks_name"]
    rs.sync_items(conn, T0)
    assert not conn.execute("SELECT 1 FROM recall_item WHERE kind='hook' AND live=1").fetchone()


# ----------------------------------------------------------------- the whole loop

def test_curate_practice_persist_reschedule_loop(conn):
    _set_size(conn, 4)
    rs.refresh(conn, _listening(), _playlists(), T0)
    rs.set_link_state(conn, "Remember Sports", "Lime Garden", "accepted", T0)
    rs.link(conn, "Remember Sports", "Hop Along", "wanted to mention them at the brewery", T0)

    # Day 0: recommend prompt is new → practiced, easy, unassisted → due in 3 days
    s = rs.start_session(conn, T0)
    p = rs.next_prompt(conn, s["session_id"], T0)
    assert p["prompt"] == "Someone says they like Remember Sports. Name two artists you'd recommend."
    fb = rs.submit(conn, p["attempt_id"], "Lime Garden and Hop Along", T0 + timedelta(seconds=20))
    assert all(e["hit"] for e in fb["expected"])
    r = rs.rate(conn, p["attempt_id"], "easy", T0 + timedelta(seconds=25))
    assert r["interval_days"] == 3
    recap = rs.end_session(conn, s["session_id"], T0 + timedelta(minutes=1))
    assert recap["recalled"] == ["Lime Garden", "Hop Along"]
    assert recap["sentence"].startswith("You like Remember Sports?")

    # Day 1: recommend isn't due; the new 'lately' item is offered instead
    d1 = T0 + timedelta(days=1)
    s1 = rs.start_session(conn, d1)
    kinds = [conn.execute("SELECT kind FROM recall_item WHERE id=?", (i,)).fetchone()[0] for i in s1["queue"]]
    assert "recommend" not in kinds and "lately" in kinds
    p1 = rs.next_prompt(conn, s1["session_id"], d1)
    rs.reveal(conn, p1["attempt_id"], d1)                     # blanked — reveal first
    rs.rate(conn, p1["attempt_id"], "couldnt", d1)
    rs.end_session(conn, s1["session_id"], d1)

    # Day 3: both are due — the missed one (due day 2) comes first
    d3 = T0 + timedelta(days=3, hours=1)
    s3 = rs.start_session(conn, d3)
    kinds = [conn.execute("SELECT kind FROM recall_item WHERE id=?", (i,)).fetchone()[0] for i in s3["queue"]]
    assert kinds == ["lately", "recommend"]

    st = rs.stats(conn, d3)
    assert dict(st["unassisted_recalls"]) == {"Lime Garden": 1, "Hop Along": 1}
    assert st["difficult"] and st["difficult"][0]["prompt"].startswith("Name three artists")
    assert st["due_now"] == 2 and st["recommendations"] == 2


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


# ----------------------------------------------------------------- tuning (#32)

def test_recent_means_into_them_lately_not_just_played():
    cfg = {k: v[0] for k, v in rs.SETTINGS.items()}

    def week(i):
        w = {"Steady": 6, "Picked Up": 1 if i < 9 else 6}
        if i >= 9:
            w["Background"] = 1                              # every recent week, but 4 plays total
        if i in (10, 12):
            w["Two Weeks"] = 8                               # plenty of plays, only 2 of 4 weeks
        return w
    c = rs.compute_candidates(rs.Listening(weeks=_weeks(13, week)), rs.Playlists(), cfg)
    assert c["picked up"]["bucket"] == "recent"
    assert any("6.0× your usual rate" in r for r in c["picked up"]["reasons"])
    assert c["steady"]["bucket"] == "developing"
    assert c.get("background", {}).get("bucket") != "recent"
    assert c.get("two weeks", {}).get("bucket") != "recent"

    cfg["recent_lift_pct"] = 100                             # lift off → steady listening counts
    c = rs.compute_candidates(rs.Listening(weeks=_weeks(13, week)), rs.Playlists(), cfg)
    assert c["steady"]["bucket"] == "recent"


def test_year_end_and_decade_pools_are_weak_link_evidence(conn):
    _set_size(conn, 4)
    annual = lambda i: {"name": f"20{i}: My Spotify Top 100", "tags": ["annual"],
                        "artists": ["Remember Sports", "Hop Along"]}
    pls = rs.Playlists(pools={**_playlists().pools, **{f"y{i}": annual(i) for i in range(17, 20)}})
    rs.refresh(conn, _listening(), pls, T0)
    pairs = {frozenset((p["src"], p["dst"])): p for p in rs.proposed_links(conn) if p["origin"] == "data"}
    # 1 real shared playlist + 3 year-end lists (3 × 0.25) — unweighted that's 4 shared; weighted
    # it's under the bar of two real ones
    assert frozenset(("Remember Sports", "Hop Along")) not in pairs
    rs_lg = pairs[frozenset(("Remember Sports", "Lime Garden"))]
    assert "3 of your playlists" in rs_lg["reason"] and "year/decade" not in rs_lg["reason"]

    pls.pools["p3"]["artists"].append("Hop Along")          # a second real shared playlist
    rs.refresh(conn, _listening(), pls, T0 + timedelta(hours=1))
    pairs = {frozenset((p["src"], p["dst"])): p for p in rs.proposed_links(conn) if p["origin"] == "data"}
    hop = pairs[frozenset(("Remember Sports", "Hop Along"))]
    assert "2 of your playlists" in hop["reason"] and "also 3 year/decade lists (weak evidence)" in hop["reason"]


def test_unsupported_data_proposals_are_withdrawn_but_decisions_stay(conn):
    _set_size(conn, 4)
    rs.refresh(conn, _listening(), _playlists(), T0)
    data = lambda: [p for p in rs.proposed_links(conn) if p["origin"] == "data"]  # noqa: E731
    only_p1 = rs.Playlists(pools={"p1": _playlists().pools["p1"]})
    assert data()
    rs.refresh(conn, _listening(), only_p1, T0 + timedelta(hours=1))
    assert not data()                                        # evidence gone → proposal withdrawn

    rs.refresh(conn, _listening(), _playlists(), T0 + timedelta(hours=2))
    link_id = data()[0]["link_id"]
    rs.set_link_state_by_id(conn, link_id, "rejected", T0)
    rs.refresh(conn, _listening(), only_p1, T0 + timedelta(hours=3))
    assert conn.execute("SELECT state FROM recall_link WHERE id=?", (link_id,)).fetchone()[0] == "rejected"


def test_nomix_pools_are_not_link_or_familiarity_evidence(conn, tmp_path):
    conn.executescript("""
        CREATE TABLE playlist_cache (data TEXT);
        CREATE TABLE playlist_tag (playlist_id TEXT, tag TEXT);
        CREATE TABLE created_playlist (playlist_id TEXT);
    """)
    me = {"id": "cory"}
    conn.execute("INSERT INTO playlist_cache VALUES (?)", (json.dumps(
        [{"id": "a", "name": "Indie SELECTS", "owner": me},
         {"id": "b", "name": "Top Tracks (Last 30 days)", "owner": me}]),))
    conn.executemany("INSERT INTO playlist_tag VALUES (?, ?)", [("a", "selects"), ("b", "nomix")])
    for pid in "ab":
        (tmp_path / f"{pid}.json").write_text(json.dumps({"tracks": [{"artist": "Hop Along"}]}))
    pls = rs.load_playlists(conn, str(tmp_path))
    assert list(pls.pools) == ["a"] and pls.pools["a"]["tags"] == ["selects"]


def test_listener_lookup_never_trusts_an_autocorrect_to_another_act():
    lastfm = {("Ratboys", False): {"name": "Ratboys", "listeners": 200621},
              ("Ratboys", True): {"name": "Ratboy", "listeners": 2788},
              ("Rat Boys", False): None,
              ("Rat Boys", True): {"name": "Ratboy", "listeners": 2788},
              ("the national", False): None,
              ("the national", True): {"name": "The National", "listeners": 3000000}}
    info = lambda name, autocorrect: lastfm[(name, autocorrect)]
    assert rs.lookup_listeners("Ratboys", info) == 200621
    assert rs.lookup_listeners("Rat Boys", info) is None         # corrected to someone else
    assert rs.lookup_listeners("the national", info) == 3000000  # same name, just cased


def test_stale_listener_counts_are_refetched_and_rechecked(conn):
    _set_size(conn, 4)
    assert rs.refresh_will_be_slow(conn)                     # empty set: first refresh is the slow one
    rs.refresh(conn, _listening(), _playlists(), T0)
    assert not rs.refresh_will_be_slow(conn)
    members = {a["name"] for a in rs.list_active(conn, T0)}
    conn.execute("UPDATE recall_artist SET listeners=2788")  # counts from the old autocorrect lookup
    conn.execute("UPDATE recall_settings SET value='1' WHERE key='listeners_rev'")
    victim = sorted(members)[0]
    fetched = {}

    def listeners(name):
        fetched[name] = 5_000_000 if name == victim else 5000
        return fetched[name]
    rep = rs.refresh(conn, _listening(), _playlists(), T0 + timedelta(hours=1), listeners_fn=listeners)
    assert members <= set(fetched)                           # every member re-checked
    assert victim in rep["active"]["retired"] and victim in rep["active"]["skipped_famous"]
    assert not conn.execute("SELECT 1 FROM recall_artist WHERE listeners=2788").fetchone()

    fetched.clear()                                          # next refresh reuses the fresh counts
    rs.refresh(conn, _listening(), _playlists(), T0 + timedelta(hours=2), listeners_fn=listeners)
    assert not set(fetched) & (members - {victim})
