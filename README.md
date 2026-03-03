# Spotify Tools

A personal Flask web app for building and managing Spotify playlists in ways the official app doesn't support.

---

## Tools

### 🎵 Block Mix
Select 2 or more playlists and build a new playlist where songs rotate in blocks — 4 from one genre, then 4 from another, like the old Pandora station experience. Supports configurable block size, repeats, and full randomization. Cover art is automatically seeded from across your selected playlists.

### 💥 Album Blaster
Browse any playlist, pick tracks you like, and blast their full albums into a brand new playlist. Great for rediscovering albums you forgot you loved.

### 🗂️ Manage Playlists
Browse all your playlists in a filterable, sortable table. Preview tracks, toggle public/private visibility, and bulk delete playlists you no longer need.

### 🏷️ Playlist Tags
Tag your playlists with free-form labels like "chill", "office", or "instrumental". Tags appear in Block Mix as filter buttons so you can quickly narrow down to the right vibe without hunting through hundreds of playlists.

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

## Known Limitations

- **Mood filter removed** — Spotify restricted the `/audio-features` endpoint for new developer apps in late 2024. The mood/vibe filtering feature that would have used it has been disabled.
- **Playlists with fewer than 20 tracks** are hidden from Block Mix (not enough songs to be useful for block rotation).
- Spotify's API returns a maximum of 50 playlists per request — the app paginates automatically, but loading 500+ playlists takes a moment. Results are cached locally for 1 hour.

---

## Tech Stack

- [Flask](https://flask.palletsprojects.com/) — web framework
- [Flask-SQLAlchemy](https://flask-sqlalchemy.palletsprojects.com/) + SQLite — playlist tag storage and cache
- [Spotipy](https://spotipy.readthedocs.io/) — Spotify Web API client
- [Bootstrap 5](https://getbootstrap.com/) — UI

---

## PlexAmp Version — Approaches

If you run a local Plex Media Server, a parallel version of these tools is very achievable. The [plexapi](https://python-plexapi.readthedocs.io/) Python library makes it straightforward. Here are a few ways to approach it:

---

### Option A — Add Plex as a second section in this same app *(recommended)*

Add Plex routes alongside the existing Spotify routes (`/plex/playlists`, `/plex/build`, etc.) in the same Flask app. Auth is just a token in `.env` — no OAuth flow needed.

**Pros:** One app, one server, shared UI and tagging system. Users with both Spotify and Plex get everything in one place.
**Cons:** `app.py` grows larger; Spotify and Plex routes need to stay clearly separated.

```python
# .env additions
PLEX_URL=http://localhost:32400
PLEX_TOKEN=your_plex_token

# New helper
from plexapi.server import PlexServer
def get_plex():
    return PlexServer(os.environ["PLEX_URL"], os.environ["PLEX_TOKEN"])
```

---

### Option B — Separate app, shared concepts

Fork this repo into a `plex-tools` project and replace the Spotipy calls with plexapi equivalents. The Block Mix and Album Blaster logic is almost identical — just different API calls.

**Pros:** Clean separation, easier to open-source independently.
**Cons:** Duplicate UI code, two servers to run.

---

### Option C — Abstract provider interface

Define a common interface (`get_playlists()`, `get_tracks()`, `create_playlist()`, `add_tracks()`) and implement it for both Spotify and Plex. The tools then work with whichever provider is configured.

**Pros:** Elegant, truly reusable, one set of templates.
**Cons:** More upfront design work; the two APIs have different data shapes that need normalization.

---

### Plex API quick reference

```python
from plexapi.server import PlexServer

plex  = PlexServer("http://localhost:32400", "YOUR_TOKEN")
music = plex.library.section("Music")

# Playlists
playlists = plex.playlists()
tracks    = playlist.items()

# Search
tracks = music.searchTracks(filters={"album.title": "Abbey Road"})

# Create playlist
plex.createPlaylist("Block Mix", items=track_list)
```

Your Plex token can be found by opening Plex in a browser, playing any item, and inspecting the network request URL — it will contain `X-Plex-Token=...`.

---

## License

MIT — do whatever you want with it.
