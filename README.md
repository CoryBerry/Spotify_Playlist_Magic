# Spotify Playlist Magic

A personal Flask web app for building and managing Spotify playlists in ways the official app doesn't support. Optionally also works with Plex.

---

## Tools

### 🎵 Block Mix

Select 2 or more playlists and build a new playlist where songs rotate in blocks — 4 from one genre, then 4 from another, like the old Pandora station experience. Supports configurable block size, repeats, weighted mixing, and a pinned playlist that recurs at a set interval. Cover art is automatically seeded from across your selected playlists.

### 💥 Album Blaster

Browse any playlist, pick tracks you like, and blast their full albums into a brand new playlist. Great for rediscovering albums you forgot you loved.

### 🗂️ Manage Playlists

Browse all your playlists in a filterable, sortable table. Preview tracks, toggle public/private visibility, and bulk delete playlists you no longer need. Sort by least used or oldest use, and filter to playlists you've never mixed to find forgotten gems. _(Spotify only)_

### 📄 Text Import

Paste a plain-text list of albums or tracks — one per line — and the app builds a Spotify playlist from it. Works great with LLM-generated suggestions. Supports `Artist - Album` and `Artist - Track` formats (auto-detected). Two modes: **Trust It** creates the playlist immediately using the best Spotify match per line, or **Manual Select** shows the top candidates for each line so you can correct any mismatches before creating. _(Spotify only)_

### 🏷️ Playlist Tags

Tag your playlists with free-form labels like "chill", "office", or "instrumental". Tags appear in Block Mix as filter buttons so you can quickly narrow down to the right vibe without hunting through hundreds of playlists. _(Spotify only)_

### 📊 Playlist Stats

See track count, total runtime, unique artist count, and a top-10 artist breakdown for any playlist. Also shows how many times it's been used in a Block Mix build.

### 🕓 Recently Created

A history of every Block Mix and Album Blast you've built, with creation date, track count, and build time. Shows whether each playlist is still alive or has been deleted. You can remove dead entries in bulk or delete a playlist directly from this page. Also displays a live cooldown summary — how many tracks are on ice, when they warm up, and how many have thawed over time.

### ⚙️ Settings

Configure the track cooldown window (default: 7 days) and how many times a track must be used before it goes on ice (default: 2 plays). Also includes a **Thaw Everything Now** button to immediately clear all track history and make every track available for the next build.

---

## Setup

### 1. Clone and install dependencies

```bash
git clone https://github.com/CoryBerry/Spotify_Playlist_Magic.git
go to the "Spotify_Playlist_Magic" folder or where you installed it
pip install -r requirements.txt
     - You must have Python installed and properly pathed.
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

## Security Note for Server Deployments

This app is designed to run **locally** — just you, on your own machine. If you deploy it to a public or shared server, be aware that it does not implement CSRF protection. That means a malicious website could potentially trigger playlist actions (create, delete, tag) on behalf of a logged-in user by submitting forged requests.

For a personal localhost setup this isn't a real concern, but if you're hosting it for others, add CSRF protection before going live — [Flask-WTF](https://flask-wtf.readthedocs.io/) makes this straightforward.

---

## How Block Mix works

1. You pick two or more playlists and configure a block size (2–10 tracks) and number of repeats (1–10 cycles).
2. The app builds a rotating cycle of your selected playlists. Optional **weights** let you make one playlist appear more frequently than others.
3. Optionally pin one playlist to recur every N blocks (good for a "home base" playlist woven through the mix).
4. Tracks are cooled down by **song ID** — if the same song appears in multiple source playlists, it still only counts once. Tracks used more than the configured limit (default: 2 plays within 7 days) are excluded from new builds. If a playlist's fresh pool drops below the block size, the cooldown is ignored for that playlist so the build doesn't fail. Expired tracks are automatically thawed when you visit Block Mix or Recently Created, and a banner tells you how many were freed.
5. Duplicates are removed while preserving block order. If "Cover Art" seeding is on, one track from each selected playlist is prepended so the playlist thumbnail represents each source.

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
