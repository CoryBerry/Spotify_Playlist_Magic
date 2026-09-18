# CLAUDE.md — Spotify Playlist Magic

> Briefing file for Claude Code. Read this before touching anything.

---

## What this is

A Flask web app for building and managing Spotify and Plex playlists in ways the official apps don't support. Originally a Python learning project, now a real shareable tool.

---

## Personal overlay (`profile/`)

**At session start, read `profile/CONTEXT.md`** — it holds Cory's hard vetoes, house rules for
building mixes, and the index of people he builds for. `profile/playlists.md` is the pool cheat
sheet. This repo is deliberately generic so it can be shared; everything personal lives in the
gitignored `profile/` overlay, which is **its own private repo**
(`CoryBerry/spotify-magic-profile-cb`) cloned into that path.

If `profile/` is **absent**, this is a fresh/unconfigured clone — copy `profile.sample/` →
`profile/` and fill it in (see `profile.sample/README.md`). The skills no-op gracefully on the
sections it leaves empty.

Two kinds of content live there, and the distinction is load-bearing:

- **Live-read, hand-authored** — `CONTEXT.md`, `playlists.md`, `notes/`. The point of the overlay.
- **Mirrors, script-written** — `memory/` (copy of `~/.claude/projects/C--dev-crate/memory/`),
  `backups/spotify_tools.sql` (DB dump), `cache/` (friend profile pulls). Never hand-edit these;
  the next sync overwrites them. `~/.claude` stays authoritative for memory.

`profile-sync.ps1` dumps the DB (`python cli.py backup` — SQL text, skipping the regenerable
`playlist_cache` table), mirrors memory/cache/notes into the overlay, then commits + pushes
**only if something changed**. `-NoPush` for a local commit, `-Quiet` for log-only output.

Run nightly at 02:30 by the **"Spotify Magic Profile Sync"** scheduled task (mirrors the
"TodoMe Backup" task's settings, offset so the two don't collide). One line per run lands in
`instance/profile-sync.log`. The task runs as `cberr` with Interactive logon, so it fires only
while logged in — `StartWhenAvailable` catches up after the machine has been off. Running the
script by hand is always safe and does the same thing.

Restore instructions for a new machine are in `profile/README.md`.

---

## Stack

- **Framework:** Flask
- **DB:** SQLAlchemy + SQLite (`instance/spotify_tools.db`)
- **Templates:** Jinja2 (extends `base.html`)
- **Frontend:** Bootstrap 5 (via CDN)
- **APIs:** Spotipy (Spotify OAuth), PlexAPI (token-based, optional)

---

## App structure

```
app.py              ← Flask routes, models, and web-specific helpers
spotify_service.py  ← Spotify search/matching (_name_sim, _search_line) + resolve_tracks /
                      create_playlist_from_lines — one source of truth, shared with the CLI
feed_service.py     ← Feed Radar: Firecrawl scrape + "Artist – Title" extraction (pure, unit-tested)
lastfm_service.py   ← Last.fm reads: per-artist tags (roster --tags) + LASTFM_USER-scoped
                      scrobbles/loves (roster --mine). Key-only auth; TTL'd cache for user data
cli.py              ← headless, prompt-free CLI (login / resolve / build) over spotify_service
templates/
  base.html         ← shared layout (Bootstrap, nav, cache footer)
  spotify_*.html    ← Spotify tool pages (incl. spotify_feed.html — Feed Radar)
  plex_*.html       ← Plex tool pages
  recently_created.html
instance/
  spotify_tools.db  ← SQLite DB (auto-created, don't commit)
profile.sample/     ← committed template for the personal overlay
profile/            ← gitignored; the private overlay repo (see above)
profile-sync.ps1    ← dumps the DB + mirrors memory/cache into the overlay, commits, pushes
IDEAS.md            ← feature backlog
```

> **Note:** `app.py` is no longer the sole home for logic — search/matching and resolver
> code now live in `spotify_service.py` so the web app and `cli.py` share one implementation.
> Feed Radar's scrape/extract logic lives in `feed_service.py`.

