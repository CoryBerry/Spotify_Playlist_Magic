---
name: mix
description: Design and create a Spotify playlist ("mix") from Cory's existing playlists. Use when the user asks to "design a mix", "make me a playlist", "build a mix", assemble a vibe (chill, gym, dinner, mid-tempo, focus) from what they already own, or wants tracks curated and shipped to their Spotify account.
---

# mix — design & ship a Spotify playlist

Turn "design me a mix" into a real private playlist in Cory's Spotify account,
curated from his **existing** playlists (not random Spotify catalog). This skill
wraps the auth → pull → curate → create flow so you never hand-roll it.

All plumbing lives in `mix_helper.py` (same directory). Run it from the repo root
with `PYTHONUTF8=1` (playlist/track names contain emoji and accents that crash the
default Windows console codec):

```
PYTHONUTF8=1 python .claude/skills/mix/mix_helper.py <command> ...
```

Auth reuses the Flask app's `.cache` token (scopes include `playlist-modify-private`).
If the helper reports no cached token, tell the user to log into the app once, then retry.

## The loop

### 1. Pick sources by vibe
List candidate playlists, richest first (most-used at the top), with tags and track counts:

```
PYTHONUTF8=1 python .claude/skills/mix/mix_helper.py sources --search chill
PYTHONUTF8=1 python .claude/skills/mix/mix_helper.py sources --tag selects
```

Names live in the app's `playlist_cache`; **the DB never stores track contents**, so
you must pull tracks live (next step). Prefer 3–5 sources that fit the requested vibe.
Tags worth knowing: `drops` (discovery), `selects` (Cory's own taste), `chill`,
`electronic`, `annual`, `office`, `feed`.

**Weight heavily by `use_count` — it's the leading signal of what Cory actually loves,
not just what matches a vibe on paper.** The `sources` list is already sorted most-used
first; lead the mix from the top. A high vibe-match with a low count (e.g. a playlist used
0–2×) is a weak pick — Cory may not even remember liking it. If a low-usage playlist is
genuinely the best fit, **say so explicitly** and pair it with a high-usage anchor rather
than centering the mix on it. Known go-to sources by usage include: Rising Appalachia's
Traditional folk (40×), Sounds & Musics Instrumental selects (38×), Cory's Folk Selects
(25×), Rising Hall Rivers (17×), and the `selects` family generally.

### 2. Pull real tracks
Dump `uri<TAB>name<TAB>artist` from the chosen sources. Add `--exclude-cooldown` to
respect the app's 7-day cooldown (skips tracks used in recent builds), matching Block Mix:

```
PYTHONUTF8=1 python .claude/skills/mix/mix_helper.py tracks "Cory's Chilled Playlist" "Chill Albums" --exclude-cooldown
```

### 3. Curate — this is the part that matters
Don't shuffle. Hand-pick and **sequence** into an intentional arc. Defaults that have
worked: ~18–24 tracks; no two adjacent tracks share an artist; interleave sources so no
genre clumps; give the mix a named shape (e.g. Slow Burn = ambient → indie → soft groove
→ landing; Cruise Control = one steady mid-tempo gear throughout). Write the chosen URIs
(one per line) to a file in the scratchpad directory.

### 4. Save first, then review
**Save the playlist immediately — don't wait for approval.** Cory prefers to react to a
real, saved playlist rather than a proposal. Default is **private**, and record + cooldown
so it behaves like a real build:

```
PYTHONUTF8=1 python .claude/skills/mix/mix_helper.py create \
  --name "Sunday Slow Burn" --desc "Chill arc, built from Cory's chill sources" \
  --uris-file /path/to/picks.txt --record --cooldown
```

Then, in your reply: share the URL, **show the full tracklist exactly as curated (grouped
by section with your design notes)**, and **ask if there are any changes** — swap tracks,
trim/extend, re-sequence. Making a mix is reversible (unfollow, or Recently Created →
remove), so saving first costs nothing and gives Cory something real to react to.

- Playlists are **private** by default. Only pass `--public` if asked.
- `--record` logs it to `created_playlist` so it shows in the app's Recently Created page.
- `--cooldown` (with `--record`) writes the tracks to `track_history` so future Block Mix /
  Sampler builds won't immediately replay them. Use it when the mix should participate in the
  cooldown pool; omit it for a one-off you don't mind repeating.

The helper prints the playlist URL — share it back to the user.

## Notes
- Reversible: if the user dislikes a result, they can unfollow it, or use the app's
  Recently Created → remove (which deletes from Spotify + DB).
- Match resolution: `sources`/`tracks` accept a full playlist id or a case-insensitive
  name substring; ambiguous names error out — use a fuller name or the id.
- Keep it simple; this mirrors existing app conventions (see `CLAUDE.md`). No new deps —
  it reuses `spotipy`, `python-dotenv`, and the SQLite DB the app already uses.
