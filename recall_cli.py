"""`cli.py recall …` — the terminal surface for music recall (see recall_service.py).

Every command except ``practice`` is prompt-free and takes ``--json``, like the rest of
cli.py. ``practice`` is the one deliberately interactive command: it's a 2–3 minute
conversation with yourself, so it needs a terminal. The web page (/recall) drives the
same service steps.

    python cli.py recall refresh                      # pull listening + playlists, rotate the set
    python cli.py recall list                         # the ready-to-recommend set, with reasons
    python cli.py recall suggestions                  # proposed links waiting on you
    python cli.py recall accept "Remember Sports" "Lime Garden"
    python cli.py recall link "Remember Sports" "Hop Along" --reason "wanted to mention them at the brewery"
    python cli.py recall hook "Lime Garden" "a newer discovery I want to bring up"
    python cli.py recall practice --minutes 3
    python cli.py recall stats
"""
from __future__ import annotations

import json
import sys
import time

import recall_service as rs


def _conn(args):
    return rs.connect(args.db)


def _out(args, payload, human):
    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False, default=str))
    else:
        human(payload)
    return 0


def _run(fn):
    """Wrap a command so RecallError prints as a one-line error, not a traceback."""
    def inner(args):
        try:
            return fn(args)
        except rs.RecallError as exc:
            if args.json:
                print(json.dumps({"error": str(exc)}))
            else:
                print(f"error: {exc}", file=sys.stderr)
            return 1
    return inner


def _day(ts):
    return ts[:10] if ts else "—"


# ----------------------------------------------------------------- commands

@_run
def cmd_refresh(args):
    conn = _conn(args)
    rep = rs.run_refresh(conn, use_similar=not args.no_similar)

    def human(r):
        lf = r["listening"]
        if "unavailable" in lf:
            print(f"Last.fm: {lf['unavailable']}")
        else:
            print(f"Last.fm: {lf['weeks']} chart weeks, {lf['top_12m']} artists in the last year, "
                  f"{lf['top_overall']} all-time, {lf['loved']} loved tracks, {lf['top_tracks']} top tracks")
        pl = r["playlists"]
        print(f"Playlists: {pl['pools']} of your own ({pl['tracks']} tracks, local cache)"
              + (f" — {pl['note']}" if pl["note"] else ""))
        b = r["by_bucket"]
        print(f"Candidates: {r['candidates']} ("
              + ", ".join(f"{b[k]} {k}" for k in ("recent", "developing", "anchor", "playlist") if k in b) + ")")
        a = r["active"]
        print(f"Ready-to-recommend set: {a['size']} (kept {a['kept']}, added {len(a['added'])}, "
              f"rotated out {len(a['retired'])})")
        if a["added"]:
            print("  new: " + ", ".join(a["added"][:12]) + (" …" if len(a["added"]) > 12 else ""))
        if a["skipped_famous"]:
            print(f"  skipped {len(a['skipped_famous'])} household names (e.g. "
                  + ", ".join(a["skipped_famous"][:4]) + ") — `recall pin` one to keep it anyway")
        print(f"New suggested links: {r['links_proposed']}  (review: cli.py recall suggestions)")
        if r["explain"]:
            print(r["explain"])
    return _out(args, rep, human)


@_run
def cmd_list(args):
    conn = _conn(args)
    rows = rs.list_active(conn)

    def human(rows):
        if not rows:
            print("The set is empty — run `cli.py recall refresh`, or pin artists by hand.")
            return
        for a in rows:
            tag = " 📌" if a["choice"] == "pinned" else ""
            fame = f", {a['listeners'] // 1000}k listeners" if a["listeners"] else ""
            print(f"{a['name']}{tag}  [{a['bucket'] or 'pinned'}{fame}]  — {a['why']}")
            hook = a["hook"] or (f"(suggested) {a['hook_suggested']}" if a["hook_suggested"] else None)
            if hook:
                print(f"    hook: {hook}")
            for n in a["neighbors"]:
                mark = "→" if n["state"] == "accepted" else "?→"
                print(f"    {mark} {n['name']}  ({n['origin']}: {n['reason'] or '—'})")
        print(f"\n{len(rows)} artists. `?→` = suggested, not yet accepted.")
    return _out(args, rows, human)


