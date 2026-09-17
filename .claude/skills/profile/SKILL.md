---
name: profile
description: Build a music-taste profile of another Spotify user (a friend, family member, coworker) from their public playlists, and save it to memory. Use when the user asks to "build a profile for X", "what's X into", shares someone's Spotify user link, or wants to know where their taste overlaps with someone else's before making them a mix.
---

# profile — read a friend's taste from their public playlists

Turn "build a profile for my friend X" into a durable memory file describing what
they actually listen to, how current it is, and where it overlaps with Cory's own
library — so a later `mix` request for that person starts from evidence instead of
guesswork.

All plumbing lives in `profile_helper.py` (same directory). Run from the repo root
with `PYTHONUTF8=1` (playlist and track names carry emoji and accents that crash the
default Windows console codec):

```
PYTHONUTF8=1 python .claude/skills/profile/profile_helper.py <command> ...
```

Auth is imported from the sibling `mix` skill's `mix_helper._client()`, so it reuses
the Flask app's cached `.cache` token. If it reports no cached token, tell the user to
log into the app once, then retry. **Every command is read-only** — this skill never
writes to Spotify.

## The loop

### 1. Pull

The user id is the `/user/<id>` part of their profile URL.

```
PYTHONUTF8=1 python .claude/skills/profile/profile_helper.py pull gregulate
```

Prints every public playlist with track count, **local-file count**, and the
`added_at` range, marking owned (`.`) vs followed (`f`). Cached to
`.profile_cache/<user>.json`; re-run with `--no-cache` to refresh.

### 2. Stats

```
PYTHONUTF8=1 python .claude/skills/profile/profile_helper.py stats gregulate
PYTHONUTF8=1 python .claude/skills/profile/profile_helper.py stats dukejansen --pool "nostalgia"
```

Recency verdict, popularity distribution, decade spread, top artists overall and
per pool. Defaults to **owned playlists only** — a followed playlist is someone
else's curation. `--include-followed` when you want the fuller picture.

### 3. Overlap

```
PYTHONUTF8=1 python .claude/skills/profile/profile_helper.py overlap dukejansen
PYTHONUTF8=1 python .claude/skills/profile/profile_helper.py overlap dukejansen gregulate
```

One user: shared artists vs Cory's library. Two or more: also the **N-way
intersection**, ranked by *weakest link* — an artist everyone has beats one that's
huge for a single person and incidental to the rest. That ranking is the whole
ballgame for a group mix; a raw shared-artist list will put someone's #1 at the top
even when the others have a single incidental track.

Cory's side is read from `.mix_cache`, which only covers playlists the `mix` skill
has actually pulled. So "absent from Cory" is weak evidence, and the counts are
"how many of Cory's *pulled* playlists", not his whole library.

## Reading the output — the judgment the helper can't do

- **Check recency before believing anything else.** A library frozen years ago
  describes who they *were*. Greg's public playlists stop dead at 2021; profiling him
  as current would have been simply wrong. Say so in the profile, out loud.
- **Median popularity is the deep-cut read.** Median under ~25 means popularity is a
  negative signal for them and Cory's normal deep-cut default is right. Near 0 across
  thousands of tracks is a real finding, not a data error.
- **A bimodal histogram is a question, not an answer — go look at what's actually in the
  low bucket.** The `POPULAR / hits listener` verdict is computed from the median alone,
  so any big spike under popularity 10 sitting beneath a healthy median is unexplained
  until you list it. Two profiles failed here in *opposite* directions:
  - **Jeff** — 50 tracks at 0-9 looked deep-cut, but printing them showed canonical songs
    on bad pressings (The Breeders "Cannonball" at pop 1, Dead Kennedys "Holiday in
    Cambodia" at 0, "Wagon Wheel" at 2) plus one whole-album add (14 Thievery Corporation
    tracks, all of *Radio Retaliation*). **Artifact.** The median was telling the truth.
  - **Holly** — 465 tracks under 10 (317 at exactly 0, 18% of her library) were all real
    obscurities, and whole lanes of hers sit down there (honky-tonk median 38, instrumental
    32). Calling her a hits listener on the median buried that, and Cory corrected it.
    **Real.** The median was hiding half of her.

  So print the low bucket before you write a word about depth:
  ```
  PYTHONUTF8=1 python -c "import json;d=json.load(open('.profile_cache/<user>.json',encoding='utf-8'));[print(p['name'],'|',', '.join(t['artists']),'-',t['name'],'|',t['release'][:4],'pop',t['pop']) for p in d['playlists'] for t in p['tracks'] if t['pop']<10]"
  ```
  Recognizable songs, reissue-era release dates, or many tracks sharing one album mean
  **artifact** — collapse album-adds and trust the median. Unfamiliar names spread across
  many playlists mean **real** — and then check the *per-pool* medians, because depth is
  usually lane-dependent rather than global. Say which it is in the profile, with the
  evidence, so the next mix doesn't inherit a guess.
