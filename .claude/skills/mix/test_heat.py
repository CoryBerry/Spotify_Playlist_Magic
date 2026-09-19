"""Tests for the `heat` command in mix_helper (issue #19).

Pure logic + local file I/O, no Spotify/DB — `_heat_tier`'s curve math takes
`today` as an injected parameter, so every boundary is exercised offline
against fixed dates rather than mocking the clock.

    PYTHONUTF8=1 python -m pytest .claude/skills/mix/test_heat.py
"""
import importlib
from datetime import date, timedelta

mh = importlib.import_module("mix_helper")


# ---------------------------------------------------------------- _parse_heat

HEAT_DOC = """\
# Heat — Cory

> What I'm into right now.

Tiers: 30/15/5
Cap: 50

## Vibing

| What | Kind | Started | Fade | Fits | Note |
|---|---|---|---|---|---|
| Dance-punk | genre | 2026-09-18 | 60d | dance, party, hype | |
| Wednesday | artist | 2026-09-14 | | | |

## Concerts

| Who | Date | Fade | Fits | Note |
|---|---|---|---|---|
| Modest Mouse | 2026-10-15 | | | |
| Wednesday | 2026-11-14 | | | w/ Mannequin Pussy |
| Mannequin Pussy | 2026-11-14 | | | same show |

## Cooled off

<ignored by the skill>
"""


def _write(tmp_path, text, name="heat.md"):
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return str(p)


def test_parses_both_tables_and_settings(tmp_path):
    entries, settings = mh._parse_heat(_write(tmp_path, HEAT_DOC))
    assert settings == {"tiers": (30, 15, 5), "cap": 50}

    vibing = [e for e in entries if e["type"] == "vibing"]
    concerts = [e for e in entries if e["type"] == "concert"]
    assert len(vibing) == 2
    assert len(concerts) == 3

    dp = vibing[0]
    assert dp["name"] == "Dance-punk"
    assert dp["kind"] == "genre"
    assert dp["started"] == date(2026, 9, 18)
    assert dp["fade"] == 60
    assert dp["fits"] == ["dance", "party", "hype"]

    wed_vibe = vibing[1]
    assert wed_vibe["fade"] == 30          # blank Fade -> default
    assert wed_vibe["fits"] == []          # blank Fits -> []

    mp = concerts[2]
    assert mp["name"] == "Mannequin Pussy"
    assert mp["date"] == date(2026, 11, 14)
    assert mp["note"] == "same show"


def test_cooled_off_section_ignored(tmp_path):
    entries, _ = mh._parse_heat(_write(tmp_path, HEAT_DOC))
    assert all(e["name"] != "ignored by the skill" for e in entries)
    assert len(entries) == 5  # 2 vibing + 3 concerts, nothing from Cooled off


def test_blank_tiers_and_cap_take_defaults(tmp_path):
    doc = """\
## Vibing

| What | Kind | Started | Fade | Fits | Note |
|---|---|---|---|---|---|
| Dance-punk | genre | 2026-09-18 | | | |
"""
    _, settings = mh._parse_heat(_write(tmp_path, doc))
    assert settings == {"tiers": mh.DEFAULT_HEAT_TIERS, "cap": mh.DEFAULT_HEAT_CAP}


def test_malformed_row_skipped_and_reported_not_fatal(tmp_path, capsys):
    doc = """\
## Vibing

| What | Kind | Started | Fade | Fits | Note |
|---|---|---|---|---|---|
| Dance-punk | genre | not-a-date | | | |
| Wednesday | artist | 2026-09-14 | garbage | | |
| | artist | 2026-09-14 | | | |
| Phoebe Bridgers | artist | 2026-09-01 | | | |
"""
    entries, _ = mh._parse_heat(_write(tmp_path, doc))
    assert [e["name"] for e in entries] == ["Phoebe Bridgers"]  # only the clean row survives
    err = capsys.readouterr().err
    assert err.count("skipped malformed heat row") == 3


def test_missing_file_is_the_caller_problem_not_parse_heats():
    # _parse_heat itself doesn't guard existence — cmd_heat does (see below).
    import pytest
    with pytest.raises(FileNotFoundError):
        mh._parse_heat("does/not/exist/heat.md")


# ---------------------------------------------------------------- _heat_tier (Vibing fade)

def _vibe(started, fade=30):
    return {"type": "vibing", "started": started, "fade": fade}


def test_vibing_fade_boundaries_30d():
    start = date(2026, 1, 1)
    cases = {
        0: 1, 9: 1, 10: 2, 19: 2, 20: 3, 29: 3, 30: 0,
    }
    for elapsed, want_tier in cases.items():
        tier, phase = mh._heat_tier(_vibe(start), start + timedelta(days=elapsed))
        assert (elapsed, tier) == (elapsed, want_tier)
        assert phase == ("expired" if want_tier == 0 else "fade")