> **Note:** `README.md` is the source of truth for user-facing feature descriptions. CLAUDE.md may drift — cross-check README when in doubt, and keep both in sync when making structural changes.

---

## Models

```python
PlaylistTag       # user-applied tags on Spotify playlists (unique per playlist+tag)
PlaylistCache     # cached Spotify playlist list, 15min/24h TTL tiers (falls back to stale on timeout);
                  # also written by the mix skill's `create` / `refresh-cache` — regenerable by design
CreatedPlaylist   # history of every Block Mix / Album Blast created (alive/checked_at, gen_seconds, track_count)
PlaylistUsage     # use_count + last_used per playlist+provider — drives "most used" sort
TrackHistory      # track_id + used_at — 7-day cooldown pool to avoid replaying recent tracks
TrackIce          # manual never-list / timed freeze (thaw_at NULL=never); HARD exclusion honored by every build + the mix skill
BuildSource       # source playlists of a Block Mix build, in cycle order (name/owner/position/weight); FK → CreatedPlaylist
ThawTally         # monthly count of tracks thawed from cooldown, per (year, month, provider)
AppSettings       # user-tunable cooldown_days (default 7) + cooldown_max_plays (default 2); driven by /settings
FeedItem          # Feed Radar review queue: one row per (source, harvested line), keyed by item_hash; status pending|added|rejected|miss
```

---

## Key routes

| Route | What it does |
|---|---|
| `/spotify/playlists` | Block Mix — select playlists, build interleaved playlist |
| `/spotify/build` | POST — executes Block Mix build |
| `/spotify/album-blaster` | Album Blaster — browse playlists |
| `/spotify/album-blaster/<id>` | Pick tracks from a playlist |
| `/spotify/album-blast` | POST — executes Album Blast build |
| `/spotify/sampler` | Album Sampler — browse playlists |
| `/spotify/sampler/<id>` | Config page — shows album count, set songs-per-album & #albums |
| `/spotify/sample` | POST — take the first X songs from each of the first Y albums on the playlist |
| `/spotify/album-sampler-blocks` | POST — second button on Block Mix: sample N random albums × X random songs from each *selected* playlist (with 7-day cooldown) |
| `/spotify/manage` | Manage playlists — filter, tag, delete, toggle visibility |
| `/spotify/tag/add` | POST JSON — add tag to playlist |
| `/spotify/tag/remove` | POST JSON — remove tag from playlist |
| `/spotify/tags/all` | GET JSON — all tags (autocomplete) |
| `/spotify/toggle-visibility/<id>` | POST — toggle playlist public/private |
| `/spotify/delete` | POST — bulk unfollow playlists |
| `/spotify/text-import` | Text Import — paste/upload a text list of albums or tracks |
| `/spotify/text-import/preview` | POST — parse text, search Spotify, Trust It or show manual review |
| `/spotify/text-import/build` | POST — create playlist from manual-select form |
| `/spotify/stats` | Playlist picker — choose a playlist to view its stats |
| `/spotify/stats/<id>` | Track count, runtime, top artists, usage count, tracklist with per-track 🧊 freeze; Randomize (shuffle in place / shuffled copy) |
| `/spotify/randomize` | POST — shuffle a playlist's order with a fresh OS-entropy seed; `mode=inplace` (owned only) or `mode=copy` (new `… : Shuffled` playlist) |
| `/spotify/ice-box` | Ice Box — list frozen tracks (thaw date/"never") + freeze-a-track search |
| `/spotify/ice/freeze` | POST JSON — freeze a track (never-list or timed); upserts on (track_id, provider) |
| `/spotify/ice/thaw` | POST JSON — thaw (delete) a track from the ice box |
| `/spotify/ice/search` | GET JSON — Spotify track search for the freeze-a-track box |
| `/spotify/feed` | Feed Radar — watched blog sources + review queue of matched `Artist – Title` lines |
| `/spotify/feed/poll` | POST — scrape a source via Firecrawl, extract lines, resolve to Spotify, queue hits (minus iced) as `pending`, misses as `miss` |
| `/spotify/feed/resolve` | POST — bulk add selected queue items to a playlist (honors ice box) or reject them |
| `/spotify/cache/refresh` | Force invalidate playlist cache |
| `/recently-created` | History of created playlists with alive/deleted status |
| `/recently-created/scan` | POST — re-check which created playlists are still alive on the provider |
| `/recently-created/delete/<id>` | POST — unfollow/delete on the provider + mark the history row dead (keeps it) |
| `/recently-created/remove/<id>` | POST — remove the history row from the DB only (no provider call) |
| `/recently-created/clear-dead` | POST — purge dead entries from DB |
| `/settings` | GET/POST — tune cooldown window (days) and max plays before a track ices |
| `/settings/thaw-all` | POST — clear all `TrackHistory`, freeing every cooldown track |
| `/plex/playlists` | Plex Block Mix |
| `/plex/build` | POST — executes Plex Block Mix build |
| `/plex/album-blaster` | Plex Album Blaster — browse playlists |
| `/plex/album-blaster/<key>` | Pick tracks from a Plex playlist |
| `/plex/album-blast` | POST — executes Plex Album Blast build |
| `/plex/stats/<key>` | Plex playlist stats |
| `/plex/not-configured` | Shown when PLEX_URL/PLEX_TOKEN missing |

