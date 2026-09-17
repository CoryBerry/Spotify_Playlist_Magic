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
**Tags are the highest-signal filter — reach for `--tag` before `--search`,** because
`--search` matches the playlist *name only* and Cory names pools by mashing artist names
together (`Rising Hall Rivers` = Rising Appalachia/Trevor Hall/Nahko; `Broken Metric Stars`
= Broken Social Scene/Metric/Stars). Those names say nothing about genre or vibe.

Two axes. **Tier** — how much to trust a hit:
- `selects` — hand-vetted over years. "Stuff Cory loves." Lead "loved" mixes from here.
- `drops` — **blind-copied from a trusted source, tracks never audited.** A hit means
  "Cory trusted the source," NOT "Cory loves this." Discovery fuel; seasoning, not spine.
- `annual` — year pools / album-of-the-year lists. Enjoyed-but-algorithm-heavy.
- `feed` — external or critic-made lists (Paste, AOTY, Rolling Stone, friends' lists).
- `notmine` — provenance only: someone else made it. **Neutral weight, not a penalty** —
  several `notmine` pools are top-20 go-tos. It tells you who to credit (see
  [[mix-state-sources]]) and that Cory's own taste didn't filter it, nothing more.

**Vibe/kind:** `chill`, `electronic`, `folk`, `instrumental`, `yoga`, `hype` (high energy),
`decade` (60s–2020s + century pools), `rotation` (the 10/20/30, low signal),
`current` (`Last 300 Liked` — rolling now-signal), `office`, `kids`.

`yoga` is a **built brief**, not a genre: barefoot folk + world-acoustic + ambient, landing
in savasana. Six pools carry it. Trust the tag over the name here — `cathedral drops` and
`Bedtime Jams For Adults` both sound like they belong and don't (art-pop and neo-soul
respectively).

`folk` is the barefoot/conscious-folk cluster Cory specifically misses when it's absent —
Rising Appalachia / Trevor Hall / Nahko / Phish. Four pools carry it.

**Weight heavily by `use_count` — it's the leading signal of what Cory actually loves,
not just what matches a vibe on paper.** The `sources` list is already sorted most-used
first; lead the mix from the top. A high vibe-match with a low count (e.g. a playlist used
0–2×) is a weak pick — Cory may not even remember liking it. If a low-usage playlist is
genuinely the best fit, **say so explicitly** and pair it with a high-usage anchor rather
than centering the mix on it. Known go-to sources by usage include: Rising Appalachia's
Traditional folk (40×), Sounds & Musics Instrumental selects (38×), Cory's Folk Selects
(25×), Rising Hall Rivers (17×), and the `selects` family generally.

### 2. Build a roster — deep cuts, not hits
**Prefer `roster` over `tracks` for curation.** Cory builds album playlists specifically to
hear **deep cuts**, and calls popularity-led mixes *boring*. `roster` groups each source's
tracks by album, ranks each album by Spotify `popularity`, and by default **skips the top
1–2 hits and hands you the upper-middle "sweet spot"** (his "3rd–5th of 10") — the album
favorites that aren't the obvious single. High popularity is a *negative* signal here.

```
PYTHONUTF8=1 python .claude/skills/mix/mix_helper.py roster "90s Albums" "Chill Albums"
```

Knobs:
- `--top` — for an album where Cory *does* want the bangers/most-played, take the highest-popularity
  tracks instead of the deep-cut band. (Note: Spotify's API has no per-user play count; `popularity`
  is global streams, which within one album orders the same as "most played.")
- `--per-album N` (default 3) / `--skip-top N` (default 2) — size and depth of the band.
- `--per-artist N` — cap one artist from clumping the roster.
- `--year-min YYYY` / `--year-max YYYY` — era filter, off the album's release year
  (`--year-min 1980 --year-max 1999` for an 80s/90s brief). A track whose release date
  Spotify doesn't report is treated as **out** of range, not assumed in — a stderr line
  counts what the filter dropped. Caveat: the year is the *release date of the album
  version in the playlist*, so a remaster can read as its reissue year rather than the
  original; spot-check the edges of a tight range.
