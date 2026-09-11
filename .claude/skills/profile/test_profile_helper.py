"""Tests for the pure helpers in profile_helper.

Covers the two things most likely to silently corrupt a profile:
local-file URI recovery (whole playlists look empty without it) and the
artist guard on resolve-local (Spotify's loose search returns confident
nonsense when a query has no good match).

    PYTHONUTF8=1 python -m pytest .claude/skills/profile/test_profile_helper.py
"""
import importlib

ph = importlib.import_module("profile_helper")


# ---------------------------------------------------------------- local URIs

def test_decode_local_basic():
    artist, album, title = ph._decode_local(
        "spotify:local:The+Beatles:Abbey+Road:I+Want+You:467")
    assert (artist, album, title) == ("The Beatles", "Abbey Road", "I Want You")


def test_decode_local_percent_escapes():
    """Accents and apostrophes arrive percent-encoded on top of '+' for spaces."""
    artist, _, title = ph._decode_local(
        "spotify:local:Sigur+R%C3%B3s:%C3%81g%C3%A6tis+byrjun:Star%C3%A1lfur:400")
    assert artist == "Sigur Rós"
    assert title == "Starálfur"


def test_decode_local_missing_trailing_fields():
    """Some rows carry only an artist; decoding must not raise."""
    assert ph._decode_local("spotify:local:Lincoln") == ("Lincoln", "", "")


# ---------------------------------------------------------------- artist guard

def _track(*artists, name="song"):
    return {"name": name, "artists": [{"name": a} for a in artists]}


def test_first_artist_match_accepts_same_artist():
    res = [_track("Blind Pilot", name="One Red Thread")]
    assert ph._first_artist_match(res, "Blind Pilot") is res[0]


def test_first_artist_match_rejects_unrelated_artist():
    """The real regression: a loose search returned Ravyn Lenae for Blind Pilot."""
    res = [_track("Ravyn Lenae", name="Love Me Not")]
    assert ph._first_artist_match(res, "Blind Pilot") is None


def test_first_artist_match_skips_to_the_right_result():
    res = [_track("Someone Else"), _track("Bon Iver", name="Blood Bank")]
    assert ph._first_artist_match(res, "Bon Iver") is res[1]


def test_first_artist_match_ignores_leading_the_and_case():
    res = [_track("shins")]
    assert ph._first_artist_match(res, "The Shins") is res[0]


def test_first_artist_match_matches_featured_artist():
    res = [_track("Calexico", "Iron & Wine")]
    assert ph._first_artist_match(res, "Iron and Wine") is res[0]


def test_first_artist_match_no_results():
    assert ph._first_artist_match([], "Anyone") is None


# ---------------------------------------------------------------- pools

def _blob(*pls):
    return {"playlists": list(pls)}


def test_pools_excludes_followed_by_default():
    blob = _blob(
        {"name": "mine", "owned": True, "tracks": [1]},
        {"name": "theirs", "owned": False, "tracks": [1]},
    )
    assert [p["name"] for p in ph._pools(blob)] == ["mine"]
    assert len(ph._pools(blob, include_followed=True)) == 2


def test_pools_skips_empty_playlists():
    """An unreadable Blend leaves tracks=[]; it must not reach the counters."""
    blob = _blob({"name": "blend", "owned": False, "tracks": [], "error": "algorithmic"})
    assert ph._pools(blob, include_followed=True) == []


def test_artist_counter_counts_every_credited_artist():
    pools = [{"tracks": [
        {"artists": ["A", "B"]},
        {"artists": ["A"]},
        {"artists": []},
    ]}]
    c = ph._artist_counter(pools)
    assert c["A"] == 2 and c["B"] == 1
