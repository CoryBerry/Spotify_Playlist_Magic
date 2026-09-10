# profile.sample/ — template for a Spotify Playlist Magic overlay

This app separates **shared code** (this repo) from **your personal curation data**
(a `profile/` folder). `profile/` is gitignored and lives as its own **private** repo, so
the app can be shared publicly while your taste profile, playlist history and friends'
music profiles stay yours.

This `profile.sample/` is the **committed template**. To stand one up:

```bash
cp -r profile.sample profile          # create your overlay
cd profile && git init && git add -A && git commit -m "initial overlay"
gh repo create <you>/spotify-magic-profile-<ctx> --private --source=. --push
```

Then fill in `profile/CONTEXT.md` and `profile/playlists.md`, and run a sync (below).

## What lives here

| Path | Role | Written by |
|---|---|---|
| `CONTEXT.md` | **Live-read.** Who you are, what you like, hard vetoes, house rules for building mixes. | You |
| `playlists.md` | **Live-read.** Your pool cheat sheet — what each playlist is for and how much to trust it. | You |
| `notes/` | **Live-read.** Personal backlog and scratch notes (`TODO.md`, `IDEAS.md`, `ISSUES.md`). | You |
| `memory/` | **Mirror.** Copy of this project's agent memory. Backup only — `~/.claude/…/memory/` stays authoritative. | `profile-sync.ps1` |
| `backups/` | **Mirror.** `spotify_tools.sql` — a git-friendly dump of the app DB (tags, ice box, build history, usage). | `profile-sync.ps1` |
| `cache/` | **Mirror.** Friend taste-profile pulls from the `profile` skill. Regenerable, but slow to rebuild. | `profile-sync.ps1` |

The distinction matters: files you author are the point of the overlay; the mirrors exist so
a dead machine costs you nothing. Never hand-edit a mirror — the next sync overwrites it.

## Syncing

```powershell
.\profile-sync.ps1            # dump DB + mirror memory/cache, commit + push if anything changed
.\profile-sync.ps1 -NoPush    # local commit only
```

It commits only when something actually changed, so it's safe to run on a schedule.

## Restoring on a new machine

```bash
git clone <your-app-repo> && cd Spotify_Playlist_Magic
git clone <your-private-overlay-repo> profile
pip install -r requirements.txt
python -c "import sqlite3,io; sqlite3.connect('instance/spotify_tools.db').executescript(io.open('profile/backups/spotify_tools.sql',encoding='utf-8').read())"
cp -r profile/memory/* ~/.claude/projects/<project-slug>/memory/
```

Secrets stay in `.env` at the repo root (gitignored), **never** in the overlay.
