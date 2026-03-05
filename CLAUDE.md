# CLAUDE.md — Spotify Playlist Magic

> Briefing file for Claude Code. Read this before touching anything.

---

## What this is

A Flask web app for building and managing Spotify and Plex playlists in ways the official apps don't support. Originally a Python learning project, now a real shareable tool.

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
app.py              ← all routes, models, helpers
templates/
  base.html         ← shared layout (Bootstrap, nav, cache footer)
  spotify_*.html    ← Spotify tool pages
  plex_*.html       ← Plex tool pages
  recently_created.html
instance/
  spotify_tools.db  ← SQLite DB (auto-created, don't commit)
IDEAS.md            ← feature backlog
```

> **Note:** `README.md` is the source of truth for user-facing feature descriptions. CLAUDE.md may drift — cross-check README when in doubt, and keep both in sync when making structural changes.

---

## Models

```python
PlaylistTag       # user-applied tags on Spotify playlists (unique per playlist+tag)
PlaylistCache     # 1-hour cache of Spotify playlist list (falls back to stale on timeout)
CreatedPlaylist   # history of every Block Mix / Album Blast created (alive/checked_at, gen_seconds, track_count)
PlaylistUsage     # use_count + last_used per playlist+provider — drives "most used" sort
TrackHistory      # track_id + used_at — 7-day cooldown pool to avoid replaying recent tracks
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
| `/spotify/manage` | Manage playlists — filter, tag, delete, toggle visibility |
| `/spotify/tag/add` | POST JSON — add tag to playlist |
| `/spotify/tag/remove` | POST JSON — remove tag from playlist |
| `/spotify/tags/all` | GET JSON — all tags (autocomplete) |
| `/spotify/toggle-visibility/<id>` | POST — toggle playlist public/private |
| `/spotify/delete` | POST — bulk unfollow playlists |
| `/spotify/text-import` | Text Import — paste/upload a text list of albums or tracks |
| `/spotify/text-import/preview` | POST — parse text, search Spotify, Trust It or show manual review |
| `/spotify/text-import/build` | POST — create playlist from manual-select form |
| `/spotify/stats/<id>` | Track count, runtime, top artists, usage count |
| `/spotify/preview/<id>` | GET JSON — lazy-load first 5 tracks (tooltip) |
| `/spotify/cache/refresh` | Force invalidate playlist cache |
| `/recently-created` | History of created playlists with alive/deleted status |
| `/recently-created/remove/<id>` | POST — delete from provider + remove from history |
| `/recently-created/clear-dead` | POST — purge dead entries from DB |
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

**Mood presets (`MOOD_PRESETS`):** code is present but disabled. Spotify restricted `/audio-features` for new apps in late 2024. Do not re-enable without verifying API access.

**Plex audio filter:** only playlists with `playlistType == "audio"` and ≥ 20 tracks are shown (`PLEX_MIN_TRACKS = 20`).

**Cache fallback:** `get_cached_playlists()` serves stale DB cache on `SpotifyException` or timeout rather than showing an error page.

**Text Import matching:** `_search_line()` searches tracks (limit=5) and albums (limit=3) separately, scores each result by word-overlap similarity (`_name_sim`) against the query title, and returns candidates sorted by score. `_detect_list_type()` votes across all lines to determine whether the list is track-dominant, album-dominant, or mixed (≥60% threshold). `_bias_matches()` re-sorts each row so the dominant type leads — preventing LLM-generated track lists from expanding into full albums.

---

## Conventions / preferences

- Keep it simple — avoid over-engineering
- Both Spotify and Plex tools share the same patterns; changes to Block Mix logic usually apply to both
- All form inputs that feed API calls or loops should be bounds-checked
- `user_id = "local"` throughout — single-user personal app by design
- SQLite is fine; no plans to move to Postgres
- No CSRF protection — acceptable for localhost; warn before any server deployment
- **Spotify OAuth callback URI must use `127.0.0.1`, not `localhost`** — Spotify treats them as different origins and will reject the callback with a redirect_uri mismatch. Always use `http://127.0.0.1:5000/callback` in both the Spotify app dashboard and `.env`.
