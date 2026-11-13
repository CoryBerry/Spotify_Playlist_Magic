"""Names for playlists the app generates.

Convention (shared with the mix skill's `[Mix] ` prefix):

    [Kind] Title — Mon D 'YY

- The `[Kind]` tag leads so every tool's output groups together under an
  alphabetical sort, and is easy to skip when browsing for real playlists.
- The title carries the searchable words. When the user gives none, it's built
  from what the playlist actually holds (its sources / albums), not the clock.
- One separator, the em dash. The date sits last because it's the least useful
  part for search and sort.
"""
from datetime import datetime

SEP = " — "

BLOCK = "Block"


def short_date(now=None):
    now = now or datetime.now()
    return f"{now.strftime('%b')} {now.day} '{now.strftime('%y')}"  # e.g. "Nov 12 '26"


def _clean(text):
    """Collapse whitespace; generated names should never carry doubled spaces."""
    return " ".join((text or "").split())


def lead_title(names, limit=40):
    """Title from a list of source/album names: 'First', 'First & Second', or 'First +N'."""
    names = [_clean(n) for n in names if _clean(n)]
    if not names:
        return ""
    if len(names) == 2 and len(names[0]) + len(names[1]) + 3 <= limit:
        return f"{names[0]} & {names[1]}"
    rest = len(names) - 1
    return names[0] + (f" +{rest}" if rest else "")


def generated_name(kind, title="", now=None):
    """`[Kind] Title — Nov 12 '26`; title falls back to nothing, never a weekday."""
    title = _clean(title)
    head  = f"[{kind}] {title}" if title else f"[{kind}]"
    return f"{head}{SEP}{short_date(now)}"


def shuffled_copy_name(original, now=None):
    """The copy keeps the original's name up front so it sorts right beside it."""
    return f"{_clean(original)}{SEP}Shuffled {short_date(now)}"


def is_block_mix(name):
    """Block Mix output, new or old style — hidden from the Block Mix source grid so a
    build never recycles an earlier build. (Old style: `… : Block Mix : March 6 '26`.)"""
    name = name or ""
    return name.startswith(f"[{BLOCK}]") or "Block Mix" in name