---

## Key algorithms

**Block Mix build order:** fetch all tracks → apply 7-day cooldown (`TrackHistory`) → build weighted cycle (`cycle.extend([pid] * weight)`) → shuffle → iterate repeats → sample blocks → insert pinned blocks every N → dedupe preserving order → prepend cover art tracks → create playlist in batches of 100.

**7-day cooldown:** tracks used in any build are written to `TrackHistory`. New builds exclude them unless the remaining pool would be smaller than `block_size` (safety fallback keeps the build from failing).

**Ice box (`TrackIce` + `iced_ids(provider)`):** a manual, long-lived exclusion — never-list (`thaw_at` NULL) or timed freeze (`thaw_at` in the future). Unlike cooldown, it's a **hard** exclusion: `iced_ids()` is filtered from the pool *before* the cooldown fallback (so an iced track never returns on a small pool) and applied as a final filter on the deterministic tools (Album Blast, Text Import) too. Timed ice is released by `auto_thaw()` alongside cooldown and folded into the same `ThawTally`. Curated from both the `mix` skill (`mix_helper.py ice add/list/thaw`) and the web Ice Box (`/spotify/ice-box`, `/spotify/ice/{freeze,thaw,search}`), which read/write the same table — a freeze in either surface is honored by the next build with no sync step. The web freeze snowflake (`templates/_ice.html` macro + a delegated handler in `base.html`) also appears on the build done page and Stats tracklists. `_add_months()` in `app.py` mirrors the mix skill's helper so both writers land identical thaw dates.

**Mood presets (`MOOD_PRESETS`):** code is present but disabled. Spotify restricted `/audio-features` for new apps in late 2024. Do not re-enable without verifying API access.

**Album Sampler build:** `_group_playlist_albums()` fetches the playlist's tracks once (album id/name ride along on each track object — no per-album API calls) and buckets track URIs by `album.id` in playlist order. Build then takes the first N albums (playlist order, default all) and the first X (default 3) tracks of each — deterministic, no randomness, result stays in album order. Cheaper than Album Blast, which does an `album_tracks` call per album.

**Album Sampler (multi-playlist mode):** second submit button on the Block Mix page (`/spotify/album-sampler-blocks`) reuses the same selection grid. For each selected playlist it groups tracks by album, picks N *random* albums × X *random* songs each (defaults 3×3 = 9/playlist), applies the same 7-day `TrackHistory` cooldown as Block Mix (URI-based, falls back to full album if too few fresh tracks remain), concatenates, and dedupes preserving order. Block-Mix-only knobs (weights, pins, block size, repeats) are ignored in this mode.

