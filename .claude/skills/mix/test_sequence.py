"""Tests for the arc sequencer in mix_helper (`sequence` command).

Pure logic, no Spotify — asserts the invariants that took iteration to get right
when this was hand-rolled: every track survives, per-energy counts are preserved,
the tail lands soft, the body has no long walls of one energy, and adjacent
same-artist is avoided where supply allows.

The second half covers the *lane* (genre) axis added for issue #15 — a mix can have
a flawless energy curve and still play eight dance-punk tracks in a row.

    PYTHONUTF8=1 python -m pytest .claude/skills/mix/test_sequence.py
"""
import argparse
import importlib
from collections import Counter

mh = importlib.import_module("mix_helper")


def _pool(spec):
    """spec: list of (energy, artist) -> track dicts with unique uris."""
    return [{"uri": f"spotify:track:{i}", "energy": e, "artist": a, "name": f"S{i}"}
            for i, (e, a) in enumerate(spec)]


def _laned(spec):
    """spec: list of (energy, artist, lane) -> track dicts with unique uris."""
    return [{"uri": f"spotify:track:{i}", "energy": e, "artist": a, "name": f"S{i}",
             "lane": ln}
            for i, (e, a, ln) in enumerate(spec)]


def _lane_spread(dist, energies=(1, 2, 3)):
    """A pool with `dist` [(lane, count)], energies cycled and artists all distinct —
    so lane is the only constraint that can bind."""
    spec, i = [], 0
    for lane, count in dist:
        for _ in range(count):
            spec.append((energies[i % len(energies)], f"artist{i}", lane))
            i += 1
    return _laned(spec)


def _runs(values):
    """Run lengths of consecutive equal values."""
    out, run = [], 1
    for i in range(1, len(values)):
        run = run + 1 if values[i] == values[i - 1] else 1
        out.append(run)
    return out or [len(values)]


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


# ------------------------------------------------------------------ lanes (#15)

# The real brief behind the issue: 49 tracks across seven lanes, one of them more
# than half the set. "don't love the genre clumping. that'd be weird at an actual
# party."
REAL_DIST = [("dance-punk", 26), ("disco", 6), ("house", 5), ("funk", 5),
             ("indie", 3), ("electro", 3), ("ambient", 1)]


def test_lane_column_caps_consecutive_runs_when_feasible():
    pool = _lane_spread(REAL_DIST)
    seq = mh._sequence_arc(pool, max_lane_run=2, seed=1)
    assert max(_runs([t["lane"] for t in seq])) <= 2


def test_max_lane_run_of_one_alternates_when_supply_allows():
    pool = _lane_spread([("a", 12), ("b", 12), ("c", 12)])
    seq = mh._sequence_arc(pool, max_lane_run=1, seed=2)
    assert max(_runs([t["lane"] for t in seq])) == 1


def test_dominant_lane_doubles_are_spread_not_bunched():
    """A lane holding more than half the set *must* double up somewhere; the point
    is that those doubles land evenly rather than as a wall at one end."""
    pool = _lane_spread([("big", 30), ("small", 16)])
    seq = mh._sequence_arc(pool, max_lane_run=2, seed=3)
    lanes = [t["lane"] for t in seq]
    assert max(_runs(lanes)) == 2  # the forced doubles, and nothing worse

    quarter = len(seq) // 4
    counts = [lanes[i * quarter:(i + 1) * quarter].count("big") for i in range(4)]
    assert max(counts) - min(counts) <= 2  # no quarter hoards the dominant lane


