"""Tests for roster --mine personal-scrobble annotation in mix_helper (issue #9).

Exercises the two pure seams — _norm_track_key (matching) and _annotate_mine
(annotation) — with injected lists, so no Last.fm key, user or network is touched.
Mirrors test_roster_tags.py's shape.

    PYTHONUTF8=1 python -m pytest .claude/skills/mix/test_roster_mine.py
"""
import importlib

mh = importlib.import_module("mix_helper")


def _rows(*pairs):
    return [{"uri": f"spotify:track:{i}", "name": n, "artist": a}
            for i, (a, n) in enumerate(pairs)]


# ----------------------------------------------------------------- normalization

def test_key_ignores_case_and_accents():
    assert mh._norm_track_key("Bjork", "Joga") == mh._norm_track_key("BJÖRK", "Jóga")


def test_key_strips_parenthetical_and_dash_qualifiers_from_title():
    base = mh._norm_track_key("Radiohead", "Creep")
    assert mh._norm_track_key("Radiohead", "Creep (Remastered 2011)") == base
    assert mh._norm_track_key("Radiohead", "Creep - 2019 Remaster") == base
    assert mh._norm_track_key("Radiohead", "Creep [Live]") == base


def test_key_drops_featured_credits_and_uses_lead_artist_only():
    base = mh._norm_track_key("Emancipator", "Anthem")
    assert mh._norm_track_key("Emancipator, Madelyn Grant", "Anthem") == base
    assert mh._norm_track_key("Emancipator", "Anthem feat. Madelyn Grant") == base


def test_key_normalizes_punctuation_and_whitespace():
    assert mh._norm_track_key("Sigur Rós", "Hoppípolla") == \
           mh._norm_track_key("sigur  ros", "hoppipolla")


def test_key_folds_intraword_marks_away_but_separators_to_spaces():
    """Acronyms and elisions collapse; joined/spaced separators agree."""
    assert mh._norm_track_key("AC/DC", "T.N.T.") == mh._norm_track_key("AC DC", "TNT")
    assert mh._norm_track_key("X", "Don't Stop") == mh._norm_track_key("X", "Dont Stop")
    assert mh._norm_track_key("X", "rock&roll") == mh._norm_track_key("X", "rock & roll")
    # curly and straight apostrophes are the same elision
    assert mh._norm_track_key("X", "Don’t") == mh._norm_track_key("X", "Don't")


def test_key_keeps_genuinely_different_tracks_apart():
    assert mh._norm_track_key("Bonobo", "Kerala") != mh._norm_track_key("Bonobo", "Kiara")
    assert mh._norm_track_key("Bonobo", "Kerala") != mh._norm_track_key("Tycho", "Kerala")


# ------------------------------------------------------------------- annotation

def test_playcount_and_loved_are_both_surfaced():
    rows = _rows(("Bonobo", "Kerala"), ("Tycho", "Awake"))
    top = [{"artist": "Bonobo", "title": "Kerala", "playcount": 42}]
    loved = [{"artist": "Bonobo", "title": "Kerala"}]

    matched, total = mh._annotate_mine(rows, top, loved)
    assert rows[0]["mine"] == {"loved": True, "playcount": 42}
    assert rows[1]["mine"] == {"loved": False, "playcount": None}
    assert (matched, total) == (1, 2)


def test_loved_without_playcount_yields_none_playcount():
    rows = _rows(("Tycho", "Awake"))
    mh._annotate_mine(rows, [], [{"artist": "Tycho", "title": "Awake"}])
    assert rows[0]["mine"] == {"loved": True, "playcount": None}


def test_scrobbled_but_not_loved():
    rows = _rows(("Tycho", "Awake"))
    mh._annotate_mine(rows, [{"artist": "Tycho", "title": "Awake", "playcount": 9}], [])
    assert rows[0]["mine"] == {"loved": False, "playcount": 9}


def test_matching_survives_remaster_and_accent_mismatch():
    rows = _rows(("Sigur Rós", "Hoppípolla (Remastered)"))
    top = [{"artist": "Sigur Ros", "title": "Hoppipolla", "playcount": 7}]
    matched, _ = mh._annotate_mine(rows, top, [])
    assert rows[0]["mine"]["playcount"] == 7
    assert matched == 1


def test_every_row_gets_a_mine_dict_even_with_no_signal():
    rows = _rows(("Nobody", "Nothing"))
    matched, total = mh._annotate_mine(rows, [], [])
    assert rows[0]["mine"] == {"loved": False, "playcount": None}
    assert (matched, total) == (0, 1)


def test_first_playcount_wins_on_duplicate_keys():
    """Last.fm lists are playcount-ordered, so the first entry is the real one."""
    rows = _rows(("Bonobo", "Kerala"))
    top = [{"artist": "Bonobo", "title": "Kerala", "playcount": 42},
           {"artist": "Bonobo", "title": "Kerala (Live)", "playcount": 3}]
    mh._annotate_mine(rows, top, [])
    assert rows[0]["mine"]["playcount"] == 42


def test_zero_playcount_is_not_counted_as_signal():
    rows = _rows(("Ghost", "Track"))
    matched, _ = mh._annotate_mine(rows, [{"artist": "Ghost", "title": "Track",
                                           "playcount": 0}], [])
    assert matched == 0


def test_annotation_never_grows_or_reorders_the_pool():
    """Q5's guarantee is structural: --mine runs after ice/cooldown have filtered, and
    annotation is pure decoration — it can't resurrect a track Last.fm knows but the
    roster already excluded."""
    rows = _rows(("Bonobo", "Kerala"), ("Tycho", "Awake"))
    before = [r["uri"] for r in rows]
    top = [{"artist": "Bonobo", "title": "Kerala", "playcount": 42},
           {"artist": "Iced Artist", "title": "Iced Track", "playcount": 999}]
    loved = [{"artist": "Iced Artist", "title": "Iced Track"}]

    matched, total = mh._annotate_mine(rows, top, loved)
    assert [r["uri"] for r in rows] == before  # same rows, same order
    assert total == 2 and matched == 1
    assert not any("Iced" in r["artist"] for r in rows)


# ----------------------------------------------------------------------- render

def test_label_renders_loved_scrobbled_and_empty():
    assert mh._mine_label({"loved": True, "playcount": 42}) == "♥42"
    assert mh._mine_label({"loved": True, "playcount": None}) == "♥"
    assert mh._mine_label({"loved": False, "playcount": 17}) == "17"
    assert mh._mine_label({"loved": False, "playcount": None}) == "·"
    assert mh._mine_label(None) == "·"
