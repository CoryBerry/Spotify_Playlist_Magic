---
name: albums
description: Build a whole-album source pool on Spotify — a chronological discography for an artist or band, or a scene/genre/era survey like "Albums - Dance-Punk I". Use when Cory asks for a discography, "every album by X", an album playlist, a chronological run through a catalog, a scene survey in album form, or wants albums added to an existing "Albums - …" pool.
---

# albums — whole-album source pools

Some playlists aren't mixes. A **pool** is a set of complete albums laid end to end,
each in its own track order, sequenced by release date. Nothing is shuffled,
interleaved, or deep-cut-filtered. Cory builds these to *have the raw material* —
they feed the `mix` skill later. Their descriptions say so out loud: "A source pool,
not a mix."

Two front ends, one machine:

- **Discography** — one artist, or one person across their bands (Maynard James
  Keenan → Tool + A Perfect Circle + Puscifer). One timeline, interleaved by release
  date, so it traces a career arc rather than grouping by project.
- **Survey** — a scene, genre, label, or era. `Albums - Dance-Punk I/II/III` is the
  reference: tiered by centrality, each tier its own pool.

## Not the `mix` skill

| | `albums` | `mix` |
|---|---|---|
| Source | the Spotify catalog | Cory's existing playlists |
| Unit | whole albums | individual tracks |
| Order | chronological by release | energy arc, de-clumped |
| Deep cuts | no — everything | yes, popularity is a negative signal |
| Cooldown | **never** | always (`--fresh` + `--cooldown`) |

**Never pass `--cooldown` when shipping a pool.** A 170-track discography written to
`track_history` locks those artists out of real mixes for a week. Pools are reference
material; they don't consume the cooldown budget. Do pass `--record` so the pool shows
up in Recently Created.

## The loop

Plumbing lives in `albums_helper.py`; run it from the repo root with `PYTHONUTF8=1`.
It never writes to Spotify — it decides *which tracks, in what order*, and emits a URI
file. `mix_helper create|replace` ships it, so the repo keeps one write path.

### 1. Discover (discography only)

```
PYTHONUTF8=1 python .claude/skills/albums/albums_helper.py releases --artist "Tool" --artist "Puscifer" --deep
```

Lists every release, classified `studio | ep | live | remix | compilation | single |
recycled`, with a KEEP/skip call and editions collapsed.

**Use `--deep` for any discography.** It reads each tracklist, which is the only way
to catch the two silent traps: a live album whose title doesn't say "live", and a
*compilation of songs that already exist on earlier records*. Puscifer's "In Case You
Were Napping" (2025) is fifteen recycled songs with a brand-new title — regexes can't
see it, tracklist overlap can. `--deep` costs one API call per release; on a catalog
that's seconds, and it is worth it every time.

For a survey you usually skip this step and write the album list yourself.

### 2. Plan

Feed `Artist - Album` lines (or album IDs/URLs), one per line:

```
PYTHONUTF8=1 python .claude/skills/albums/albums_helper.py plan --from albums.txt --uris pool.txt
```

Resolves each line to the best edition, prunes reissues, orders by release date,
prints a manifest, writes URIs. **Read the manifest before shipping** — any album
whose note isn't `clean` had tracks cut, and `VERIFY` means the helper couldn't tell
where the bonus material started.

### 3. Ship

```
PYTHONUTF8=1 python .claude/skills/mix/mix_helper.py create --name "Albums - …" --desc "…" --uris-file pool.txt --record
```

### 4. Extend an existing pool

`--extend` merges the current pool's albums in, so new records land in their real
chronological slot instead of being tacked on the end. Then `replace` keeps the URL:

```
PYTHONUTF8=1 python .claude/skills/albums/albums_helper.py plan --from new.txt --extend "Albums - Dance-Punk II" --uris pool.txt
PYTHONUTF8=1 python .claude/skills/mix/mix_helper.py replace --playlist <id> --uris-file pool.txt --record
```

## Inclusion rules

These are Cory's, decided explicitly. The governing principle: **he would rather
delete a few tracks than rebuild a list.** When genuinely torn, include.

- **EPs are in.** 4+ tracks counts. 1–3 track singles stay out.
- **Live albums are out — but always report them.** They mostly duplicate songs
  already in the pool. List what you skipped so "add Cinquanta" is one word, not a
  rebuild.
- **Remixes and covers are fine.** A covers record (A Perfect Circle's *eMOTIVe*) or a
  remix EP is worth having. Title-obvious remix *albums* are skipped by default —
  mention them and offer.
- **Recycled records are out.** Flagged by `--deep` when most of the tracklist already
  appears on earlier releases. Say which and why; the percentage is in the output.
- **Original release over deluxe.** The helper prefers an unmarked title, then falls
  back to the earliest, leanest listing. *Rumours (Super Deluxe)* is 58 tracks and
  must never land in a pool as-is.
- A plain `(Remastered)` is **not** a deluxe — it carries the original tracklist and
  is left alone.

## Sequencing

Chronological by the album's *earliest* release date across its editions, so picking a
2004 remaster doesn't shove a 1977 record to the end. Within an album, original track
order, untouched.

## Size and tiering

Past roughly **20 albums or 250 tracks**, a pool wants splitting. **Propose the tier
breakdown and wait for a yes before creating anything** — the split is a judgment call
about the scene, and it's Cory's to make.

Tier by centrality, the way Dance-Punk does it: Tier I is the spine (the canonical,
defining records), Tier II the deep scene (second albums, the adjacent wing), Tier III
the outer orbit (rarer, weirder, further from the center). Give each tier a name and a
one-line rationale in the proposal.

A discography stays one pool unless it's enormous — a career arc is the point, and
splitting it by tier would destroy it. Split a huge discography by era instead, and
only if asked.

## Naming and description

Follow the library's existing convention so pools sort together:

```
Albums - <Topic>              Albums - Dance-Punk I
Albums - <Artist>             Albums - Maynard James Keenan
```

Description, one line, matching the Dance-Punk house style:

```
Tier 1 / The Spine: 16 whole albums, 2001-2008, in album order and chronological by
release. Dance-punk, DFA, electroclash, nu-rave. A source pool, not a mix.
```

For a discography, drop the tier clause and name the bands instead. Always end with
**"A source pool, not a mix."** — it's how Cory tells these apart at a glance.

## Report back

After shipping, state:

1. The album table — year, artist, album, track count.
2. **What you excluded and why**, grouped: live records, remix albums, recycled
   compilations, singles. This is the part that saves a rebuild.
3. Any album the manifest didn't mark `clean` — what got cut.
4. That cooldown was deliberately not written.

## Tests

```
python -m pytest .claude/skills/albums/
```

The Spotify reads are thin paging loops. What's tested is the judgment: classification,
edition preference, reissue pruning, recycled detection, artist matching, ordering.
