# ISSUES.md — Known bugs / things to investigate

---

## Source Playlists order on stats page

**Status:** Unconfirmed — needs testing

**Report:** User reports the Source Playlists list on the stats page does not appear to be in the order playlists were shuffled into blocks.

**Code says:** `BuildSource.position` is assigned by iterating `seen_pids`, which walks the shuffled `cycle` list and records first appearances. Stats page queries `order_by(BuildSource.position)`. Should be shuffle order.

**To test:** Run a Block Mix with a small set of playlists (3–5), note the actual interleave order from the resulting Spotify playlist, compare to the numbered list on the stats page.

**Possible causes if order is wrong:**
- Cycle list is shuffled but weights cause a playlist to appear many times — first occurrence may not reflect "dominant" position intuitively
- Form submission order of `selected_ids` could affect cycle construction before shuffle
- Position 0 = first drawn, but user may expect "most used" or "most prominent" playlist to be listed first