**Plex audio filter:** only playlists with `playlistType == "audio"` and ≥ 20 tracks are shown (`PLEX_MIN_TRACKS = 20`).

**Cache fallback:** `get_cached_playlists()` serves stale DB cache on `SpotifyException` or timeout rather than showing an error page.

**Text Import matching:** `_search_line()` searches tracks (limit=5) and albums (limit=3) separately, scores each result by word-overlap similarity (`_name_sim`) against the query title, and returns candidates sorted by score. `_detect_list_type()` votes across all lines to determine whether the list is track-dominant, album-dominant, or mixed (≥60% threshold). `_bias_matches()` re-sorts each row so the dominant type leads — preventing LLM-generated track lists from expanding into full albums. `_search_line` / `_name_sim` now live in `spotify_service.py` (shared with `cli.py`) and are imported back into `app.py`.

**Feed Radar (`feed_service.py` + `FeedItem`):** watch a music blog, harvest new `Artist – Title` mentions, and hold Spotify matches in a review queue until you approve them into a playlist. `feed_service` keeps two responsibilities apart: fetch (side-effecting, shells out to the `firecrawl` CLI so it reuses `firecrawl login` — no API key threaded through Flask) and extract (pure, unit-tested in `test_feed_service.py`). Each source declares an extraction `mode`: `"anchors"` (cheap regex over markdown link anchors) or `"llm"` (Firecrawl structured extraction with a schema — slower/pricier but layout-agnostic). `/spotify/feed/poll` harvests → dedupes by `item_hash` (stable per source+line, so re-polling never re-queues) → runs the lines through `resolve_tracks()` (the *same* resolver as Text Import) → queues hits as `status="pending"` (dropping anything in `iced_ids("spotify")`) and non-matches as `status="miss"` for transparency. `/spotify/feed/resolve` bulk-adds selected pending items to a chosen playlist (re-checking the ice box at add time) or rejects them; handled items stay in the DB as `added`/`rejected` so they never re-queue. Sources are hardcoded for the prototype (`FEED_SOURCES`); a real version would store them in a table.

---

## Conventions / preferences

- Keep it simple — avoid over-engineering
- Both Spotify and Plex tools share the same patterns; changes to Block Mix logic usually apply to both
- All form inputs that feed API calls or loops should be bounds-checked
- `user_id = "local"` throughout — single-user personal app by design
- SQLite is fine; no plans to move to Postgres
- No CSRF protection — acceptable for localhost; warn before any server deployment
- **Spotify OAuth callback URI must use `127.0.0.1`, not `localhost`** — Spotify treats them as different origins and will reject the callback with a redirect_uri mismatch. Always use `http://127.0.0.1:5000/callback` in both the Spotify app dashboard and `.env`.


## TODO / Upcoming
- [ ] Create wirefunk GitHub account
- [ ] New repo under wirefunk (fresh, no history)
- [ ] Set local git config: user.name + user.email to wirefunk
- [ ] Copy project files over, initial commit as wirefunk
- [ ] Polish pass before r/truespotify post

## Agent skills

### mix / profile (Spotify curation pair)

`.claude/skills/mix/` builds and ships playlists from Cory's own library;
`.claude/skills/profile/` reads a *friend's* public playlists into a taste profile
saved to agent memory, which the mix skill then builds against. Both follow the same
split — a `*_helper.py` owning all plumbing, a `SKILL.md` owning the judgment — and
`profile_helper` imports `mix_helper._client()` so there is one Spotify auth path.
Run both with `PYTHONUTF8=1` from the repo root. Tests: `python -m pytest .claude/skills/`.

### Issue tracker

Issues live in the `CoryBerry/Spotify_Playlist_Magic` GitHub Issues (via the `gh` CLI). See `docs/agents/issue-tracker.md`.

### Triage labels

Default canonical labels — `needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context — `CONTEXT.md` + `docs/adr/` at the repo root. See `docs/agents/domain.md`.