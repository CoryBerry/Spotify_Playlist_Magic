# Spotify Tools

A personal Flask web app for building and managing Spotify playlists in ways the official app doesn't support. Optionally also works with Plex.

---

## Tools

### 🎵 Block Mix
Select 2 or more playlists and build a new playlist where songs rotate in blocks — 4 from one genre, then 4 from another, like the old Pandora station experience. Supports configurable block size, repeats, weighted mixing, and a pinned playlist that recurs at a set interval. Cover art is automatically seeded from across your selected playlists.

### 💥 Album Blaster
Browse any playlist, pick tracks you like, and blast their full albums into a brand new playlist. Great for rediscovering albums you forgot you loved.

### 🗂️ Manage Playlists
Browse all your playlists in a filterable, sortable table. Preview tracks, toggle public/private visibility, and bulk delete playlists you no longer need. *(Spotify only)*

### 🏷️ Playlist Tags
Tag your playlists with free-form labels like "chill", "office", or "instrumental". Tags appear in Block Mix as filter buttons so you can quickly narrow down to the right vibe without hunting through hundreds of playlists. *(Spotify only)*

---

## Setup

### 1. Clone and install dependencies

```bash
git clone https://github.com/CoryBerry/claudone.git
cd claudone
pip install -r requirements.txt
```

### 2. Create a Spotify Developer app

1. Go to [developer.spotify.com/dashboard](https://developer.spotify.com/dashboard)
2. Create a new app
3. Add `http://127.0.0.1:5000/callback` as a Redirect URI and save
   - Note: Use `127.0.0.1`, **not** `localhost` — Spotify treats them differently

### 3. Configure your environment

```bash
cp .env.example .env
```

Edit `.env` and fill in your Spotify credentials:

```
SPOTIFY_CLIENT_ID=your_client_id
SPOTIFY_CLIENT_SECRET=your_client_secret
SPOTIFY_REDIRECT_URI=http://127.0.0.1:5000/callback
SECRET_KEY=any_random_string
```

### 4. Run the app

```bash
flask run
```

Open [http://127.0.0.1:5000](http://127.0.0.1:5000) and connect your Spotify account.

---

## Plex Setup (optional)

If you run a local Plex Media Server, Block Mix and Album Blaster also work with your Plex music library — no Spotify account needed for that half.

### 1. Install the Plex dependency

```bash
pip install plexapi
```

### 2. Get your Plex token

Open Plex in a browser, play any item, and open the browser dev tools → Network tab. Look at any request URL — it will contain `X-Plex-Token=...`. Copy that value.

### 3. Add Plex to your `.env`

```
PLEX_URL=http://localhost:32400
PLEX_TOKEN=your_plex_token
```

The Plex section of the nav will appear automatically once both values are set. If they're missing, the Plex links redirect to a "not configured" page instead of erroring.

---

## Known Limitations

- **Mood filter removed** — Spotify restricted the `/audio-features` endpoint for new developer apps in late 2024. The mood/vibe filtering feature that would have used it has been disabled.
- **Playlists with fewer than 20 tracks** are hidden from Block Mix (not enough songs to be useful for block rotation).
- Spotify's API returns a maximum of 50 playlists per request — the app paginates automatically, but loading 500+ playlists takes a moment. Results are cached locally for 1 hour.

---

## Tech Stack

- [Flask](https://flask.palletsprojects.com/) — web framework
- [Flask-SQLAlchemy](https://flask-sqlalchemy.palletsprojects.com/) + SQLite — playlist tag storage and cache
- [Spotipy](https://spotipy.readthedocs.io/) — Spotify Web API client
- [plexapi](https://python-plexapi.readthedocs.io/) — Plex Media Server client (optional)
- [Bootstrap 5](https://getbootstrap.com/) — UI

---

## License

MIT — do whatever you want with it.
