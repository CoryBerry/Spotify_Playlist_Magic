"""Tests for roster's candidate filters — era/explicit (issue #12) and the
popularity band (issue #14).

The filters are pure functions over a cached track dict, so these run with no DB,
no Spotify client and no disk: build a track, build an args namespace, assert.

    PYTHONUTF8=1 python -m pytest .claude/skills/mix/test_roster_filters.py
"""
import argparse
import importlib

import pytest

mh = importlib.import_module("mix_helper")


def _args(**over):
    """A roster args namespace with every filter off, overridden per test."""
    base = dict(year_min=0, year_max=0, no_explicit=False, pop_min=None, pop_max=None)
    base.update(over)
    return argparse.Namespace(**base)


def _track(pop=50, year=1994, explicit=False):
    t = {"uri": "spotify:track:x", "name": "Song", "artist": "Artist",
         "album_id": "a", "album": "Album", "pop": pop, "duration_ms": 210_000,
         "explicit": explicit}
    if year is not None:
        t["year"] = year
    return t


# ---------------------------------------------------------------- no filters

def test_nothing_is_excluded_by_default():
    assert mh._filter_reason(_track(), _args()) is None


def test_defaults_keep_a_track_with_no_year_or_flags():
    assert mh._filter_reason({"pop": 0}, _args()) is None


# ---------------------------------------------------------------- era (#12)

@pytest.mark.parametrize("year, keep", [
    (1979, False), (1980, True), (1994, True), (1999, True), (2000, False),
])
def test_year_range_is_inclusive_on_both_ends(year, keep):
    reason = mh._filter_reason(_track(year=year), _args(year_min=1980, year_max=1999))
    assert (reason is None) == keep
    if not keep:
        assert reason == "era"


def test_unknown_year_is_out_of_range_not_assumed_in():
    """An era brief wants certainty — a track Spotify gave no release date for is
    dropped by either bound rather than sneaking through."""
    t = _track(year=None)
    assert mh._filter_reason(t, _args(year_min=1980)) == "era"
    assert mh._filter_reason(t, _args(year_max=1999)) == "era"
    # ...but with no year filter at all it still passes
    assert mh._filter_reason(t, _args()) is None


def test_year_bounds_work_independently():
    assert mh._filter_reason(_track(year=2024), _args(year_min=1980)) is None
    assert mh._filter_reason(_track(year=1970), _args(year_min=1980)) == "era"


# ---------------------------------------------------------------- explicit (#12)

def test_no_explicit_drops_only_flagged_tracks():
    assert mh._filter_reason(_track(explicit=True), _args(no_explicit=True)) == "explicit"
    assert mh._filter_reason(_track(explicit=False), _args(no_explicit=True)) is None
    assert mh._filter_reason(_track(explicit=True), _args()) is None


# ---------------------------------------------------------------- pop band (#14)

@pytest.mark.parametrize("pop, keep", [
    (39, False), (40, True), (58, True), (75, True), (76, False),
])
def test_pop_band_is_inclusive_on_both_ends(pop, keep):
    """The 'known but still snobby' brief from the issue: pop 40-75."""
    reason = mh._filter_reason(_track(pop=pop), _args(pop_min=40, pop_max=75))
    assert (reason is None) == keep
    if not keep:
        assert reason == "pop"


def test_pop_bounds_work_independently():
    assert mh._filter_reason(_track(pop=90), _args(pop_max=65)) == "pop"
    assert mh._filter_reason(_track(pop=90), _args(pop_min=65)) is None
    assert mh._filter_reason(_track(pop=10), _args(pop_min=65)) == "pop"


def test_pop_max_zero_is_a_real_ceiling_not_absent():
    """0 is a valid bound, so the flags default to None rather than 0 — otherwise
    `--pop-max 0` would be indistinguishable from not passing it."""
    assert mh._filter_reason(_track(pop=5), _args(pop_max=0)) == "pop"
    assert mh._filter_reason(_track(pop=0), _args(pop_max=0)) is None


def test_missing_pop_is_treated_as_zero():
    assert mh._filter_reason({"pop": None, "year": 1994}, _args(pop_min=1)) == "pop"


# ---------------------------------------------------------------- precedence

def test_era_is_reported_before_explicit_and_pop():
    """A track failing several filters reports one reason, in _FILTER_REASONS order,
    so the stderr tallies stay a partition of what was dropped rather than
    double-counting."""
    t = _track(pop=99, year=2024, explicit=True)
    args = _args(year_max=1999, no_explicit=True, pop_max=50)
    assert mh._filter_reason(t, args) == "era"
    assert mh._FILTER_REASONS == ("era", "explicit", "pop")


# ---------------------------------------------------------------- validation

def test_valid_ranges_pass_validation():
    mh._validate_filters(_args(pop_min=0, pop_max=100, year_min=1980, year_max=1999))
    mh._validate_filters(_args())  # all off


@pytest.mark.parametrize("over, needle", [
    (dict(pop_min=101), "--pop-min must be 0-100"),
    (dict(pop_min=-1), "--pop-min must be 0-100"),
    (dict(pop_max=150), "--pop-max must be 0-100"),
    (dict(pop_min=80, pop_max=40), "is above --pop-max"),
    (dict(year_min=1999, year_max=1980), "is above --year-max"),
])
def test_impossible_windows_error_clearly(over, needle):
    """An empty roster reads as 'the library has nothing like that' — a much more
    expensive wrong conclusion than an error."""
    with pytest.raises(SystemExit) as exc:
        mh._validate_filters(_args(**over))
    assert needle in str(exc.value)


def test_equal_bounds_are_allowed():
    """--pop-min 50 --pop-max 50 is a narrow band, not an inverted one."""
    mh._validate_filters(_args(pop_min=50, pop_max=50))
    mh._validate_filters(_args(year_min=1994, year_max=1994))
    assert mh._filter_reason(_track(pop=50, year=1994),
                             _args(pop_min=50, pop_max=50,
                                   year_min=1994, year_max=1994)) is None
