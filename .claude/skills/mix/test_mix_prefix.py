"""Tests for the `[Mix] ` name tag shared by create and replace.

Exercises the pure _prefixed seam — no Spotify or DB touched. The tag exists so
generated playlists group together in the library; the contract that matters is
that it's idempotent (a name can round-trip through create and replace without
growing `[Mix] [Mix] `) and that it's opt-out-able.

    PYTHONUTF8=1 python -m pytest .claude/skills/mix/test_mix_prefix.py
"""
import importlib

mh = importlib.import_module("mix_helper")


def test_plain_name_gets_the_tag():
    assert mh._prefixed("Cruise Control") == "[Mix] Cruise Control"


def test_already_tagged_name_is_untouched():
    # Idempotent: callers may hand over a name that already came back from create.
    assert mh._prefixed("[Mix] Cruise Control") == "[Mix] Cruise Control"


def test_repeated_application_never_stacks():
    name = "Chores"
    for _ in range(3):
        name = mh._prefixed(name)
    assert name == "[Mix] Chores"


def test_no_prefix_opts_out():
    assert mh._prefixed("Cruise Control", no_prefix=True) == "Cruise Control"


def test_none_passes_through():
    # `replace` without --name: there is no rename to tag, so nothing happens.
    assert mh._prefixed(None) is None


def test_empty_name_passes_through():
    assert mh._prefixed("") == ""