@_run
def cmd_artist(args):
    conn = _conn(args)
    d = rs.artist_detail(conn, args.name)

    def human(d):
        state = "in the set" if d["active"] else "not in the set"
        extra = f", {d['choice']}" if d["choice"] else ""
        extra += f", snoozed until {_day(d['snoozed_until'])}" if d["snoozed_until"] else ""
        print(f"{d['name']}  ({state}{extra}; origin: {d['origin']})")
        for r in d["reasons"]:
            print(f"  · {r}")
        if not d["reasons"]:
            print("  · no listening evidence on file (curated by you)")
        print(f"  hook: {d['hook'] or '—'}" + (f"   suggested: {d['hook_suggested']}" if d["hook_suggested"] else ""))
        if d["contexts"]:
            print("  contexts: " + ", ".join(d["contexts"]))
        if d["aliases"]:
            print("  also answers to: " + ", ".join(d["aliases"]))
        for n in d["links_out"]:
            print(f"  → {n['name']}  [{n['origin']}, {n['state']}] {n['reason'] or ''}")
        for n in d["links_in"]:
            print(f"  ← {n['name']}  [{n['origin']}, {n['state']}]")
        if d["rejected"]:
            print("  rejected: " + ", ".join(d["rejected"]))
    return _out(args, d, human)


@_run
def cmd_suggestions(args):
    conn = _conn(args)
    rows = rs.proposed_links(conn, args.limit)

    def human(rows):
        if not rows:
            print("No open suggestions.")
            return
        for p in rows:
            conf = f", {p['confidence']:.2f}" if p["confidence"] is not None else ""
            print(f"#{p['link_id']:<4} {p['src']} → {p['dst']}  [{p['origin']}{conf}]  {p['reason']}")
        print('\nAccept: cli.py recall accept "SRC" "DST"   (or --id N)   Reject: recall reject …')
    return _out(args, rows, human)


def _link_state(state):
    @_run
    def cmd(args):
        conn = _conn(args)
        if args.id:
            out = rs.set_link_state_by_id(conn, args.id, state)
        elif args.src and args.dst:
            out = rs.set_link_state(conn, args.src, args.dst, state)
        else:
            raise rs.RecallError("give SRC and DST, or --id N")
        return _out(args, out, lambda o: print(f"{o['src']} → {o['dst']}: {o['state']} ({o['origin']})"))
    return cmd


@_run
def cmd_link(args):
    conn = _conn(args)
    out = rs.link(conn, args.src, args.dst, args.reason or "")

    def human(o):
        print(f"When {o['src']} comes up → {o['dst']}")
        for w in o["warnings"]:
            print(f"  warning: {w}")
    return _out(args, out, human)


@_run
def cmd_unlink(args):
    conn = _conn(args)
    out = rs.unlink(conn, args.src, args.dst)
    return _out(args, out, lambda o: print(f"Removed {o['src']} → {o['dst']}"))


@_run
def cmd_hook(args):
    conn = _conn(args)
    out = rs.set_hook(conn, args.name, None if args.clear else args.text)

    def human(o):
        print(f"{o['artist']}: hook {'cleared' if not o['hook'] else repr(o['hook'])}")
        if o["leaks_name"]:
            print("  note: the hook contains the name, so it won't be used as a prompt or hint")
    return _out(args, out, human)


def _choice(choice):
    @_run
    def cmd(args):
        conn = _conn(args)
        out = rs.set_choice(conn, args.name, choice, snooze_days=getattr(args, "days", None))

        def human(o):
            if "snooze_until" in o:
                print(f"{o['artist']}: snoozed until {_day(o['snooze_until'])}")
            else:
                print(f"{o['artist']}: {o['choice'] or 'cleared (back to normal)'}")
        return _out(args, out, human)
    return cmd


@_run
def cmd_context(args):
    conn = _conn(args)
    out = rs.tag_context(conn, args.name, args.context, remove=args.remove)
    return _out(args, out, lambda o: print(f"{o['artist']} {'removed from' if o['removed'] else 'filed under'} “{o['context']}”"))


@_run
def cmd_alias(args):
    conn = _conn(args)
    out = rs.add_alias(conn, args.alias, args.name)
    return _out(args, out, lambda o: print(f"“{o['alias']}” now counts as {o['artist']}"))


@_run
def cmd_note(args):
    conn = _conn(args)
    out = rs.add_note(conn, args.text)
    return _out(args, out, lambda o: print("Noted."))


@_run
def cmd_config(args):
    conn = _conn(args)
    if args.key:
        if args.value is None:
            raise rs.RecallError("give a value, e.g. `recall config set_size 25`")
        rs.set_setting(conn, args.key, args.value)
    out = rs.all_settings(conn)
    return _out(args, out, lambda o: [print(f"  {k:<17} {v}") for k, v in o.items()])


@_run
def cmd_stats(args):
    conn = _conn(args)
    st = rs.stats(conn, days=args.days)

    def human(s):
        print(f"Ready-to-recommend: {s['active_set']} artists · {s['recommendations']} accepted links"
              f" · {s['proposed']} suggestions waiting")
        print(f"Practice: {s['due_now']} due now, {s['new_items']} not yet tried")
        if s["unassisted_recalls"]:
            print(f"Recalled without help (last {s['window_days']} days): "
                  + ", ".join(f"{n}" + (f" ×{c}" if c > 1 else "") for n, c in s["unassisted_recalls"]))
        if s["difficult"]:
            print("Felt difficult:")
            for d in s["difficult"]:
                print(f"  · {d['prompt']}  (back {_day(d['next_due'])})")
        if s["notes"]:
            print("In real conversations:")
            for n in s["notes"]:
                print(f"  · {_day(n['created_at'])}  {n['text']}")
        else:
            print('Remembered a recommendation out in the world? `cli.py recall note "…"`')
    return _out(args, st, human)