- `--no-explicit` — drop anything Spotify flags explicit (the `[E]` marker in the output).
- `--pop-min N` / `--pop-max N` — a **popularity window**, which band mode and `--top` can't
  express on their own. Applied *after* band/`--top` selection, so they compose:
  `--top --pop-max 70` is "each album's biggest track, minus the ubiquitous ones" — precisely
  the "recognizable but not overplayed" ask. Rough calibration from real builds:
  *"known, but you can still be snobby about liking it"* ≈ `--pop-min 40 --pop-max 75`;
  *"more snobby / less mainstream"* ≈ `--pop-max 65`, no floor.
- Impossible windows (`--pop-min 80 --pop-max 40`, `--pop-min 150`, an inverted year range)
  **error** rather than returning an empty roster — a silent nil reads as "the library has
  nothing like that", which is a much more expensive wrong conclusion.
- `--sample N [--seed S]` — randomly keep N of the candidates, so repeated builds surprise.
- Cooldown column: `·` never played, `❄Nd` on cooldown ice (within the 7-day window), `~Nd` played but thawed.
  `--fresh` drops anything on cooldown; `--thawed` surfaces only off-ice throwbacks you've heard before.
- **Ice box** (manual never-list / timed freeze — see below): iced tracks are excluded from `roster`
  and `tracks` by default. `--show-iced` reveals them tagged `🧊NVR` (never) / `🧊Nd` (days to thaw).
- `--no-cache` — force a live pull, bypassing (and refreshing) the disk cache described below.
- `--tags` — annotate each candidate with its **lead artist's** top-5 Last.fm tags (mood/genre
  context, handy now that Spotify's `/audio-features` is gone). Opt-in; plain `roster` stays offline.
  Tags show as an appended `[tag, tag, tag]` on the text line and a `"tags": [...]` field in `--json`.
  Each distinct artist is fetched once per run and reuses `lastfm_service`'s `.lastfm_cache.json`, so
  repeat rosters are cheap. **Needs `LASTFM_API_KEY`** in `.env` — `--tags` hard-fails without it;
  individual artists unknown to Last.fm just come back tagless (a stderr line reports how many resolved).

Each row carries its **runtime** (`m:ss`), **release year** (after the album name) and an
`[E]` marker when explicit; the stderr footer totals the roster's runtime (`runtime 2h57m`).
`--json` carries the same as `duration_ms` / `year` / `explicit`. **Size a mix off these —
don't spend an `sp.tracks()` pass on it.** "Make it ~3 hours" is arithmetic on the roster you
already have, including after a trim.

The old `tracks` command still exists for a plain full dump (add `--exclude-cooldown`), but reach
for it only when you deliberately want *everything*, not for normal curation.

**Track cache:** `roster` and `tracks` memoize each source's track pull to `.mix_cache/<playlist_id>.json`
(git-ignored, created lazily), keyed by Spotify's `snapshot_id`. An unchanged source is served straight
from disk — zero `playlist_items` calls — and re-pulls automatically the moment the playlist is edited
(the snapshot flips). No TTL, no config. One caveat: `snapshot_id` doesn't change when Spotify quietly
recomputes a track's `popularity`, so a long-untouched source serves *frozen* popularity — which `roster`
band-selection ranks on. Band selection is coarse enough that a few points of drift rarely matters; reach
for `--no-cache` if you want the freshest popularity for a build. The blob also carries a schema
`version`; bumping it in `mix_helper.py` invalidates every existing file, so entries written before a
field was added re-pull on their own rather than serving rows without it.

**Playlist cache (`refresh-cache`):** source *names and ids* resolve against the app's single-row
`playlist_cache` table. The Flask app refreshes it on a TTL, and the skill writes it too, so a
playlist `create` just made is usable as a source immediately. If a name still won't resolve — the
app has been closed a while, or you renamed something in the Spotify client — re-read the library:

```
PYTHONUTF8=1 python .claude/skills/mix/mix_helper.py refresh-cache
```

