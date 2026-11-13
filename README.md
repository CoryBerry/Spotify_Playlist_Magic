# Crate

A personal Flask web app for building and managing Spotify playlists in ways the official app doesn't support. Optionally also works with Plex.

---

## Tools

### 🎵 Block Mix

Select 2 or more playlists and build a new playlist where songs rotate in blocks — 4 from one genre, then 4 from another, like the old Pandora station experience. Supports configurable block size, repeats, weighted mixing, and a pinned playlist that recurs at a set interval. Cover art is automatically seeded from across your selected playlists.

### 💥 Album Blaster

Browse any playlist, pick tracks you like, and blast their full albums into a brand new playlist. Great for rediscovering albums you forgot you loved.

### 🎲 Album Sampler

Turn a big "full albums" playlist into a bite-sized sampler. Point it at a playlist, choose how many songs to pull per album (3 by default) and how many albums to include, and it takes the first few songs from each album in order into a new playlist. Perfect for skimming a genre/decade collection without playing every album end to end. _(Spotify only)_

There's also a **Build Album Sampler** button on the Block Mix page: select several playlists and it pulls a few random songs from a few random albums of each (3 albums × 3 songs = 9 per playlist by default), honoring the same 7-day cooldown as Block Mix. Great for a fresh cross-genre sampler built from your usual blocks. _(Spotify only)_

### 🗂️ Manage Playlists

Browse all your playlists in a filterable, sortable table. Preview tracks, toggle public/private visibility, and bulk delete playlists you no longer need. Sort by least used or oldest use, and filter to playlists you've never mixed to find forgotten gems. _(Spotify only)_

### 📄 Text Import

Paste a plain-text list of albums or tracks — one per line — and the app builds a Spotify playlist from it. Works great with LLM-generated suggestions. Supports `Artist - Album` and `Artist - Track` formats (auto-detected). Two modes: **Trust It** creates the playlist immediately using the best Spotify match per line, or **Manual Select** shows the top candidates for each line so you can correct any mismatches before creating. _(Spotify only)_

### 📡 Feed Radar

Watch your favorite music blogs and let the app surface new tracks for you. Point it at a source (Gorilla vs. Bear, Pitchfork Best New Tracks, Line of Best Fit), hit **Check now**, and it scrapes the page, pulls out every `Artist – Title` mention, and matches each against Spotify using the same resolver as Text Import. Confident matches land in a review queue with a match-confidence score; you tick the ones you want and add them to any playlist in one click. Anything on ice is skipped automatically, and lines that didn't resolve are shown separately so nothing silently vanishes. Fast sources use a lightweight regex; trickier layouts use AI extraction (slower, but reads any page). Requires the Firecrawl CLI (see setup below). _(Spotify only, prototype)_

### 🏷️ Playlist Tags

Tag your playlists with free-form labels like "chill", "office", or "instrumental". Tags appear in Block Mix as filter buttons so you can quickly narrow down to the right vibe without hunting through hundreds of playlists. _(Spotify only)_

### 📁 Folders

Group playlists into folders on the Manage page — move one at a time, or select a batch and move
them together. Filter the list down to a single folder, or sort by folder to see everything
clustered. A playlist lives in exactly one folder, and a folder disappears once the last playlist
leaves it.

> **Note:** these folders are local to this app. Spotify's Web API has no folder support at all —
> folders exist only inside the official client and can't be read or written by third-party apps —
> so filing something here won't move it in Spotify itself. _(Spotify only)_

### 🔤 Generated Playlist Names

Since Spotify can't show this app's folders, the names do the organizing. Every playlist the app
creates is named `[Kind] Title — Nov 12 '26`:

- **`[Kind]` first** — `[Block]`, `[Sampler]`, `[Blast]`, `[Import]` — so each tool's output
  groups together when you sort your Spotify library alphabetically (the `mix` skill uses `[Mix]`).
- **Title next** — whatever you type in the name box, or, if blank, what the playlist actually
  holds: its sources (`Chill Selects & Easy Drops`, `Indie Selects +3`) or its albums. Never just
  the weekday. These are the words a search will find.
- **Date last** — least useful for searching, so it goes at the end.

