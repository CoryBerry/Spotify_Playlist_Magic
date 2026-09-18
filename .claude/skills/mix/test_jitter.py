"""Tests for roster --jitter random walk in mix_helper.

Exercises the pure _jitter_skip seam — no Spotify or DB touched. Asserts the
walk's contract: fresh albums stay on the sweet spot, the coin is deterministic
(same album + step count reproduces), it walks off-center under use, and it
stays bounded in [0, hi] with top (skip 0) reachable.

    PYTHONUTF8=1 python -m pytest .claude/skills/mix/test_jitter.py
"""
import importlib

mh = importlib.import_module("mix_helper")


def test_zero_steps_stays_on_sweet_spot():
    # A never-used album (no history) must not move off the band default.
    assert mh._jitter_skip("albumA", base_skip=2, steps=0, hi=8) == 2


def test_no_room_returns_base():
    # hi <= 0 (album no bigger than per_album) — nothing to walk across.
    assert mh._jitter_skip("albumA", base_skip=2, steps=5, hi=0) == 2


def test_deterministic_for_same_album_and_steps():
    a = mh._jitter_skip("albumX", base_skip=2, steps=4, hi=9)
    b = mh._jitter_skip("albumX", base_skip=2, steps=4, hi=9)
    assert a == b  # same history reproduces the same pick


def test_history_actually_moves_the_pick():
    # Over its own step counts, at least one lands off the sweet spot — the walk
    # isn't a no-op. (Deterministic, so this is stable.)
    base, hi = 2, 9
    positions = {mh._jitter_skip("Rumours", base, steps, hi) for steps in range(1, 8)}
    assert positions != {base}


def test_stays_within_bounds_and_can_reach_top():
    # Reflected walk never escapes [0, hi]; skip 0 (the #1 track) is allowed.
    hi = 5
    seen = set()
    for album in ("a", "b", "c", "d", "e", "f", "g", "h"):
        for steps in range(0, 12):
            pos = mh._jitter_skip(album, base_skip=2, steps=steps, hi=hi)
            assert 0 <= pos <= hi
            seen.add(pos)
    assert 0 in seen  # "picking top is fine" — the walk can reach rank 0
