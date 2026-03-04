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

---

## Models

```python
PlaylistTag       # user-applied tags on Spotify playlists
PlaylistCache     # 1-hour cache of Spotify playlist list
CreatedPlaylist   # history of every Block Mix / Album Blast created
```

---

## Key routes

| Route | What it does |
|---|---|
| `/spotify/playlists` | Block Mix — select playlists, build interleaved playlist |
| `/spotify/album-blaster` | Album Blaster — pick tracks, blast full albums into new playlist |
| `/spotify/manage` | Manage playlists — filter, tag, delete, toggle visibility |
| `/spotify/stats/<id>` | Track count, runtime, unique artists for a playlist |
| `/recently-created` | History of created playlists with alive/deleted status |
| `/plex/playlists` | Plex Block Mix |
| `/plex/album-blaster` | Plex Album Blaster |
| `/plex/stats/<key>` | Plex playlist stats |

---

## Conventions / preferences

- Keep it simple — avoid over-engineering
- Both Spotify and Plex tools share the same patterns; changes to Block Mix logic usually apply to both
- All form inputs that feed API calls or loops should be bounds-checked
- `user_id = "local"` throughout — single-user personal app by design
- SQLite is fine; no plans to move to Postgres