def test_a_lane_too_big_for_the_cap_spreads_instead_of_walling():
    """40 of 50 in one lane cannot be capped at 2 — with 10 others there are only 11
    gaps, so 18 tracks must run long. The ask is that those land as evenly-sized
    groups throughout, not as ten tidy pairs followed by a wall of twenty."""
    pool = _lane_spread([("big", 40), ("small", 10)])
    seq = mh._sequence_arc(pool, max_lane_run=2, seed=11)
    lanes = [t["lane"] for t in seq]

    floor = 40 - (10 + 1) * 2  # tracks that must exceed the cap, however ordered
    assert mh._lane_excess(seq, 2) == floor  # no worse than arithmetic demands
    assert max(_runs(lanes)) <= 5          # groups of ~4, nowhere near a wall of 20

    quarter = len(seq) // 4
    counts = [lanes[i * quarter:(i + 1) * quarter].count("big") for i in range(4)]
    assert max(counts) - min(counts) <= 2


def test_lanes_correlated_with_energy_degrade_instead_of_failing():
    """When every banger is one lane, the arc itself forces the clumping — nothing
    can separate tracks that only exist at one energy. It must still return the whole
    pool, keep artists apart, and leave the curve alone."""
    pool = _laned([(3, f"hard{i}", "dance-punk") for i in range(15)]
                  + [(1, f"soft{i}", "ambient") for i in range(15)])
    seq = mh._sequence_arc(pool, max_lane_run=2, seed=12)
    assert {t["uri"] for t in seq} == {t["uri"] for t in pool}
    assert mh._artist_clashes(seq) == 0
    assert all((t["lane"] == "dance-punk") == (t["energy"] == 3) for t in seq)


def test_lane_de_clumping_never_bends_the_energy_arc():
    """Lane only chooses *which track of a given energy* fills a slot, so the energy
    shape must come out bit-for-bit identical to the same pool without lanes."""
    laned = _lane_spread(REAL_DIST)
    plain = [{k: v for k, v in t.items() if k != "lane"} for t in laned]
    assert ([t["energy"] for t in mh._sequence_arc(laned, max_lane_run=2, seed=4)]
            == [t["energy"] for t in mh._sequence_arc(plain, seed=4)])


def test_pools_with_no_lane_key_order_exactly_as_before():
    """Existing 2- and 3-column inputs carry no lane at all: the new code path must
    be inert for them, not merely harmless."""
    pool = _pool([(1, f"a{i}") for i in range(15)]
                 + [(2, f"b{i}") for i in range(15)]
                 + [(3, f"c{i}") for i in range(15)])
    assert ([t["uri"] for t in mh._sequence_arc(pool, max_lane_run=2, seed=5)]
            == [t["uri"] for t in mh._sequence_arc(pool, max_lane_run=0, seed=5)])


def test_max_lane_run_zero_disables_the_constraint():
    pool = _lane_spread(REAL_DIST)
    off = mh._sequence_arc(pool, max_lane_run=0, seed=6)
    on = mh._sequence_arc(pool, max_lane_run=2, seed=6)
    assert max(_runs([t["lane"] for t in off])) > max(_runs([t["lane"] for t in on]))


def test_unlabeled_tracks_separate_runs_rather_than_extending_them():
    pool = _laned([(2, f"a{i}", "rock") for i in range(6)]
                  + [(2, f"b{i}", "") for i in range(6)])
    seq = mh._sequence_arc(pool, max_lane_run=2, seed=7)
    assert mh._longest_lane_run(seq) <= 2
    assert mh._longest_lane_run([{"lane": ""}] * 10) == 0  # all blank: no run at all


def test_lane_work_never_buys_spacing_with_a_same_artist_adjacency():
    """The repair pass scores (lane excess, artist clashes) as a pair, so it can't
    fix genre clumping by putting one artist back to back."""
    pool = _laned([(1 + i % 3, "solo", "big") for i in range(5)]
                  + [(1 + i % 3, f"x{i}", "big") for i in range(25)]
                  + [(1 + i % 3, f"y{i}", "small") for i in range(16)])
    seq = mh._sequence_arc(pool, max_lane_run=2, seed=8)
    assert mh._artist_clashes(seq) == 0
    assert mh._longest_lane_run(seq) <= 2