# ----------------------------------------------------------------- practice (interactive)

_MARKS = {"exact": "✓", "alias": "✓", "loose": "✓", "spelling": "≈", "ambiguous": "?", "unknown": "+"}


def _ask(prompt: str) -> str:
    try:
        return input(prompt).strip()
    except EOFError:
        return "q"


def _show_feedback(conn, fb) -> None:
    for r in fb["resolved"]:
        mark = _MARKS[r["how"]]
        if r["how"] == "ambiguous":
            print(f"  ? “{r['text']}” could be {' / '.join(r['options'])} — not counted; use the exact name")
        elif r["how"] == "unknown":
            print(f"  + “{r['text']}” — not in Crate yet")
        else:
            spelled = f" (from “{r['text']}”)" if r["how"] == "spelling" else ""
            where = "✓ on your list" if r["expected"] else ("in your set" if r["in_active_set"] else "known to Crate")
            print(f"  {mark} {r['name']}{spelled} — {where}")
    print("  Your list:" if fb["expected"] else "")
    for e in fb["expected"][:8]:
        print(f"    {'✓' if e['hit'] else '·'} {e['name']} — {e['reason']}")
    if len(fb["expected"]) > 8:
        print(f"    … and {len(fb['expected']) - 8} more")
    if fb["suggestions"]:
        print("  Also suggested (examples, not an answer key):")
        for s in fb["suggestions"]:
            print(f"    ?→ {s['name']} — {s['reason']}")
    if fb["sentence"]:
        print(f"  Try saying: “{fb['sentence']}”")


def _follow_ups(conn, fb) -> None:
    """Offer to keep what came up: spelling → alias, new answers → links, suggestions → accept."""
    for r in fb["resolved"]:
        if r["how"] == "spelling" and _ask(f"  Remember “{r['text']}” as {r['name']}? [y/N] ").lower() == "y":
            rs.add_alias(conn, r["text"], r["name"])
    if fb["cue"]:
        for r in fb["resolved"]:
            if r["how"] in ("unknown", "exact", "alias", "loose", "spelling") and not r.get("expected"):
                name = r["name"] or r["text"]
                if rs.norm(name) == rs.norm(fb["cue"]):
                    continue
                if _ask(f"  Add {name} as a recommendation when {fb['cue']} comes up? [y/N] ").lower() == "y":
                    try:
                        out = rs.link(conn, fb["cue"], name, "came up in practice")
                    except rs.RecallError as exc:
                        print(f"    {exc}")
                        continue
                    for w in out["warnings"]:
                        print(f"    warning: {w}")
        for s in fb["suggestions"]:
            ans = _ask(f"  Keep {fb['cue']} → {s['name']}? [y]es / [n]o / Enter to leave it ").lower()
            if ans in ("y", "n"):
                rs.set_link_state_by_id(conn, s["link_id"], "accepted" if ans == "y" else "rejected")


def _rate(conn, attempt_id) -> bool:
    while True:
        ans = _ask("  How did that feel?  1) came easily  2) took a moment  3) couldn't recall  (q to stop) ")
        if ans in ("1", "2", "3"):
            r = rs.rate(conn, attempt_id, rs.RATINGS[int(ans) - 1])
            print(f"  → back around {_day(r['next_due'])}")
            return True
        if ans.lower() == "q":
            return False


