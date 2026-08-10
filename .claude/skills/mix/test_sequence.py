"""Tests for the arc sequencer in mix_helper (`sequence` command).

Pure logic, no Spotify — asserts the invariants that took iteration to get right
when this was hand-rolled: every track survives, per-energy counts are preserved,
the tail lands soft, the body has no long walls of one energy, and adjacent
same-artist is avoided where supply allows.

    PYTHONUTF8=1 python -m pytest .claude/skills/mix/test_sequence.py
"""
import importlib
from collections import Counter

mh = importlib.import_module("mix_helper")


def _pool(spec):
    """spec: list of (energy, artist) -> track dicts with unique uris."""
    return [{"uri": f"spotify:track:{i}", "energy": e, "artist": a, "name": f"S{i}"}
            for i, (e, a) in enumerate(spec)]


def _max_run(levels, body_end):
    run = best = 1
    for i in range(1, body_end):
        run = run + 1 if levels[i] == levels[i - 1] else 1
        best = max(best, run)
    return best


def test_preserves_every_track_and_per_level_supply():
    pool = _pool([(1, f"a{i}") for i in range(20)]
                 + [(2, f"b{i}") for i in range(25)]
                 + [(3, f"c{i}") for i in range(15)])
    seq = mh._sequence_arc(pool, seed=1)
    assert len(seq) == len(pool)
    assert {t["uri"] for t in seq} == {t["uri"] for t in pool}  # no drops/dupes
    assert Counter(t["energy"] for t in seq) == Counter(t["energy"] for t in pool)


def test_lands_soft_on_low_energy_tail():
    pool = _pool([(1, f"a{i}") for i in range(30)]
                 + [(2, f"b{i}") for i in range(30)]
                 + [(3, f"c{i}") for i in range(30)])
    seq = mh._sequence_arc(pool, land_frac=0.14, seed=2)
    tail = [t["energy"] for t in seq[-10:]]
    # the wind-down is the lowest gear throughout, and the very last is a floor
    assert max(tail) == 1
    assert seq[-1]["energy"] == 1


def test_opens_below_peak():
    pool = _pool([(1, f"a{i}") for i in range(30)]
                 + [(2, f"b{i}") for i in range(30)]
                 + [(3, f"c{i}") for i in range(30)])
    seq = mh._sequence_arc(pool, open_frac=0.07, seed=3)
    assert seq[0]["energy"] < max(t["energy"] for t in pool)  # not straight to a banger


def test_smoothing_de_clumps_transition_shoulders():
    """max_run is a best-effort de-clumper, not a hard cap (a genuine peak may
    sustain). So the honest contract: turning it on never *increases* the longest
    body run, and here it strictly reduces it."""
    pool = _pool([(1, f"a{i}") for i in range(30)]
                 + [(2, f"b{i}") for i in range(30)]
                 + [(3, f"c{i}") for i in range(30)])
    n = len(pool)
    body_end = next(i for i in range(n) if i / (n - 1) > 0.86)
    raw = _max_run([t["energy"] for t in mh._sequence_arc(pool, max_run=999, seed=4)], body_end)
    smooth = _max_run([t["energy"] for t in mh._sequence_arc(pool, max_run=3, seed=4)], body_end)
    assert smooth <= raw
    assert smooth < raw  # there was shoulder clumping to fix, and it did


def test_avoids_adjacent_same_artist_when_supply_allows():
    # plenty of distinct artists per level -> adjacency is always avoidable
    pool = _pool([(1, f"a{i}") for i in range(20)]
                 + [(2, f"b{i}") for i in range(20)]
                 + [(3, f"c{i}") for i in range(20)])
    seq = mh._sequence_arc(pool, seed=5)
    assert all(seq[i]["artist"] != seq[i - 1]["artist"] for i in range(1, len(seq)))


def test_multi_track_artist_never_adjacent():
    # one artist owns several tracks; they must not end up back-to-back
    pool = _pool([(2, "solo")] * 4 + [(2, f"x{i}") for i in range(20)]
                 + [(1, f"y{i}") for i in range(10)] + [(3, f"z{i}") for i in range(10)])
    seq = mh._sequence_arc(pool, seed=6)
    assert all(not (seq[i]["artist"] == "solo" and seq[i - 1]["artist"] == "solo")
               for i in range(1, len(seq)))


def test_deterministic_for_seed():
    pool = _pool([(1, f"a{i}") for i in range(15)]
                 + [(2, f"b{i}") for i in range(15)]
                 + [(3, f"c{i}") for i in range(15)])
    assert ([t["uri"] for t in mh._sequence_arc(pool, seed=7)]
            == [t["uri"] for t in mh._sequence_arc(pool, seed=7)])


def test_empty_and_singleton():
    assert mh._sequence_arc([]) == []
    one = _pool([(2, "only")])
    assert [t["uri"] for t in mh._sequence_arc(one)] == ["spotify:track:0"]


def test_single_energy_level_degrades_gracefully():
    pool = _pool([(2, f"a{i}") for i in range(12)])
    seq = mh._sequence_arc(pool, seed=8)
    assert len(seq) == 12
    assert {t["uri"] for t in seq} == {t["uri"] for t in pool}