It reports the playlist count. Safe to run any time: the blob is regenerable by design (`cli.py
backup` skips it), and a `create`/`replace`/`refresh-cache` write leaves the app's own TTL checks
consistent rather than stale.

### 2b. "Do we even have these artists?" — `find-artists`

Before designing a themed mix around a roster of artists, check what the library actually holds.
This sweeps every **already-pulled** pool at once, fully offline — no Spotify calls — and it finds
things `sources --search` never could, because pool names (`Broken Metric Stars`) say nothing
about their contents:

```
PYTHONUTF8=1 python .claude/skills/mix/mix_helper.py find-artists "LCD Soundsystem" "The Rapture"
PYTHONUTF8=1 python .claude/skills/mix/mix_helper.py find-artists --file canon.txt --missing
```

Hits are grouped per name, ranked by popularity, each row tagged with the pool it came from
(`+N` when the track sits in several). `--top N` (default 3) sizes the sample, `--json` for
scripting, `--file` takes one name per line (`#` comments fine).

- **`--missing` is the useful half** — it lists only the names with *no* hits, which is what tells
  you whether a themed mix is buildable at all.
- Matching is **case-insensitive substring on the cached artist field, on purpose.** Punctuation
  and acronym names resolve badly through Spotify's artist search (`!!!` returns R.E.M., `CSS`
  returns RAC); the cache sidesteps it. For the same reason, when you *do* need to find such an
  artist on Spotify, search a known **album title** instead of the artist name.
- **Coverage is cached pools only.** A nil result means "not in any pool pulled so far", not "not
  in the library" — the footer says so, and names the count of pools searched. Pools still on an
  older cache schema are searched but show no runtime/year until a roster re-pulls them; the
  footer counts those too.

### 2c. Keeping a build cheap

Two habits that cost real time when ignored.

**Narrow with flags, not with your eyes.** Nearly every constraint a brief implies now has a
flag — `--pop-min`/`--pop-max`, `--year-min`/`--year-max`, `--no-explicit`, `--fresh`,
`--per-artist`, `--sample`. Push the brief *into the roster call* so what comes back is already
close to the shortlist. Rostering a 300-800 track pool wide and then reading 40-70 lines of
mostly-discarded rows is the single largest output block in a typical session, and it's avoidable.
When a filter genuinely has no flag, take `--json` and print only the slice you'll actually use —
never the raw dump.

**Budget ~4.0-4.5 min per track, then let the footer settle it.** Three ~3-hour mixes built from
these pools landed at:

| Mix | Tracks | Runtime | min/track |
|---|---|---|---|
| eclectic / world / organic house | 42 | 3h10 | 4.51 |
| dance-punk + hip-hop + hyperpop | 44 | 3h03 | 4.15 |
| deep-cut dance party | 46 | 3h06 | 4.03 |

So **3 hours ≈ 42-46 tracks** — start near 45 and read the roster footer's `runtime` total rather
than guessing. Expect to trim: all three overshot on first assembly, by 5, 39 and 19 minutes.
Track *count* is a poor predictor because a couple of outliers move the total more than the count
suggests — an 8-9 min remix or a 7 min house cut is worth two ordinary tracks. Cut the longest
low-conviction picks first; that's usually two or three edits rather than a rebalance.

### 3. Curate — this is the part that matters
Don't shuffle. Hand-pick and **sequence** into an intentional arc. Defaults that have
worked: ~18–24 tracks; no two adjacent tracks share an artist; interleave sources so no
genre clumps; give the mix a named shape (e.g. Slow Burn = ambient → indie → soft groove
→ landing; Cruise Control = one steady mid-tempo gear throughout). Write the chosen URIs
(one per line) to a file in the scratchpad directory.