@_run
def cmd_practice(args):
    if not sys.stdin.isatty():
        raise rs.RecallError("practice is interactive — run it in a terminal (or use the /recall page)")
    conn = _conn(args)
    plan = rs.start_session(conn, prompts=args.prompts, fresh=args.fresh)
    if not plan["session_id"]:
        if not plan["has_items"]:
            print("Nothing to practice yet. Accept a suggestion (`recall suggestions`), add a link, "
                  "or run `recall refresh` so there's a set to recall from.")
            return 0
        when = f" (next up {_day(plan['next_due'])})" if plan["next_due"] else ""
        if _ask(f"Nothing is due{when}. Try a fresh prompt anyway? [y/N] ").lower() != "y":
            return 0
        plan = rs.start_session(conn, prompts=args.prompts, fresh=True)
    sid = plan["session_id"]
    deadline = time.monotonic() + args.minutes * 60
    print("Answer in your own words — commas between names. `?` hint · `!` reveal · Enter to skip · `q` to stop.\n")
    stopped = False
    while not stopped:
        p = rs.next_prompt(conn, sid)
        if p is None:
            break
        print(f"[{p['position']}/{p['total']}] {p['prompt']}")
        fb = None
        while fb is None:
            ans = _ask("> ")
            if ans == "?":
                try:
                    print("  hint: " + rs.hint(conn, p["attempt_id"])["hint"])
                except rs.RecallError as exc:
                    print(f"  {exc}")
            elif ans == "!":
                fb = rs.reveal(conn, p["attempt_id"])
            elif ans.lower() == "q":
                stopped = True
                break
            else:
                fb = rs.submit(conn, p["attempt_id"], ans)
        if fb is None:
            break
        _show_feedback(conn, fb)
        _follow_ups(conn, fb)
        if not _rate(conn, p["attempt_id"]):
            break
        print()
        if time.monotonic() > deadline:
            print("That's your time — wrapping up.\n")
            break
    recap = rs.end_session(conn, sid)
    print("— Recap —")
    print("Recalled: " + (", ".join(recap["recalled"]) or "nothing this time — that's fine, it's practice"))
    for r in recap["revisit"]:
        print(f"Worth revisiting: {r}")
    if recap["sentence"]:
        print(f"Ready to say: “{recap['sentence']}”")
    return 0


# ----------------------------------------------------------------- wiring

def add_recall_parser(sub, common) -> None:
    """Attach `recall` and its subcommands to cli.py's subparsers."""
    rp = sub.add_parser("recall", parents=[common], help="practice recalling artists to recommend")
    rp.add_argument("--db", default=rs.DEFAULT_DB, help="path to the SQLite DB")
    rsub = rp.add_subparsers(dest="recall_command", required=True)

    def add(name, fn, help_):
        p = rsub.add_parser(name, parents=[common], help=help_)
        p.set_defaults(func=fn)
        return p

    p = add("refresh", cmd_refresh, "rebuild candidates from Last.fm + your playlists; rotate the set")
    p.add_argument("--no-similar", action="store_true", help="skip Last.fm similar-artist suggestions")
    add("list", cmd_list, "the ready-to-recommend set, with reasons, hooks, neighbors")
    p = add("artist", cmd_artist, "everything Crate knows about one artist")
    p.add_argument("name")
    p = add("suggestions", cmd_suggestions, "suggested links waiting for accept/reject")
    p.add_argument("--limit", type=int, default=30)
    for name, state in (("accept", "accepted"), ("reject", "rejected")):
        p = add(name, _link_state(state), f"{name} a suggested link")
        p.add_argument("src", nargs="?")
        p.add_argument("dst", nargs="?")
        p.add_argument("--id", type=int, help="link id from `recall suggestions`")
    p = add("link", cmd_link, "when SRC comes up, remember DST (your link beats any suggestion)")
    p.add_argument("src")
    p.add_argument("dst")
    p.add_argument("--reason", default="")
    p = add("unlink", cmd_unlink, "delete a link")
    p.add_argument("src")
    p.add_argument("dst")
    p = add("hook", cmd_hook, "set a one-line hook for an artist")
    p.add_argument("name")
    p.add_argument("text", nargs="?")
    p.add_argument("--clear", action="store_true")
    for name, choice, help_ in (("pin", "pinned", "keep in the set"),
                                ("dismiss", "dismissed", "drop from the set"),
                                ("nope", "nope", "I know this one but don't want to recommend it"),
                                ("unpin", "clear", "clear pin/dismiss/nope/snooze")):
        p = add(name, _choice(choice), help_)
        p.add_argument("name")
    p = add("snooze", _choice("snooze"), "hide from the set for a while")
    p.add_argument("name")
    p.add_argument("--days", type=int, default=None)
    p = add("context", cmd_context, "file an artist under a conversation context")
    p.add_argument("name")
    p.add_argument("context")
    p.add_argument("--remove", action="store_true")
    p = add("alias", cmd_alias, "count another spelling as this artist")
    p.add_argument("alias")
    p.add_argument("name")
    p = add("practice", cmd_practice, "a short interactive practice session")
    p.add_argument("--minutes", type=float, default=3, help="soft time limit (default 3)")
    p.add_argument("--prompts", type=int, default=None, help="max prompts (default: setting, 3)")
    p.add_argument("--fresh", action="store_true", help="practice even if nothing is due")
    p = add("stats", cmd_stats, "recent recalls, difficult items, real-conversation notes")
    p.add_argument("--days", type=int, default=14)
    p = add("note", cmd_note, "log a recommendation you remembered in a real conversation")
    p.add_argument("text")
    p = add("config", cmd_config, "show or set a setting (set_size, ladder, …)")
    p.add_argument("key", nargs="?")
    p.add_argument("value", nargs="?")