def test_single_lane_pool_degrades_instead_of_failing():
    """Nothing to de-clump — every track is the same lane. It should still return
    the whole pool in arc order rather than spinning or dropping tracks."""
    pool = _lane_spread([("only", 30)])
    seq = mh._sequence_arc(pool, max_lane_run=2, seed=9)
    assert {t["uri"] for t in seq} == {t["uri"] for t in pool}


def test_deterministic_for_seed_with_lanes():
    pool = _lane_spread(REAL_DIST)
    assert ([t["uri"] for t in mh._sequence_arc(pool, max_lane_run=2, seed=10)]
            == [t["uri"] for t in mh._sequence_arc(pool, max_lane_run=2, seed=10)])


# ------------------------------------------------------- the command's I/O (#15)

def _args(tracks, **over):
    base = dict(tracks=tracks, waves=3.0, landing=0.14, max_run=3, max_lane_run=2,
                seed=0, uris_only=False)
    base.update(over)
    return argparse.Namespace(**base)


def _write(tmp_path, lines):
    f = tmp_path / "picks.txt"
    f.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(f)


def test_lane_column_round_trips_through_the_command(tmp_path, capsys):
    lines = [f"spotify:track:{i}\t{1 + i % 3}\tArtist{i}\tSong {i}\t"
             f"{'dance-punk' if i % 2 else 'disco'}" for i in range(12)]
    mh.cmd_sequence(_args(_write(tmp_path, lines)))
    out = capsys.readouterr().out.strip().splitlines()
    assert len(out) == 12
    assert all(len(row.split("\t")) == 5 for row in out)
    assert sorted(out) == sorted(lines)  # same rows, reordered


def test_shorter_inputs_keep_their_shape(tmp_path, capsys):
    two = [f"spotify:track:{i}\t{1 + i % 3}" for i in range(9)]
    mh.cmd_sequence(_args(_write(tmp_path, two)))
    assert all(len(r.split("\t")) == 2 for r in capsys.readouterr().out.strip().splitlines())

    three = [f"spotify:track:{i}\t{1 + i % 3}\tArtist{i}" for i in range(9)]
    mh.cmd_sequence(_args(_write(tmp_path, three)))
    assert all(len(r.split("\t")) == 3 for r in capsys.readouterr().out.strip().splitlines())


def test_a_lane_can_be_given_without_an_artist_or_name(tmp_path, capsys):
    lines = [f"spotify:track:{i}\t{1 + i % 3}\t\t\t{'a' if i % 2 else 'b'}"
             for i in range(10)]
    mh.cmd_sequence(_args(_write(tmp_path, lines)))
    out = capsys.readouterr().out.strip().splitlines()
    assert sorted(out) == sorted(lines)


def test_reports_the_achieved_lane_run_on_stderr(tmp_path, capsys):
    lines = [f"spotify:track:{i}\t{1 + i % 3}\tArtist{i}\tSong {i}\t"
             f"{'dance-punk' if i % 2 else 'disco'}" for i in range(12)]
    mh.cmd_sequence(_args(_write(tmp_path, lines)))
    err = capsys.readouterr().err
    assert "# energy " in err                       # the old sparkline still prints
    assert "dance-punk×6" in err and "disco×6" in err
    assert "longest lane run 2 (--max-lane-run 2)" in err


def test_a_laneless_input_reports_no_lane_line(tmp_path, capsys):
    lines = [f"spotify:track:{i}\t{1 + i % 3}\tArtist{i}" for i in range(9)]
    mh.cmd_sequence(_args(_write(tmp_path, lines)))
    err = capsys.readouterr().err
    assert "# energy " in err
    assert "lane" not in err


def test_uris_only_still_prints_bare_uris_with_lanes(tmp_path, capsys):
    lines = [f"spotify:track:{i}\t{1 + i % 3}\tArtist{i}\tSong {i}\trock"
             for i in range(9)]
    mh.cmd_sequence(_args(_write(tmp_path, lines), uris_only=True))
    out = capsys.readouterr().out.strip().splitlines()
    assert all(r.startswith("spotify:track:") and "\t" not in r for r in out)

