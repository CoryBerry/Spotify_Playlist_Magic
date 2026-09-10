"""Tests for the pure feed extractor — no Firecrawl, no network."""
from feed_service import extract_lines, item_hash

SAMPLE_MD = """
# Gorilla vs. Bear

[Menu](https://example.com/menu)  Search  Subscribe

## New tracks this week

**Beach House – "Space Song"**

Snail Mail - "Valentine"

Some rambling paragraph about the show that is far too long to ever be a track title and should be ignored entirely by the extractor no matter what.

Big Thief — Change

Posted 12 - 15 in the archive

Follow us @gorillavsbear on socials

Beach House – "Space Song"
"""


def test_extracts_quoted_and_bare():
    lines = extract_lines(SAMPLE_MD)
    assert 'Beach House - Space Song' in lines
    assert 'Snail Mail - Valentine' in lines
    assert 'Big Thief - Change' in lines


def test_quoted_matches_lead():
    # Quoted titles are higher-confidence, so they sort ahead of bare ones.
    lines = extract_lines(SAMPLE_MD)
    assert lines.index('Beach House - Space Song') < lines.index('Big Thief - Change')


def test_dedupes():
    lines = extract_lines(SAMPLE_MD)
    assert lines.count('Beach House - Space Song') == 1


def test_drops_junk_and_ranges():
    lines = extract_lines(SAMPLE_MD)
    assert not any('12 - 15' in l for l in lines)          # numeric range, not a song
    assert not any('rambling' in l.lower() for l in lines)  # prose is too long / no dash
    assert not any('@' in l for l in lines)                 # social handle line


def test_hash_stable_and_normalized():
    assert item_hash("gvb", "Beach House - Space Song") == item_hash("gvb", "beach house  -  space song")
    assert item_hash("gvb", "A - B") != item_hash("other", "A - B")


if __name__ == "__main__":
    import sys
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  PASS {name}")
            except AssertionError as e:
                fails += 1
                print(f"  FAIL {name}: {e}")
    sys.exit(1 if fails else 0)
