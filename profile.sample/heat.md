# Heat — <YOUR NAME / CONTEXT> (template)

> What you're into *right now*, each fading out on its own — the mirror image of the
> ice box: ice pushes tracks out of builds, heat pulls them in. Read back offline via
> the `mix` skill's `heat list` (nothing consumes it into a build yet).

Tiers: 30/15/5
Cap: 50

<`Tiers:` and `Cap:` are optional overrides — delete either line to take the defaults
shown above. Tiers are the % share of a mix each tier may claim (tier 1 hottest, tier
3 warmest); Cap is the ceiling on how much of a mix heat may claim in total, so one
hot week can't eat the whole playlist.>

## Vibing

<A plain fade: an artist or genre you're into right now. Starts hot, steps down over
`Fade` (default 30d if left blank), then expires and drops off the list on its own —
delete nothing, just let it age out. `Fits` scopes the row to briefs whose terms
overlap its comma-separated list (blank = fits anywhere); it isn't consumed until a
later ticket, but fill it in now if you know it.>

| What | Kind | Started | Fade | Fits | Note |
|---|---|---|---|---|---|
| <Dance-punk> | <genre> | <2026-09-18> | <60d> | <dance, party, hype> | |
| <Some Artist> | <artist> | <2026-09-14> | | | |

## Concerts

<Ramps UP toward the show date — dormant past 90 days out, then tier 3/2/1 as it
nears, peaking the final week — then fades back DOWN afterward (afterglow) over
`Fade` (default 30d) before expiring. Two artists on the same bill are two rows
sharing the same Date.>

| Who | Date | Fade | Fits | Note |
|---|---|---|---|---|
| <Some Headliner> | <2026-10-15> | | | |
| <Support Act> | <2026-10-15> | | | <w/ Some Headliner> |

## Cooled off

<Anything that used to be hot and isn't worth deleting outright — park it here.
Ignored entirely by the parser, so move a row down here instead of deleting it if
you might want the history later.>