A shuffled copy keeps the original's name up front (`Chill Selects — Shuffled Nov 12 '26`) so it
sorts right beside it. Block Mix hides its own earlier builds (old- and new-style names) from the
source grid.

### 📊 Playlist Stats

See track count, total runtime, unique artist count, and a top-10 artist breakdown for any playlist. Also shows how many times it's been used in a Block Mix build, plus a full tracklist with a 🧊 snowflake on every row so you can freeze a track into the Ice Box without leaving the page.

### 🔀 Randomize

From any playlist's Stats page, shuffle the whole tracklist with a fresh OS-entropy random seed (surfaced in the confirmation so the shuffle is auditable). Two options: **Shuffle this playlist** reorders it in place (only for playlists you own), or **Create a shuffled copy** leaves the original alone and builds a new private `… — Shuffled <date>` playlist. It's a pure reorder — nothing is added or removed, and cooldown / ice box / Block Mix knobs don't apply. _(Spotify only)_

### 🧊 Ice Box

A manual, long-lived exclusion list — a **hard** never-list or a timed freeze — shared with the `mix` skill (both read and write the same `track_ice` table, so a freeze in either place is honored by the next build immediately). Unlike the automatic cooldown, an iced track *never* enters a build, even as a small-pool fallback.

- **Ice Box page** (`/spotify/ice-box`) lists every currently-frozen track with its reason and thaw date (or "never"), each with a **Thaw** button.
- **Freeze an artist or track** search resolves any Spotify artist or title and freezes it for **Never / 12 / 6 / 3 months** with an optional reason — even tracks that aren't in any recent build.
- **Freeze a whole artist** (🎤) — for the "good, but I keep not wanting them" artists. Every track they're credited on (features included) is hard-excluded from Block Mix, Album Blast, Album Sampler, Text Import, and Feed Radar. Matched by Spotify artist ID, so same-named bands never collide. _(Spotify builds only for now — Plex and the `mix` skill still honor track freezes only.)_
- The same 🧊 snowflake dropdown appears on the **build done page** tracklist and the **Playlist Stats** tracklist, and a 🧊 shortcut on each **Manage** row jumps to that playlist's tracklist. Timed ice thaws itself on the same schedule as the cooldown.

### 🧠 Recall

Practice turning artists you *recognize* into names you can *bring up* — so the next time a band comes on and someone says "I love these guys," the two artists you'd recommend are on the tip of your tongue instead of arriving two minutes after you leave.

- **Ready-to-recommend set** — a small rotating set (default 30, configurable 15–50) built from your real listening. Every artist shows *why* it's there ("played in 9 of the last 13 weeks", "in 4 of your playlists"). Pin, dismiss, snooze, or mark **nope** ("I know them, I just don't recommend them") — refreshes never overwrite those choices.
- **Hooks and neighbors** — give an artist a one-line hook, and link artists ("when Remember Sports comes up → Lime Garden"). Crate *suggests* neighbors from your own playlists and Last.fm's similar artists, but only links you accept become practice material.
- **Short practice sessions** — 2–3 minutes, up to three conversational prompts ("Someone says they like X. Name two artists you'd recommend."). Answers stay hidden until you try, ask for a hint, or reveal. Rate yourself — *came easily / took a moment / couldn't recall* — and that drives when each prompt comes back. No streaks, no scores.
- **Two surfaces, one engine** — the `/recall` page (dashboard, per-artist detail, practice card) and `python cli.py recall …` in the terminal share one engine and one database — curate or practice in either and the other sees it.

Needs a Last.fm account — see [Recall Setup](#recall-setup-optional).

### 🕓 Recently Created

A history of every Block Mix and Album Blast you've built, with creation date, track count, and build time. Shows whether each playlist is still alive or has been deleted. You can remove dead entries in bulk or delete a playlist directly from this page. Also displays a live cooldown summary — how many tracks are on ice, when they warm up, and how many have thawed over time.

### ⚙️ Settings

Configure the track cooldown window (default: 7 days) and how many times a track must be used before it goes on ice (default: 2 plays). Also includes a **Thaw Everything Now** button to immediately clear all track history and make every track available for the next build.

---

## Setup

### 1. Clone and install dependencies

Requires **Python 3.9 or newer** (developed and tested on 3.12), with `python` and `pip`
available on your PATH.

```bash
git clone https://github.com/CoryBerry/Spotify_Playlist_Magic.git
cd Spotify_Playlist_Magic
```

A virtual environment is optional but recommended, so the app's packages stay out of your
system Python:

```bash
python -m venv .venv