At ~18–24 tracks you can hand-order by eye. **For a bigger pool or an explicit
"ride ups and downs" / dynamic-arc request, use the `sequence` command** instead of
re-deriving the arc math by hand (that's how the first big one burned a pile of iterations):

```
# input: one 'uri<TAB>energy[<TAB>artist[<TAB>name[<TAB>lane]]]' line per track.
# energy is YOUR taste call — 1=mellow, 2=mid, 3=banger (any integer scale).
# lane is YOUR genre bucket — dance-punk / disco / house / … (optional; see below).
PYTHONUTF8=1 python .claude/skills/mix/mix_helper.py sequence --tracks picks.txt > ordered.txt
```

It oscillates energy `--waves` times (default 3), eases in, and lands soft — the last
`--landing` fraction (default 0.14) is a firm wind-down. `--max-run` de-clumps the
transition shoulders (best-effort; a genuine peak/trough may sustain longer, which is
fine — a run of bangers at the apex *is* an "up"). `--seed` makes the within-energy
shuffle reproducible; `--uris-only` pipes straight into `create`. It prints an **energy
sparkline to stderr** (`▄▄▄█▄▄███▄…▁▁▁`) — glance at it to confirm the shape before you
ship, rather than reading 100 rows. Pure local logic, no Spotify calls.

**Give it a lane column for anything party-shaped.** Energy and genre are different axes:
a set can hold a flawless energy curve and still play eight dance-punk tracks back to back,
which "would be weird at an actual party." With a `lane` column, `--max-lane-run` (default 2)
stops more than N in a row from sharing one, and stderr adds the lane spread and the longest
run actually achieved. The columns are positional, so a lane with no artist/name means two empty
columns (`uri<TAB>2<TAB><TAB><TAB>disco`). The axes never fight — lane only decides *which* track
of an already-chosen energy fills a slot, so the curve comes out identical either way. If one
lane is more than about half the mix, some doubling up is arithmetic rather than a bug: it
gets spread evenly end to end instead of walling up at the finish, and stderr says how many
were forced.

**Assigning energy and lane is the one thing you can't automate** — Spotify killed
`/audio-features`, so there's no danceability/energy to read. Tag each track yourself
(artist/genre knowledge, or `roster --tags` for a Last.fm mood hint). The command owns the
*arc*, you own the *taste*.

### 4. Save first, then review
**Save the playlist immediately — don't wait for approval.** Cory prefers to react to a
real, saved playlist rather than a proposal. Default is **private**, and record + cooldown
so it behaves like a real build:

```
PYTHONUTF8=1 python .claude/skills/mix/mix_helper.py create \
  --name "Sunday Slow Burn" --desc "Chill arc, built from Cory's chill sources" \
  --uris-file /path/to/picks.txt --record --cooldown \
  --source "Chill Albums" --source "90s Albums" --source "Folk Selects"
```

**Always pass a `--source` for every playlist you drew from.** With `--record` this
bumps each source's `use_count` in `playlist_usage` — the *same* table and the *same*
+1-per-build semantics as a web Block Mix / Album Blast build. Without it the skill is
blind to its own builds: a fresh source you've leaned on for five mixes still reads as
`1×` in `sources`/`roster`, and step 1 would wrongly flag it as low-signal. Pass the same
source tokens (id or name substring) you used in `roster`/`tracks`; they resolve up front,
so a typo aborts before anything is created.

Then, in your reply: share the URL, **show the full tracklist exactly as curated (grouped
by section with your design notes)**, and **ask if there are any changes** — swap tracks,
trim/extend, re-sequence. Making a mix is reversible (unfollow, or Recently Created →
remove), so saving first costs nothing and gives Cory something real to react to.

- Playlists are **private** by default. Only pass `--public` if asked.
- `--record` logs it to `created_playlist` so it shows in the app's Recently Created page.
- `--cooldown` (with `--record`) writes the tracks to `track_history` so future Block Mix /
  Sampler builds won't immediately replay them. Use it when the mix should participate in the
  cooldown pool; omit it for a one-off you don't mind repeating.
- `--source PLAYLIST` (repeatable, with `--record`) counts this build toward each source's
  `use_count` — the "most used" signal step 1 leans on. Pass one per source you pulled from.

The helper prints the playlist URL — share it back to the user.

## Ice box — the never-list

A manual, long-lived exclusion list for tracks Cory is sick of. Distinct from the 7-day
cooldown: cooldown is automatic and short and *yields* when a pool gets small; **ice is a
hard exclusion** — an iced track never enters *any* build (this skill's `roster`/`tracks`,
and every Block Mix / Sampler / Album Blast / Text Import build in the web app), even as a
last-resort fallback. It's the same `track_ice` table on both sides, so a freeze here takes
effect in the app immediately, and vice versa.

Two shapes, one mechanism — a track is iced while `thaw_at` is NULL or still in the future:
- **Never-list** — bare `add` (or `--never`): gone until manually thawed.
- **Timed ice** — `--months N`: resurfaces on its own N months later, nostalgia intact.
  Timed rows auto-release through the app's existing thaw pass (and count into its
  "🌊 thawed" tally); never-list rows never auto-release.