- **Depth can be per-lane, and the "How to apply" line should say so.** "Invert the
  deep-cut default" is one global switch and it's often too blunt — Holly wants hits in
  her singalong pools and genuine obscurity in her honky-tonk and instrumental ones.
- **A taste read is not automatically the brief.** What someone's library holds and what
  Cory wants a mix *for them* to do are different questions. Jeff's library is canonical,
  but his mixes are a discovery channel on Cory's recommendations — so "he likes hits"
  was true about the data and wrong as build guidance. **Ask what the mix is for** when
  the profile is about to constrain the picks.
- **A playlist holding one artist's whole discography is usually personal, not taste.**
  Jason keeps four playlists covering every Naked Jane release — because *he is*
  Naked Jane. Don't fold that into the artist counts. **Ask rather than infer**; this
  was guessed at once and had to be corrected.
- **Named/dated playlists say more than big dumps.** `2013 02 numbers` (every song
  title containing a number, ordered numerically) reveals a curator; a 1,400-track
  `Starred` dump reveals an import. Weight the deliberate lists.
- **Auto-generated lists are a seed, not a lane.** "Similar to X", "Quick playlist by
  magicplaylist.co" reflect one seed the user picked — worth a line, not a genre claim.
- **Look for the personal thread.** Venue names, one local artist appearing dozens of
  times, a city — Jason's `tori's birthday banger at swan dive` plus 33 Abram Shook
  tracks says Austin. That kind of detail makes a profile useful.
- **Don't name lanes the data doesn't support.** Three incidental tracks is not a lane.

## Traps that will otherwise cost a session

**Local files look like empty playlists.** Old playlists hold `spotify:local:` entries
with no track object, so a naive pull returns *zero tracks* and you conclude the
playlist is empty. Jason's five biggest playlists are like this — ~1,300 songs, and
they are the bulk of his taste data. Greg's `dnbubstep` is 575 of 697. `pull` recovers
artist/album/title from the URI itself and reports the count in the `local` column.
They're unplayable: to use one in a mix, run `resolve-local` first.

```
PYTHONUTF8=1 python .claude/skills/profile/profile_helper.py resolve-local dukejansen --limit 40 --out picks.txt
```

Matches are top search hits, so **skim the printed mapping before shipping them**.
The artist is verified (a mismatch is reported `MISS` rather than silently accepted)
but the *track* can still land on a different cut of the right artist.

**Blends and algorithmic playlists cannot be read.** Any id starting `37i9dQZF1E`
(Blend, Discover Weekly, Release Radar, Daily Mix) 404s on the API — restricted for
apps since late 2024, same wall as the disabled `/audio-features` noted in CLAUDE.md
— and the web page is behind reCAPTCHA plus a login gate. `pull` flags these rather
than failing.

**The workaround, when a Blend matters:** ask Cory to copy it into a normal owned
playlist in the Spotify app. He already does this (`Natalie and Cory Mega Blend`,
`Me + Kids Blend`), and a copy under his account reads fine. Worth asking for when
the friend's own library is stale — the Blend may be the only current signal.
Remember it mixes *both* accounts, so attribute carefully rather than reading it as
pure friend-taste (see [[kids-music-profile]]'s caveat).

**A freshly created playlist won't resolve as a `mix --source`.** The app's
`playlist_cache` has a 1-hour TTL, so a playlist made minutes ago isn't in it yet and
the source token aborts the build. Either wait for the refresh or drop that one credit.

## Save it to memory

A profile is only worth building if it survives the session. Write
`<name>-music-taste.md` to the memory directory, following the existing shape of
[[greg-music-taste]] / [[jason-music-taste]] / [[pj-music-taste]]:

- who they are (display name, user id, profile URL, relationship to Cory)
- the deep-cut/hits read, with the actual numbers
- a recency warning if the library is stale
- taste lanes, each with real artist names behind it
- source playlists worth pulling from, as an id/count/name table
- overlap with Cory, and with other profiled friends
- personal threads (their own band, their city) marked clearly as *not* taste
- a **How to apply** line for the next mix request

Then add the one-line pointer to `MEMORY.md`. Link related memories with `[[name]]`
— especially [[mix-deep-cuts]], [[mix-cooldown-participate]], and [[mix-state-sources]],
which the follow-on `mix` build will need.

## Then hand off to `mix`

Profiles exist to make mixes better. Once one is saved, a mix for that person should:
lead from the lanes the profile names, keep the deep-cut default unless the profile
says otherwise, and for a *group* mix build only from the weakest-link intersection —
two friends with no shared genre still share a corner, and that corner is the mix.