def test_vibing_fade_boundaries_5d():
    start = date(2026, 1, 1)
    cases = {0: 1, 1: 1, 2: 2, 3: 2, 4: 3, 5: 0}
    for elapsed, want_tier in cases.items():
        tier, phase = mh._heat_tier(_vibe(start, fade=5), start + timedelta(days=elapsed))
        assert tier == want_tier
        assert phase == ("expired" if want_tier == 0 else "fade")


def test_vibing_fade_boundaries_60d():
    start = date(2026, 1, 1)
    cases = {19: 1, 20: 2, 39: 2, 40: 3, 59: 3, 60: 0}
    for elapsed, want_tier in cases.items():
        tier, phase = mh._heat_tier(_vibe(start, fade=60), start + timedelta(days=elapsed))
        assert tier == want_tier


# ---------------------------------------------------------------- _heat_tier (Concerts)

def _show(show_date, fade=30):
    return {"type": "concert", "date": show_date, "fade": fade}


def test_concert_ramp_boundaries():
    show = date(2026, 10, 15)
    # until=91 dormant, 90 tier3, 31 tier3, 30 tier2, 8 tier2, 7 tier1, 0 tier1
    cases = {
        91: (0, "dormant"),
        90: (3, "ramp"),
        31: (3, "ramp"),
        30: (2, "ramp"),
        8: (2, "ramp"),
        7: (1, "ramp"),
        0: (1, "ramp"),
    }
    for until, want in cases.items():
        today = show - timedelta(days=until)
        assert mh._heat_tier(_show(show), today) == want


def test_concert_afterglow_boundaries_default_30d_fade():
    show = date(2026, 10, 15)
    # afterglow: elapsed = -until - 1
    cases = {
        -1: (1, "afterglow"),    # elapsed 0
        -10: (1, "afterglow"),   # elapsed 9
        -11: (2, "afterglow"),   # elapsed 10
        -30: (3, "afterglow"),   # elapsed 29
        -31: (0, "expired"),     # elapsed 30
    }
    for until, want in cases.items():
        today = show - timedelta(days=until)
        assert mh._heat_tier(_show(show), today) == want


def test_concert_afterglow_matches_worked_example():
    """The exact Oct 15 show worked through in the issue spec."""
    show = date(2026, 10, 15)
    expect_tier1 = [date(2026, 10, d) for d in range(16, 26)]
    expect_tier2 = [date(2026, 10, 26)] + [date(2026, 11, d) for d in range(1, 5)]
    expect_tier3 = [date(2026, 11, d) for d in range(5, 15)]
    for d in expect_tier1:
        assert mh._heat_tier(_show(show), d) == (1, "afterglow")
    for d in expect_tier2:
        assert mh._heat_tier(_show(show), d) == (2, "afterglow")
    for d in expect_tier3:
        assert mh._heat_tier(_show(show), d) == (3, "afterglow")
    assert mh._heat_tier(_show(show), date(2026, 11, 15)) == (0, "expired")


# ---------------------------------------------------------------- _heat_slots

def test_slots_under_cap_use_full_share():
    entries = [{"tier": 1}, {"tier": 2}, {"tier": 3}]  # shares 30/15/5 = 50, == cap
    settings = {"tiers": (30, 15, 5), "cap": 50}
    assert mh._heat_slots(entries, 20, settings) == [6, 3, 1]


def test_slots_scale_down_proportionally_past_cap():
    entries = [{"tier": 1}, {"tier": 1}, {"tier": 1}]  # 30+30+30=90 > cap 50
    settings = {"tiers": (30, 15, 5), "cap": 50}
    slots = mh._heat_slots(entries, 30, settings)
    # scale = 50/90; each scaled share = 16.67%; 16.67% of 30 = 5.0 -> 5 each
    assert slots == [5, 5, 5]
    assert sum(slots) == 15  # exactly the cap (50%) of a 30-track mix


def test_slots_floor_at_one_can_exceed_cap():
    entries = [{"tier": 3}]  # 5% of 10 tracks = 0.5 -> floors to 0, bumped to 1
    settings = {"tiers": (30, 15, 5), "cap": 50}
    assert mh._heat_slots(entries, 10, settings) == [1]


def test_slots_empty_or_zero_total():
    settings = {"tiers": (30, 15, 5), "cap": 50}
    assert mh._heat_slots([], 20, settings) == []
    assert mh._heat_slots([{"tier": 1}], 0, settings) == [0]


# ---------------------------------------------------------------- _heat_fits

def test_fits_blank_matches_anything():
    assert mh._heat_fits({"fits": []}, ["chill", "focus"]) is True
    assert mh._heat_fits({}, []) is True


def test_fits_requires_overlap():
    entry = {"fits": ["dance", "party", "hype"]}
    assert mh._heat_fits(entry, ["gym", "hype"]) is True
    assert mh._heat_fits(entry, ["chill", "focus"]) is False


def test_fits_is_case_insensitive():
    entry = {"fits": ["Dance"]}
    assert mh._heat_fits(entry, ["dANCE"]) is True