```
# freeze — resolves a URI/URL directly, else searches Spotify and takes the top hit
PYTHONUTF8=1 python .claude/skills/mix/mix_helper.py ice add "HUMBLE. Kendrick Lamar" --months 6 --reason "heard to death"
PYTHONUTF8=1 python .claude/skills/mix/mix_helper.py ice add spotify:track:6nzXkCBOhb2mxctNihOqbb   # never-list

# review / release
PYTHONUTF8=1 python .claude/skills/mix/mix_helper.py ice list
PYTHONUTF8=1 python .claude/skills/mix/mix_helper.py ice thaw "Bad Girls"   # URI or name substring
```

`ice add` reports the exact track it matched (name — artist) — read it back before trusting a
name-based freeze; if it grabbed the wrong track, thaw it and re-add by URI.

## Big "best of the year" mixes (multi-source, cross-referenced)

A request like "best of 2025, ~100 songs, one per album, double the ones I played most"
is a different animal from a vibe mix. It's an offline data job — merge sources, cross-
reference a play-ranked list, pick per album, then `sequence`. Expect to **write a short
script in the scratchpad and iterate by running it** (read the output, fix, rerun); this
is data wrangling, not a one-shot prompt. Gotchas learned the hard way:

- **Normalize albums before grouping.** Spotify fragments one album into many `album_id`s
  — deluxe editions, pre-release singles, feat. variants all differ. Bucketing by raw
  `album_id` inflates the album count and double-picks the same record. Group by
  `(primary_artist, normalized_album_title)` — strip `(Deluxe)`/`(Extended)`/`(… Version)`
  suffixes — and merge across your sources so each real album is one bucket.
- **Re-check the ice box on your final picks — including boosted ones.** `roster`/`tracks`
  exclude iced tracks, but if you're reading the `.mix_cache` JSON directly (as a big
  cross-ref script does) you bypass that filter. Filter your picks against
  `track_ice` (thaw_at NULL or future) yourself, *before* counting to 100, or Cory's
  "overplayed" freezes leak back in. A boosted "played-most" album is exactly where an
  iced hit hides — fall back to another cut from that album when its top track is frozen.
- **The play-count overlap may be small — report it, don't force it.** Cross-referencing
  a curated album list against "My Spotify Top 100" can match only a handful of albums
  (the Top 100 skews to singles/other listening). That's a real finding worth stating
  plainly, not a bug to engineer around.
- **Energy tiers and lanes stay manual** (see step 3) — tag ~100 tracks by artist/genre
  knowledge, then hand the file to `sequence`. At that size the lane column earns its keep:
  a hundred tracks is where one genre quietly takes over whole stretches.

## Notes
- Reversible: if the user dislikes a result, they can unfollow it, or use the app's
  Recently Created → remove (which deletes from Spotify + DB).
- Match resolution: `sources`/`tracks`/`roster` accept a full playlist id or a case-insensitive
  name substring; ambiguous names error out — use a fuller name or the id. A name (or id) that
  matches *nothing* usually means a stale `playlist_cache` — run `refresh-cache`.
- Keep it simple; this mirrors existing app conventions (see `CLAUDE.md`). No new deps —
  it reuses `spotipy`, `python-dotenv`, and the SQLite DB the app already uses; `--tags` reuses the
  repo's own `lastfm_service` (stdlib-only, no extra pip packages).