# Windows (PowerShell)
.venv\Scripts\Activate.ps1

# macOS / Linux
source .venv/bin/activate
```

Then install the dependencies:

```bash
pip install -r requirements.txt
```

This also installs `plexapi`, so the optional Plex tools work with no extra step.

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

> **Plex note:** `PLEX_URL` and `PLEX_TOKEN` come commented out in the example file — leave
> them that way unless you actually run Plex. The app treats *any* non-empty value as "Plex is
> configured", so a leftover placeholder gives you Plex nav links that lead nowhere.

### 4. Run the app

```bash
flask run
```

Open [http://127.0.0.1:5000](http://127.0.0.1:5000) and connect your Spotify account.

---

## Plex Setup (optional)

If you run a local Plex Media Server, Block Mix and Album Blaster also work with your Plex music library — no Spotify account needed for that half.

### 1. Get your Plex token

Open Plex in a browser, play any item, and open the browser dev tools → Network tab. Look at any request URL — it will contain `X-Plex-Token=...`. Copy that value.

### 2. Add Plex to your `.env`

Uncomment the two Plex lines (drop the leading `#`) and fill in your token:

```
PLEX_URL=http://localhost:32400
PLEX_TOKEN=your_plex_token
```

The Plex section of the nav will appear automatically once both values are set. If they're missing, the Plex links redirect to a "not configured" page instead of erroring.

---

## Feed Radar Setup (optional)

The **📡 Feed Radar** tool scrapes music blogs through the [Firecrawl](https://www.firecrawl.dev/) CLI, so that half of the app only works once Firecrawl is installed and logged in.

### 1. Install the Firecrawl CLI

```bash
npm install -g firecrawl-cli
```

### 2. Log in once

```bash
firecrawl login
```

The app reuses this login — no API key goes in `.env`. If Firecrawl isn't installed or authenticated, the rest of the app is unaffected; Feed Radar just shows a clear "run `firecrawl login`" message when you hit **Check now**. Note that the AI-extraction sources take ~2 minutes and cost more Firecrawl credits per check than the fast regex sources.

---

## Recall Setup (optional)

**🧠 Recall** reads your listening history from [Last.fm](https://www.last.fm/), so it needs a scrobbling account and a free API key.

### 1. Add Last.fm to your `.env`

```
LASTFM_API_KEY=your_key      # free: https://www.last.fm/api/account/create
LASTFM_USER=your_username
```

Your playlists come from the app's local playlist cache — open **Manage** once so it's populated. Recall makes no Spotify calls of its own.

### 2. Your first session

1. **Refresh.** Click **Refresh** on `/recall` (or `python cli.py recall refresh`). The first one takes about 2 minutes — it checks Last.fm listener counts so household names (The Beatles tier) are left out of the set; nobody needs practice to bring those up. Later refreshes only top up a few slots.
2. **Accept the starter pair.** Crate seeds one suggested link from the story that started this feature: *Remember Sports → Lime Garden, Hop Along*. Accept it (or reject it), then accept or reject a few of the suggested neighbors. Add your own with `recall link "Artist A" "Artist B"` or from any artist's page.
3. **Practice.** Hit **Start practice** on the dashboard (or `python cli.py recall practice`). Answer, hint, or reveal — then rate yourself.
4. **Check in.** `python cli.py recall stats` (or the dashboard) shows recent recalls and the prompts giving you trouble. Remembered one in a real conversation? Log it: `python cli.py recall note "recommended Hop Along at the brewery"`.

Run `python cli.py recall --help` for the full command list (`pin`, `snooze`, `hook`, `alias`, `context`, `config`, …).

**What it uses:** Last.fm weekly artist charts (last 13 weeks), top artists (12-month and all-time), top tracks (for suggested hooks), loved tracks, global listener counts (to skip household names), and similar artists (for suggested neighbors) — plus which of *your own* playlists each artist appears in. Mixes Crate generated don't count as evidence. Spotify listening history isn't used (the API doesn't expose it).

**Not built yet:** an association graph view, a weekly ready-to-recommend digest, playlists that reinforce the active set, prompts tied to what you just heard, and reminders.

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
