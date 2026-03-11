# ---------------------------------------------------------------
# app.py — Spotify Tools
# ---------------------------------------------------------------

from flask import Flask, render_template, request, session, redirect, url_for, jsonify, flash
from urllib.parse import urlparse
from flask_sqlalchemy import SQLAlchemy
from datetime import datetime, timedelta
from dotenv import load_dotenv
import random
import json
import os
import time
import spotipy
from spotipy.oauth2 import SpotifyOAuth

try:
    from plexapi.server import PlexServer as _PlexServer
    PLEX_AVAILABLE = True
except ImportError:
    PLEX_AVAILABLE = False

load_dotenv()

def _name_sim(query, result):
    """Word-overlap similarity between a query title and a Spotify result name."""
    q = set(query.lower().split())
    r = set(result.lower().split())
    return len(q & r) / len(q) if q else 0.0


def _search_line(sp, line):
    """Search Spotify for a line, returning up to 4 scored candidates sorted by name similarity."""
    parts  = line.split(" - ", 1)
    artist = parts[0].strip() if len(parts) == 2 else ""
    title  = parts[1].strip() if len(parts) == 2 else line.strip()
    q      = (f"artist:{artist} " if artist else "") + title

    candidates = []
    try:
        for t in (sp.search(q=q, type="track", limit=5).get("tracks") or {}).get("items") or []:
            if t:
                candidates.append({
                    "uri":    t["uri"],
                    "type":   "track",
                    "name":   t["name"],
                    "artist": t["artists"][0]["name"] if t.get("artists") else "",
                    "score":  _name_sim(title, t["name"]),
                })
    except Exception:
        pass
    try:
        for a in (sp.search(q=q, type="album", limit=3).get("albums") or {}).get("items") or []:
            if a:
                candidates.append({
                    "uri":    a["uri"],
                    "type":   "album",
                    "name":   a["name"],
                    "artist": a["artists"][0]["name"] if a.get("artists") else "",
                    "score":  _name_sim(title, a["name"]),
                })
    except Exception:
        pass
    candidates.sort(key=lambda x: -x["score"])
    return candidates[:4]


def _detect_list_type(line_results):
    """Return 'track', 'album', or 'mixed' based on majority top-match type."""
    top_types = [lr["matches"][0]["type"] for lr in line_results if lr["matches"]]
    if not top_types:
        return "mixed"
    track_n = top_types.count("track")
    album_n = top_types.count("album")
    total   = track_n + album_n
    if track_n / total >= 0.6:
        return "track"
    if album_n / total >= 0.6:
        return "album"
    return "mixed"


def _bias_matches(line_results, list_type):
    """Re-sort each line's matches so the dominant list type appears first."""
    if list_type == "mixed":
        return
    for lr in line_results:
        preferred = [m for m in lr["matches"] if m["type"] == list_type]
        other     = [m for m in lr["matches"] if m["type"] != list_type]
        lr["matches"] = preferred + other


def _now_label():
    return datetime.now().strftime('%m/%d %I:%M%p')

def _date_label():
    now = datetime.now()
    return now.strftime("%B ") + str(now.day) + now.strftime(" '%y")  # e.g. "March 6 '26"

def _period_of_day():
    hour = datetime.now().hour
    if hour < 12:  return "Morning"
    if hour < 17:  return "Afternoon"
    if hour < 21:  return "Evening"
    return "Night"

app = Flask(__name__)
_secret_key = os.environ.get("SECRET_KEY")
if not _secret_key:
    raise RuntimeError("SECRET_KEY environment variable must be set")
app.secret_key = _secret_key
app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///spotify_tools.db"

db = SQLAlchemy(app)


# ---------------------------------------------------------------
# Models
# ---------------------------------------------------------------

class PlaylistTag(db.Model):
    id          = db.Column(db.Integer, primary_key=True)
    user_id     = db.Column(db.String(100), nullable=False, default="local")
    playlist_id = db.Column(db.String(100), nullable=False)
    tag         = db.Column(db.String(50),  nullable=False)
    created_at  = db.Column(db.DateTime, default=datetime.now)

    __table_args__ = (db.UniqueConstraint("user_id", "playlist_id", "tag"),)


class PlaylistCache(db.Model):
    id         = db.Column(db.Integer, primary_key=True)
    user_id    = db.Column(db.String(100), nullable=False, unique=True)
    data       = db.Column(db.Text, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.now)


class CreatedPlaylist(db.Model):
    id          = db.Column(db.Integer, primary_key=True)
    playlist_id = db.Column(db.String(100), nullable=False)
    name        = db.Column(db.String(200), nullable=False)
    tool        = db.Column(db.String(50),  nullable=False)   # "Block Mix" or "Album Blast"
    provider    = db.Column(db.String(20),  nullable=False)   # "spotify" or "plex"
    url         = db.Column(db.String(500), nullable=True)
    created_at  = db.Column(db.DateTime,   default=datetime.now)
    alive       = db.Column(db.Boolean,    default=True)
    checked_at  = db.Column(db.DateTime,   nullable=True)
    gen_seconds = db.Column(db.Float,      nullable=True)
    track_count = db.Column(db.Integer,    nullable=True)


class PlaylistUsage(db.Model):
    id          = db.Column(db.Integer, primary_key=True)
    playlist_id = db.Column(db.String(100), nullable=False)
    provider    = db.Column(db.String(20),  nullable=False)  # "spotify" or "plex"
    use_count   = db.Column(db.Integer,     default=1, nullable=False)
    last_used   = db.Column(db.DateTime,    default=datetime.now)

    __table_args__ = (db.UniqueConstraint("playlist_id", "provider"),)


class TrackHistory(db.Model):
    id       = db.Column(db.Integer, primary_key=True)
    track_id = db.Column(db.String(200), nullable=False)  # URI for Spotify, ratingKey for Plex
    provider = db.Column(db.String(20),  nullable=False)
    used_at  = db.Column(db.DateTime,    default=datetime.now)


class BuildSource(db.Model):
    """Source playlists used in a Block Mix build, in cycle order."""
    id                  = db.Column(db.Integer, primary_key=True)
    created_playlist_id = db.Column(db.Integer, db.ForeignKey("created_playlist.id"), nullable=False)
    playlist_id         = db.Column(db.String(100), nullable=False)
    playlist_name       = db.Column(db.String(200), nullable=False)
    owner_id            = db.Column(db.String(100), nullable=True)   # Spotify owner ID; None for pre-migration rows
    position            = db.Column(db.Integer,     nullable=False)  # 0-indexed order in cycle


class ThawTally(db.Model):
    """Monthly tally of tracks thawed from cooldown."""
    id       = db.Column(db.Integer, primary_key=True)
    year     = db.Column(db.Integer, nullable=False)
    month    = db.Column(db.Integer, nullable=False)
    provider = db.Column(db.String(20), nullable=False)
    count    = db.Column(db.Integer, default=0, nullable=False)
    __table_args__ = (db.UniqueConstraint("year", "month", "provider"),)


class AppSettings(db.Model):
    id                 = db.Column(db.Integer, primary_key=True)
    cooldown_days      = db.Column(db.Integer, default=7)
    cooldown_max_plays = db.Column(db.Integer, default=2)


with app.app_context():
    db.create_all()


def _record_usage(playlist_ids, provider):
    for pid in playlist_ids:
        rec = PlaylistUsage.query.filter_by(playlist_id=pid, provider=provider).first()
        if rec:
            rec.use_count += 1
            rec.last_used  = datetime.now()
        else:
            db.session.add(PlaylistUsage(playlist_id=pid, provider=provider))
    db.session.commit()


def get_settings():
    s = AppSettings.query.get(1)
    if not s:
        s = AppSettings(id=1)
        db.session.add(s)
        db.session.commit()
    return s


def get_cooldown_stats(provider):
    from sqlalchemy import func
    s      = get_settings()
    cd     = s.cooldown_days
    cutoff = datetime.now() - timedelta(days=cd)
    now    = datetime.now()

    # Tracks used within the cooldown window
    rows = db.session.query(
        TrackHistory.track_id,
        func.count(TrackHistory.id).label("n"),
        func.max(TrackHistory.used_at).label("last_used")
    ).filter(
        TrackHistory.provider == provider,
        TrackHistory.used_at  >= cutoff
    ).group_by(TrackHistory.track_id).all()

    on_ice     = [r for r in rows if r.n >= s.cooldown_max_plays]
    on_ice_ids = {r.track_id for r in on_ice}

    # All-time stats: unique tracks ever used + total play events
    all_time = db.session.query(
        TrackHistory.track_id,
        func.count(TrackHistory.id).label("total_plays")
    ).filter(
        TrackHistory.provider == provider
    ).group_by(TrackHistory.track_id).all()

    total_plays = sum(r.total_plays for r in all_time)
    multi_cycle = sum(1 for r in all_time if r.total_plays > s.cooldown_max_plays)
    thawed      = db.session.query(func.sum(ThawTally.count)).filter_by(provider=provider).scalar() or 0

    # Bucket day values: 1/7, 3/7, 6/7 of cooldown_days
    b1 = max(1, round(cd / 7))
    b2 = max(1, round(3 * cd / 7))
    b3 = max(1, round(6 * cd / 7))

    def _count_range(min_days, max_days):
        lo = now - timedelta(days=max_days)
        hi = now - timedelta(days=min_days)
        return sum(1 for r in on_ice if lo <= r.last_used < hi)

    return {
        "total":         len(on_ice),
        "thawed":        thawed,
        "total_plays":   total_plays,
        "multi_cycle":   multi_cycle,
        "buckets":       [(b1, _count_range(0, b1)), (b2, _count_range(b1, b2)), (b3, _count_range(b2, b3))],
        "cooldown_days": cd,
    }


def auto_thaw(provider):
    """Delete TrackHistory rows older than the cooldown window. Returns count of unique tracks thawed."""
    s      = get_settings()
    cutoff = datetime.now() - timedelta(days=s.cooldown_days)
    rows   = TrackHistory.query.filter(
        TrackHistory.provider == provider,
        TrackHistory.used_at  <  cutoff
    ).all()
    if not rows:
        return 0
    unique = len({r.track_id for r in rows})
    for r in rows:
        db.session.delete(r)
    now = datetime.now()
    tally = ThawTally.query.filter_by(year=now.year, month=now.month, provider=provider).first()
    if tally:
        tally.count += unique
    else:
        db.session.add(ThawTally(year=now.year, month=now.month, provider=provider, count=unique))
    db.session.commit()
    return unique


# ---------------------------------------------------------------
# Spotify mood presets — maps preset name to audio feature ranges
# ---------------------------------------------------------------

MOOD_PRESETS = {
    "hype":       {"energy": (0.7, 1.0), "danceability": (0.6, 1.0)},
    "chill":      {"energy": (0.0, 0.5), "acousticness": (0.4, 1.0)},
    "happy":      {"valence": (0.6, 1.0), "energy": (0.4, 1.0)},
    "melancholy": {"valence": (0.0, 0.4), "energy": (0.0, 0.5)},
    "workout":    {"energy": (0.8, 1.0), "tempo": (120, 999)},
    "focus":      {"instrumentalness": (0.3, 1.0), "speechiness": (0.0, 0.1)},
}

def fetch_audio_features(sp, track_uris):
    """Batch fetch audio features for a list of track URIs. Returns dict keyed by track ID."""
    track_ids    = [uri.split(":")[-1] for uri in track_uris]
    features_map = {}
    for i in range(0, len(track_ids), 100):  # Spotify allows 100 per request
        batch = sp.audio_features(track_ids[i:i + 100])
        for f in batch:
            if f:  # can be None for local files / podcasts
                features_map[f["id"]] = f
    return features_map

def filter_by_mood(track_uris, features_map, preset):
    """Return tracks matching the mood preset. Falls back to full list if not enough match."""
    rules    = MOOD_PRESETS.get(preset, {})
    filtered = [
        uri for uri in track_uris
        if (f := features_map.get(uri.split(":")[-1]))
        and all(lo <= f.get(key, 0) <= hi for key, (lo, hi) in rules.items())
    ]
    return filtered


# ---------------------------------------------------------------
# Spotify OAuth helper — builds an authenticated Spotify client
# ---------------------------------------------------------------

SPOTIFY_SCOPE = "playlist-read-private playlist-modify-private playlist-modify-public"

def get_spotify_oauth():
    return SpotifyOAuth(
        client_id=os.environ.get("SPOTIFY_CLIENT_ID"),
        client_secret=os.environ.get("SPOTIFY_CLIENT_SECRET"),
        redirect_uri=os.environ.get("SPOTIFY_REDIRECT_URI"),
        scope=SPOTIFY_SCOPE
    )

def get_spotify_client():
    token_info = session.get("spotify_token")
    if not token_info:
        return None
    # Refresh token if expired
    sp_oauth = get_spotify_oauth()
    if sp_oauth.is_token_expired(token_info):
        token_info = sp_oauth.refresh_access_token(token_info["refresh_token"])
        session["spotify_token"] = token_info
    return spotipy.Spotify(auth=token_info["access_token"])


CACHE_TTL  = timedelta(hours=24)   # full re-fetch once a day
VERIFY_TTL = timedelta(minutes=15)  # no API call at all under 15 min

def get_cached_playlists(sp, user_id):
    """Return (playlists, updated_at, from_cache).

    Three-tier TTL:
      < 15 min  → serve cache, no API call
      15 min–24h → fetch first page only; if total count unchanged, serve stale
      > 24h     → full paginated re-fetch
    """
    cache = PlaylistCache.query.filter_by(user_id=user_id).first()
    now   = datetime.now()

    # Tier 1: hot cache — no API call
    if cache and (now - cache.updated_at) < VERIFY_TTL:
        return json.loads(cache.data), cache.updated_at, True

    first_page = None  # reuse in full fetch if we already paid for it

    # Tier 2: warm cache — one call to check if anything changed
    if cache and (now - cache.updated_at) < CACHE_TTL:
        try:
            first_page = sp.current_user_playlists(limit=50)
            if first_page["total"] == len(json.loads(cache.data)):
                return json.loads(cache.data), cache.updated_at, True
            # Total changed — fall through to full fetch, reusing first_page
        except Exception:
            return json.loads(cache.data), cache.updated_at, True

    # Tier 3: cold cache or total changed — full paginated fetch
    try:
        playlists = []
        results   = first_page or sp.current_user_playlists(limit=50)
        while results:
            playlists.extend(results["items"])
            results = sp.next(results) if results["next"] else None

        data = json.dumps(playlists)
        if cache:
            cache.data       = data
            cache.updated_at = now
        else:
            db.session.add(PlaylistCache(user_id=user_id, data=data, updated_at=now))
        db.session.commit()
        return playlists, now, False

    except Exception:
        if cache:
            return json.loads(cache.data), cache.updated_at, True
        return [], now, False


# ---------------------------------------------------------------
# Plex helpers
# ---------------------------------------------------------------

PLEX_MIN_TRACKS = 20   # minimum tracks for a playlist to appear in Block Mix

_plex_configured = bool(os.environ.get("PLEX_URL") and os.environ.get("PLEX_TOKEN"))
_plex_instance   = None

def get_plex():
    """Return a cached PlexServer, or None if not configured/available."""
    global _plex_instance
    if not PLEX_AVAILABLE or not _plex_configured:
        return None
    if _plex_instance is not None:
        return _plex_instance
    try:
        _plex_instance = _PlexServer(os.environ["PLEX_URL"], os.environ["PLEX_TOKEN"])
        return _plex_instance
    except Exception:
        return None


@app.context_processor
def inject_plex_enabled():
    return {"plex_enabled": _plex_configured}


# ---------------------------------------------------------------
# Routes — General
# ---------------------------------------------------------------

@app.route("/")
def index():
    sp = get_spotify_client()
    return render_template("index.html", logged_in=sp is not None)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("index"))



# ---------------------------------------------------------------
# Routes — Spotify
# ---------------------------------------------------------------

@app.route("/spotify")
def spotify():
    sp = get_spotify_client()
    if not sp:
        return redirect(url_for("spotify_login"))
    return redirect(url_for("spotify_playlists"))


@app.route("/spotify/login")
def spotify_login():
    auth_url = get_spotify_oauth().get_authorize_url()
    return redirect(auth_url)


@app.route("/callback")
def spotify_callback():
    code = request.args.get("code")
    token_info = get_spotify_oauth().get_access_token(code)
    session["spotify_token"] = token_info
    return redirect(url_for("spotify_playlists"))


@app.route("/spotify/playlists")
def spotify_playlists():
    sp = get_spotify_client()
    if not sp:
        return redirect(url_for("spotify_login"))

    thawed = auto_thaw("spotify")
    if thawed:
        flash(f"🌊 {thawed} track{'s' if thawed != 1 else ''} thawed from cooldown", "info")

    user_id = sp.me()["id"]
    playlists, cache_updated_at, _ = get_cached_playlists(sp, user_id)

    # Only show playlists with enough tracks to be useful for block mixing
    # Guard against null items and null tracks fields the Spotify API occasionally returns
    playlists = [p for p in playlists
                 if p and p.get("tracks") and p["tracks"]["total"] >= 20
                 and "Block Mix" not in p.get("name", "")]

    # Pass tag data so Block Mix can filter by tag
    all_tags = PlaylistTag.query.filter_by(user_id="local").all()
    tag_map  = {}
    for t in all_tags:
        tag_map.setdefault(t.playlist_id, []).append(t.tag)

    all_tag_names = sorted({t.tag for t in all_tags})

    usage_records    = PlaylistUsage.query.filter_by(provider="spotify").all()
    usage_map_count  = {r.playlist_id: r.use_count for r in usage_records}

    return render_template("spotify_playlists.html", playlists=playlists, tag_map=tag_map,
                           all_tags=all_tag_names, user_id=user_id,
                           cache_updated_at=cache_updated_at,
                           cache_refresh_url=url_for("cache_refresh") + "?next=" + request.path,
                           usage_map_count=usage_map_count)


@app.route("/spotify/build", methods=["POST"])
def spotify_build():
    sp = get_spotify_client()
    if not sp:
        return redirect(url_for("spotify_login"))

    selected_ids = request.form.getlist("playlist_ids")
    block_size   = max(1, min(10, int(request.form.get("block_size", 4))))
    repeats      = max(1, min(10, int(request.form.get("repeats", 1))))
    mood         = request.form.get("mood", "none")
    pinned_id    = request.form.get("pinned_playlist_id", "").strip()
    pin_interval = max(1, min(10, int(request.form.get("pin_interval", 1))))
    weights      = {pid: max(0.5, min(5, float(request.form.get(f"weight_{pid}", 1)))) for pid in selected_ids}
    if any(w == 0.5 for w in weights.values()):
        weights = {pid: int(w * 2) for pid, w in weights.items()}
    else:
        weights = {pid: int(w) for pid, w in weights.items()}

    if len(selected_ids) < 2:
        return redirect(url_for("spotify_playlists"))

    t0 = time.time()

    # Fetch all tracks for each playlist — paginate to get the full pool
    all_tracks     = {}
    playlist_names  = {}
    playlist_owners = {}
    for playlist_id in selected_ids:
        playlist_names[playlist_id]  = request.form.get(f"name_{playlist_id}", playlist_id)
        playlist_owners[playlist_id] = request.form.get(f"owner_{playlist_id}", "")
        tracks  = []
        results = sp.playlist_tracks(playlist_id, fields="next,items(track(uri))")
        while results:
            tracks.extend(item["track"]["uri"] for item in results["items"] if item["track"])
            results = sp.next(results) if results["next"] else None
        all_tracks[playlist_id] = tracks

    # Mood filter disabled — Spotify restricted /audio-features for new apps in late 2024
    fallbacks = []
    mood      = "none"

    # Cooldown: exclude tracks used >= max_plays times within window; fall back to full pool if too few remain
    from sqlalchemy import func as _func
    settings    = get_settings()
    cutoff      = datetime.now() - timedelta(days=settings.cooldown_days)
    raw         = db.session.query(
        TrackHistory.track_id,
        _func.count(TrackHistory.id).label("n")
    ).filter(
        TrackHistory.provider == "spotify",
        TrackHistory.used_at  >= cutoff
    ).group_by(TrackHistory.track_id).all()
    on_cooldown = {r.track_id for r in raw if r.n >= settings.cooldown_max_plays}
    cooldown_excluded = sum(sum(1 for t in tracks if t in on_cooldown) for tracks in all_tracks.values())
    for pid in list(all_tracks):
        fresh = [t for t in all_tracks[pid] if t not in on_cooldown]
        all_tracks[pid] = fresh if len(fresh) >= block_size else all_tracks[pid]

    # Build weighted cycle from non-pinned playlists, then shuffle once
    non_pinned = [pid for pid in selected_ids if pid != pinned_id]
    cycle      = []
    for pid in non_pinned:
        cycle.extend([pid] * weights.get(pid, 1))
    random.shuffle(cycle)

    # Cover art: Spotify generates a 2x2 grid from the first 4 tracks.
    # Optionally seed one track from each of the first 4 playlists so the cover represents all genres.
    seed_cover = request.form.get("cover_art") == "on"
    cover_uris = []
    if seed_cover:
        cover_ids  = cycle[:4]
        cover_uris = [random.choice(all_tracks[pid]) for pid in cover_ids if all_tracks[pid]]

    # Build the main block list, inserting a pinned block every pin_interval non-pinned blocks.
    # Re-shuffle the cycle each repeat so same-playlist blocks don't cluster at repeat boundaries.
    block_uris   = []
    block_count  = 0
    pinned_pool  = all_tracks.get(pinned_id, []) if pinned_id in all_tracks else []
    last_pid     = None
    for _ in range(repeats):
        shuffled = cycle[:]
        random.shuffle(shuffled)
        # If the first entry matches the last playlist of the previous repeat, swap it away
        if last_pid and shuffled and shuffled[0] == last_pid:
            for i in range(1, len(shuffled)):
                if shuffled[i] != last_pid:
                    shuffled[0], shuffled[i] = shuffled[i], shuffled[0]
                    break
        for playlist_id in shuffled:
            tracks = all_tracks[playlist_id]
            sample = random.sample(tracks, min(block_size, len(tracks)))
            block_uris.extend(sample)
            block_count += 1
            if pinned_pool and block_count % pin_interval == 0:
                block_uris.extend(random.sample(pinned_pool, min(block_size, len(pinned_pool))))
        last_pid = shuffled[-1] if shuffled else last_pid

    # Prepend cover tracks, remove them from block list, then deduplicate preserving order
    cover_set  = set(cover_uris)
    block_uris = [uri for uri in block_uris if uri not in cover_set]
    seen       = set(cover_uris)
    deduped    = []
    for uri in block_uris:
        if uri not in seen:
            seen.add(uri)
            deduped.append(uri)
    track_uris = cover_uris + deduped

    # Create playlist
    user_id        = sp.me()["id"]
    prefix        = request.form.get("playlist_prefix", "").strip()
    if prefix:
        base = f"{prefix} : Block Mix"
    else:
        base = f"{datetime.now().strftime('%A')} {_period_of_day()} : Block Mix"
    playlist_name = f"{base} : {_date_label()}"
    new_playlist  = sp.user_playlist_create(user_id, playlist_name, public=False)
    # Add tracks in batches of 100 — Spotify's API limit per request
    for i in range(0, len(track_uris), 100):
        sp.playlist_add_items(new_playlist["id"], track_uris[i:i + 100])

    cp = CreatedPlaylist(
        playlist_id=new_playlist["id"],
        name=new_playlist["name"],
        tool="Block Mix",
        provider="spotify",
        url=new_playlist["external_urls"]["spotify"],
        gen_seconds=round(time.time() - t0, 1),
        track_count=len(track_uris)
    )
    db.session.add(cp)
    db.session.flush()  # get cp.id

    seen_pids = []
    for pid in cycle:
        if pid not in seen_pids:
            seen_pids.append(pid)
    if pinned_id and pinned_id not in seen_pids:
        seen_pids.append(pinned_id)
    for pos, pid in enumerate(seen_pids):
        db.session.add(BuildSource(
            created_playlist_id=cp.id,
            playlist_id=pid,
            playlist_name=playlist_names.get(pid, pid),
            owner_id=playlist_owners.get(pid) or None,
            position=pos
        ))
    db.session.commit()

    _record_usage(selected_ids, "spotify")

    for uri in track_uris:
        db.session.add(TrackHistory(track_id=uri, provider="spotify"))
    db.session.commit()

    return render_template("spotify_done.html", playlist=new_playlist, track_count=len(track_uris),
                           cooldown_excluded=cooldown_excluded,
                           source_names=list(playlist_names.values()))


@app.route("/spotify/album-blaster")
def album_blaster():
    sp = get_spotify_client()
    if not sp:
        return redirect(url_for("spotify_login"))

    user_id = sp.me()["id"]
    playlists, cache_updated_at, _ = get_cached_playlists(sp, user_id)

    all_tags = PlaylistTag.query.filter_by(user_id="local").all()
    tag_map  = {}
    for t in all_tags:
        tag_map.setdefault(t.playlist_id, []).append(t.tag)

    all_tag_names = sorted({t.tag for t in all_tags})

    return render_template("spotify_album_blaster.html", playlists=playlists, user_id=user_id,
                           tag_map=tag_map, all_tags=all_tag_names,
                           cache_updated_at=cache_updated_at,
                           cache_refresh_url=url_for("cache_refresh") + "?next=" + request.path)


@app.route("/spotify/album-blaster/<playlist_id>")
def album_blaster_tracks(playlist_id):
    sp = get_spotify_client()
    if not sp:
        return redirect(url_for("spotify_login"))

    # Fetch playlist name
    playlist = sp.playlist(playlist_id, fields="name")

    # Fetch all tracks with album info
    tracks  = []
    results = sp.playlist_tracks(playlist_id, fields="next,items(track(id,name,artists(name),album(name)))")
    while results:
        for item in results["items"]:
            if item["track"] and item["track"]["id"]:
                tracks.append(item["track"])
        results = sp.next(results) if results["next"] else None

    return render_template("spotify_album_tracks.html", tracks=tracks, playlist=playlist)


@app.route("/spotify/album-blast", methods=["POST"])
def album_blast():
    sp = get_spotify_client()
    if not sp:
        return redirect(url_for("spotify_login"))

    track_ids = request.form.getlist("track_ids")
    t0 = time.time()

    # Get album IDs from selected tracks — batch 50 at a time
    seen_album_ids = set()
    album_ids      = []
    for i in range(0, len(track_ids), 50):
        batch = sp.tracks(track_ids[i:i + 50])
        for track in batch["tracks"]:
            if track:
                aid = track["album"]["id"]
                if aid not in seen_album_ids:
                    seen_album_ids.add(aid)
                    album_ids.append((aid, track["album"]["name"]))

    # Fetch all tracks from each album
    track_uris  = []
    album_names = []
    for album_id, album_name in album_ids:
        album_names.append(album_name)
        results = sp.album_tracks(album_id)
        while results:
            track_uris.extend(item["uri"] for item in results["items"])
            results = sp.next(results) if results["next"] else None

    # Create new playlist and add tracks in batches of 100
    user_id      = sp.me()["id"]
    new_playlist = sp.user_playlist_create(
        user_id, f"Album Blast {_now_label()}", public=False
    )
    for i in range(0, len(track_uris), 100):
        sp.playlist_add_items(new_playlist["id"], track_uris[i:i + 100])

    db.session.add(CreatedPlaylist(
        playlist_id=new_playlist["id"],
        name=new_playlist["name"],
        tool="Album Blast",
        provider="spotify",
        url=new_playlist["external_urls"]["spotify"],
        gen_seconds=round(time.time() - t0, 1),
        track_count=len(track_uris)
    ))
    db.session.commit()

    return render_template(
        "spotify_album_blast_done.html",
        playlist=new_playlist,
        track_count=len(track_uris),
        album_names=album_names
    )


# ---------------------------------------------------------------
# Routes — Tagging
# ---------------------------------------------------------------

@app.route("/spotify/tag/add", methods=["POST"])
def tag_add():
    playlist_id = request.json.get("playlist_id")
    tag         = request.json.get("tag", "").strip().lower()
    if not playlist_id or not tag:
        return jsonify({"error": "missing fields"}), 400
    try:
        db.session.add(PlaylistTag(user_id="local", playlist_id=playlist_id, tag=tag))
        db.session.commit()
    except Exception:
        db.session.rollback()  # tag already exists — ignore duplicate
    tags = [r.tag for r in PlaylistTag.query.filter_by(user_id="local", playlist_id=playlist_id).order_by(PlaylistTag.tag)]
    return jsonify({"tags": tags})


@app.route("/spotify/tag/remove", methods=["POST"])
def tag_remove():
    playlist_id = request.json.get("playlist_id")
    tag         = request.json.get("tag", "").strip().lower()
    PlaylistTag.query.filter_by(user_id="local", playlist_id=playlist_id, tag=tag).delete()
    db.session.commit()
    tags = [r.tag for r in PlaylistTag.query.filter_by(user_id="local", playlist_id=playlist_id).order_by(PlaylistTag.tag)]
    return jsonify({"tags": tags})


@app.route("/spotify/tags/all")
def tags_all():
    """Return every unique tag for autocomplete."""
    tags = [r.tag for r in db.session.query(PlaylistTag.tag).filter_by(user_id="local").distinct().order_by(PlaylistTag.tag)]
    return jsonify({"tags": tags})


@app.route("/spotify/manage")
def spotify_manage():
    sp = get_spotify_client()
    if not sp:
        return redirect(url_for("spotify_login"))

    user_id = sp.me()["id"]
    playlists, cache_updated_at, _ = get_cached_playlists(sp, user_id)

    # Build a dict of {playlist_id: [tags]} for the template
    all_tags = PlaylistTag.query.filter_by(user_id="local").all()
    tag_map  = {}
    for t in all_tags:
        tag_map.setdefault(t.playlist_id, []).append(t.tag)

    usages    = PlaylistUsage.query.filter_by(provider="spotify").all()
    usage_map = {u.playlist_id: u for u in usages}

    return render_template("spotify_manage.html", playlists=playlists, user_id=user_id,
                           tag_map=tag_map, usage_map=usage_map, cache_updated_at=cache_updated_at,
                           cache_refresh_url=url_for("cache_refresh") + "?next=" + request.path)


@app.route("/spotify/preview/<playlist_id>")
def spotify_preview(playlist_id):
    sp = get_spotify_client()
    if not sp:
        return {"error": "not authenticated"}, 401

    results = sp.playlist_tracks(playlist_id, fields="items(track(name,artists(name)))", limit=5)
    tracks  = []
    for item in results["items"][:5]:
        if item["track"]:
            track  = item["track"]
            artist = track["artists"][0]["name"] if track["artists"] else "Unknown"
            tracks.append(f"{track['name']} — {artist}")

    return {"tracks": tracks}


@app.route("/spotify/toggle-visibility/<playlist_id>", methods=["POST"])
def spotify_toggle_visibility(playlist_id):
    sp = get_spotify_client()
    if not sp:
        return redirect(url_for("spotify_login"))

    playlist = sp.playlist(playlist_id, fields="public")
    sp.playlist_change_details(playlist_id, public=not playlist["public"])
    return redirect(url_for("spotify_manage"))


@app.route("/spotify/delete", methods=["POST"])
def spotify_delete():
    sp = get_spotify_client()
    if not sp:
        return redirect(url_for("spotify_login"))

    playlist_ids = request.form.getlist("playlist_ids")
    for playlist_id in playlist_ids:
        sp.current_user_unfollow_playlist(playlist_id)  # unfollow = delete for owned playlists

    return redirect(url_for("spotify_manage"))


@app.route("/spotify/cache/refresh")
def cache_refresh():
    sp = get_spotify_client()
    if not sp:
        return redirect(url_for("spotify_login"))
    user_id = sp.me()["id"]
    cache = PlaylistCache.query.filter_by(user_id=user_id).first()
    if cache:
        db.session.delete(cache)
        db.session.commit()
    next_url = request.args.get("next", "")
    # Reject absolute URLs (external redirects) — only allow internal paths
    if not next_url or urlparse(next_url).scheme:
        next_url = url_for("spotify_playlists")
    return redirect(next_url)


# ---------------------------------------------------------------
# Routes — Plex
# ---------------------------------------------------------------

def _plex_or_bust():
    """Return a PlexServer or a redirect response. Caller checks type."""
    plex = get_plex()
    if not plex:
        return None, redirect(url_for("plex_not_configured"))
    return plex, None


@app.route("/plex")
def plex_index():
    _, err = _plex_or_bust()
    if err:
        return err
    return redirect(url_for("plex_playlists"))


@app.route("/plex/not-configured")
def plex_not_configured():
    return render_template("plex_not_configured.html")


@app.route("/plex/playlists")
def plex_playlists():
    plex, err = _plex_or_bust()
    if err:
        return err

    playlists = [p for p in plex.playlists()
                 if p.playlistType == "audio" and p.leafCount >= PLEX_MIN_TRACKS
                 and "Block Mix" not in p.title]

    usage_records   = PlaylistUsage.query.filter_by(provider="plex").all()
    usage_map_count = {r.playlist_id: r.use_count for r in usage_records}

    return render_template("plex_playlists.html", playlists=playlists, usage_map_count=usage_map_count)


@app.route("/plex/build", methods=["POST"])
def plex_build():
    plex, err = _plex_or_bust()
    if err:
        return err

    selected_keys = request.form.getlist("playlist_ids")
    block_size    = max(1, min(10, int(request.form.get("block_size", 4))))
    repeats       = max(1, min(10, int(request.form.get("repeats", 1))))
    pinned_key    = request.form.get("pinned_playlist_id", "").strip()
    pin_interval  = max(1, min(10, int(request.form.get("pin_interval", 1))))
    weights       = {k: max(0.5, min(5, float(request.form.get(f"weight_{k}", 1)))) for k in selected_keys}
    if any(w == 0.5 for w in weights.values()):
        weights = {k: int(w * 2) for k, w in weights.items()}
    else:
        weights = {k: int(w) for k, w in weights.items()}

    if len(selected_keys) < 2:
        return redirect(url_for("plex_playlists"))

    t0 = time.time()

    # Fetch all tracks from each selected playlist — skip fetchItem, go direct to items endpoint
    all_tracks = {}
    for key in selected_keys:
        all_tracks[key] = plex.fetchItems(f"/playlists/{key}/items")

    # Cooldown: exclude tracks used >= max_plays times within window; fall back to full pool if too few remain
    from sqlalchemy import func as _func
    settings    = get_settings()
    cutoff      = datetime.now() - timedelta(days=settings.cooldown_days)
    raw         = db.session.query(
        TrackHistory.track_id,
        _func.count(TrackHistory.id).label("n")
    ).filter(
        TrackHistory.provider == "plex",
        TrackHistory.used_at  >= cutoff
    ).group_by(TrackHistory.track_id).all()
    on_cooldown = {r.track_id for r in raw if r.n >= settings.cooldown_max_plays}
    for key in list(all_tracks):
        fresh = [t for t in all_tracks[key] if str(t.ratingKey) not in on_cooldown]
        all_tracks[key] = fresh if len(fresh) >= block_size else all_tracks[key]

    # Build weighted cycle from non-pinned playlists, then shuffle once
    non_pinned = [k for k in selected_keys if k != pinned_key]
    cycle      = []
    for k in non_pinned:
        cycle.extend([k] * weights.get(k, 1))
    random.shuffle(cycle)

    block_tracks = []
    block_count  = 0
    pinned_pool  = all_tracks.get(pinned_key, []) if pinned_key in all_tracks else []
    last_key     = None
    for _ in range(repeats):
        shuffled = cycle[:]
        random.shuffle(shuffled)
        if last_key and shuffled and shuffled[0] == last_key:
            for i in range(1, len(shuffled)):
                if shuffled[i] != last_key:
                    shuffled[0], shuffled[i] = shuffled[i], shuffled[0]
                    break
        for key in shuffled:
            tracks = all_tracks[key]
            sample = random.sample(tracks, min(block_size, len(tracks)))
            block_tracks.extend(sample)
            block_count += 1
            if pinned_pool and block_count % pin_interval == 0:
                block_tracks.extend(random.sample(pinned_pool, min(block_size, len(pinned_pool))))
        last_key = shuffled[-1] if shuffled else last_key

    # Deduplicate preserving order
    seen_keys   = set()
    deduped     = []
    for track in block_tracks:
        if track.ratingKey not in seen_keys:
            seen_keys.add(track.ratingKey)
            deduped.append(track)
    block_tracks = deduped

    prefix       = request.form.get("playlist_prefix", "").strip()
    if prefix:
        base = f"{prefix} : Block Mix"
    else:
        base = f"{datetime.now().strftime('%A')} {_period_of_day()} : Block Mix"
    title        = f"{base} : {_date_label()}"
    new_playlist = plex.createPlaylist(title, items=block_tracks)

    db.session.add(CreatedPlaylist(
        playlist_id=str(new_playlist.ratingKey),
        name=new_playlist.title,
        tool="Block Mix",
        provider="plex",
        gen_seconds=round(time.time() - t0, 1),
        track_count=len(block_tracks)
    ))
    db.session.commit()

    _record_usage(selected_keys, "plex")

    for track in block_tracks:
        db.session.add(TrackHistory(track_id=str(track.ratingKey), provider="plex"))
    db.session.commit()

    return render_template("plex_done.html", playlist=new_playlist, track_count=len(block_tracks))


@app.route("/plex/album-blaster")
def plex_album_blaster():
    plex, err = _plex_or_bust()
    if err:
        return err

    playlists = [p for p in plex.playlists() if p.playlistType == "audio"]
    return render_template("plex_album_blaster.html", playlists=playlists)


@app.route("/plex/album-blaster/<int:playlist_key>")
def plex_album_blaster_tracks(playlist_key):
    plex, err = _plex_or_bust()
    if err:
        return err

    playlist = plex.fetchItem(playlist_key)
    tracks   = [t for t in playlist.items() if t.type == "track"]
    return render_template("plex_album_tracks.html", tracks=tracks, playlist=playlist)


@app.route("/plex/album-blast", methods=["POST"])
def plex_album_blast():
    plex, err = _plex_or_bust()
    if err:
        return err

    track_keys = [int(k) for k in request.form.getlist("track_ids")]
    t0 = time.time()

    # Batch-fetch all selected tracks in one API call, then read parentRatingKey directly
    # from each track object — avoids N*2 sequential calls (fetchItem + album() per track)
    tracks          = plex.fetchItems(track_keys)
    seen_album_keys = set()
    album_keys      = []
    for track in tracks:
        if track.parentRatingKey not in seen_album_keys:
            seen_album_keys.add(track.parentRatingKey)
            album_keys.append(track.parentRatingKey)

    # Batch-fetch all unique albums in one more API call
    albums      = plex.fetchItems(album_keys)
    all_tracks  = []
    album_names = []
    for album in albums:
        album_names.append(album.title)
        all_tracks.extend(album.tracks())

    title        = f"Album Blast {_now_label()}"
    new_playlist = plex.createPlaylist(title, items=all_tracks)

    db.session.add(CreatedPlaylist(
        playlist_id=str(new_playlist.ratingKey),
        name=new_playlist.title,
        tool="Album Blast",
        provider="plex",
        gen_seconds=round(time.time() - t0, 1),
        track_count=len(all_tracks)
    ))
    db.session.commit()

    return render_template("plex_album_blast_done.html",
                           playlist=new_playlist,
                           track_count=len(all_tracks),
                           album_names=album_names)


# ---------------------------------------------------------------
# Routes — Spotify Stats
# ---------------------------------------------------------------

@app.route("/spotify/stats/<playlist_id>")
def spotify_stats(playlist_id):
    sp = get_spotify_client()
    if not sp:
        return redirect(url_for("spotify_login"))

    playlist = sp.playlist(playlist_id, fields="name,tracks.total")

    tracks  = []
    results = sp.playlist_tracks(playlist_id, fields="next,items(track(name,duration_ms,artists(name)))")
    while results:
        for item in results["items"]:
            if item["track"]:
                tracks.append(item["track"])
        results = sp.next(results) if results["next"] else None

    total_ms = sum(t.get("duration_ms", 0) or 0 for t in tracks)
    hours    = total_ms // 3600000
    minutes  = (total_ms % 3600000) // 60000

    artists = {}
    for track in tracks:
        for artist in track.get("artists", []):
            artists[artist["name"]] = artists.get(artist["name"], 0) + 1
    top_artists = sorted(artists.items(), key=lambda x: -x[1])[:10]

    usage     = PlaylistUsage.query.filter_by(playlist_id=playlist_id, provider="spotify").first()
    use_count = usage.use_count if usage else 0

    cp = CreatedPlaylist.query.filter_by(playlist_id=playlist_id, provider="spotify").first()
    build_sources = (BuildSource.query
                     .filter_by(created_playlist_id=cp.id)
                     .order_by(BuildSource.position)
                     .all()) if cp else []

    user_id = sp.me()["id"]
    return render_template("spotify_stats.html", playlist=playlist,
                           track_count=len(tracks), hours=hours, minutes=minutes,
                           unique_artists=len(artists), top_artists=top_artists,
                           use_count=use_count, build_sources=build_sources,
                           user_id=user_id)


@app.route("/spotify/stats")
def spotify_build_history():
    builds = (CreatedPlaylist.query
              .filter_by(tool="Block Mix", provider="spotify")
              .order_by(CreatedPlaylist.created_at.desc())
              .all())
    sources = {}
    if builds:
        build_ids = [b.id for b in builds]
        rows = (BuildSource.query
                .filter(BuildSource.created_playlist_id.in_(build_ids))
                .order_by(BuildSource.created_playlist_id, BuildSource.position)
                .all())
        for row in rows:
            sources.setdefault(row.created_playlist_id, []).append(row)
    return render_template("spotify_build_history.html", builds=builds, sources=sources)


# ---------------------------------------------------------------
# Routes — Plex Stats
# ---------------------------------------------------------------

@app.route("/plex/stats/<int:playlist_key>")
def plex_stats(playlist_key):
    plex, err = _plex_or_bust()
    if err:
        return err

    playlist = plex.fetchItem(playlist_key)
    tracks   = [t for t in playlist.items() if t.type == "track"]

    total_ms = sum(getattr(t, "duration", 0) or 0 for t in tracks)
    hours    = total_ms // 3600000
    minutes  = (total_ms % 3600000) // 60000

    artists = {}
    for track in tracks:
        name = getattr(track, "grandparentTitle", "Unknown")
        artists[name] = artists.get(name, 0) + 1
    top_artists = sorted(artists.items(), key=lambda x: -x[1])[:10]

    usage     = PlaylistUsage.query.filter_by(playlist_id=str(playlist_key), provider="plex").first()
    use_count = usage.use_count if usage else 0

    return render_template("plex_stats.html", playlist=playlist,
                           track_count=len(tracks), hours=hours, minutes=minutes,
                           unique_artists=len(artists), top_artists=top_artists,
                           use_count=use_count)


# ---------------------------------------------------------------
# Routes — Text Import
# ---------------------------------------------------------------


@app.route("/spotify/text-import")
def text_import():
    sp = get_spotify_client()
    if not sp:
        return redirect(url_for("spotify_login"))
    return render_template("spotify_text_import.html")


@app.route("/spotify/text-import/preview", methods=["POST"])
def text_import_preview():
    sp = get_spotify_client()
    if not sp:
        return redirect(url_for("spotify_login"))

    raw_text      = request.form.get("playlist_text", "")
    playlist_name = request.form.get("playlist_name", "").strip() or f"Text Import {_date_label()}"
    mode          = request.form.get("mode", "trust")  # "trust" or "manual"

    # Parse lines: skip blanks and comments
    lines = [l.strip() for l in raw_text.splitlines() if l.strip() and not l.strip().startswith("#")]

    def _uris_from_match(match):
        if match["type"] == "track":
            return [match["uri"]]
        album_id = match["uri"].split(":")[-1]
        uris     = []
        res      = sp.album_tracks(album_id)
        while res:
            uris.extend(t["uri"] for t in res["items"] if t)
            res = sp.next(res) if res["next"] else None
        return uris

    # Search Spotify for each line, then detect and apply list-level type bias
    line_results = [{"original": line, "matches": _search_line(sp, line)} for line in lines]
    list_type    = _detect_list_type(line_results)
    _bias_matches(line_results, list_type)

    if mode == "trust":
        t0        = time.time()
        track_uris = []
        for lr in line_results:
            if lr["matches"]:
                track_uris.extend(_uris_from_match(lr["matches"][0]))

        if not track_uris:
            return render_template("spotify_text_import.html", error="No tracks matched — check your input.")

        user_id      = sp.me()["id"]
        new_playlist = sp.user_playlist_create(user_id, playlist_name, public=False)
        for i in range(0, len(track_uris), 100):
            sp.playlist_add_items(new_playlist["id"], track_uris[i:i + 100])

        db.session.add(CreatedPlaylist(
            playlist_id=new_playlist["id"],
            name=new_playlist["name"],
            tool="Text Import",
            provider="spotify",
            url=new_playlist["external_urls"]["spotify"],
            gen_seconds=round(time.time() - t0, 1),
            track_count=len(track_uris)
        ))
        db.session.commit()

        unmatched = sum(1 for lr in line_results if not lr["matches"])
        return render_template("spotify_done.html", playlist=new_playlist,
                               track_count=len(track_uris), unmatched=unmatched)

    # Manual mode: show preview
    return render_template("spotify_text_import_preview.html",
                           line_results=line_results,
                           playlist_name=playlist_name)


@app.route("/spotify/text-import/build", methods=["POST"])
def text_import_build():
    sp = get_spotify_client()
    if not sp:
        return redirect(url_for("spotify_login"))

    playlist_name = request.form.get("playlist_name", f"Text Import {_date_label()}")
    line_count    = int(request.form.get("line_count", 0))
    raw_uris      = [request.form.get(f"uri_{i}", "skip") for i in range(line_count)]

    t0         = time.time()
    track_uris = []
    for uri in raw_uris:
        if not uri or uri == "skip":
            continue
        if ":album:" in uri:
            album_id = uri.split(":")[-1]
            res      = sp.album_tracks(album_id)
            while res:
                track_uris.extend(t["uri"] for t in res["items"] if t)
                res = sp.next(res) if res["next"] else None
        else:
            track_uris.append(uri)

    if not track_uris:
        return redirect(url_for("text_import"))

    user_id      = sp.me()["id"]
    new_playlist = sp.user_playlist_create(user_id, playlist_name, public=False)
    for i in range(0, len(track_uris), 100):
        sp.playlist_add_items(new_playlist["id"], track_uris[i:i + 100])

    db.session.add(CreatedPlaylist(
        playlist_id=new_playlist["id"],
        name=new_playlist["name"],
        tool="Text Import",
        provider="spotify",
        url=new_playlist["external_urls"]["spotify"],
        gen_seconds=round(time.time() - t0, 1),
        track_count=len(track_uris)
    ))
    db.session.commit()

    return render_template("spotify_done.html", playlist=new_playlist,
                           track_count=len(track_uris), unmatched=0)


# ---------------------------------------------------------------
# Routes — Recently Created
# ---------------------------------------------------------------


# ---------------------------------------------------------------

@app.route("/recently-created")
def recently_created():
    sp   = get_spotify_client()
    plex = get_plex()
    now  = datetime.now()

    thawed = 0
    if sp:
        thawed += auto_thaw("spotify")
    if plex:
        thawed += auto_thaw("plex")
    if thawed:
        flash(f"🌊 {thawed} track{'s' if thawed != 1 else ''} thawed from cooldown", "info")

    records = CreatedPlaylist.query.order_by(CreatedPlaylist.created_at.desc()).all()

    # Verify playlists not checked recently — skip if we can't reach that provider
    changed = False
    for rec in records:
        if not rec.alive:
            continue
        if rec.checked_at and (now - rec.checked_at) < VERIFY_TTL:
            continue
        try:
            if rec.provider == "spotify" and sp:
                sp.playlist(rec.playlist_id, fields="id")
            elif rec.provider == "plex" and plex:
                plex.fetchItem(int(rec.playlist_id))
            else:
                continue  # can't reach provider right now, leave as-is
            rec.alive = True
        except Exception:
            rec.alive = False
        rec.checked_at = now
        changed = True

    if changed:
        db.session.commit()

    cooldown_stats = get_cooldown_stats("spotify") if sp else None
    return render_template("recently_created.html", records=records, cooldown_stats=cooldown_stats)


@app.route("/recently-created/delete/<int:record_id>", methods=["POST"])
def recently_created_delete(record_id):
    """Delete from provider and mark dead — keeps history."""
    rec = CreatedPlaylist.query.get(record_id)
    if rec and rec.alive:
        try:
            if rec.provider == "spotify":
                sp = get_spotify_client()
                if sp:
                    sp.current_user_unfollow_playlist(rec.playlist_id)
            elif rec.provider == "plex":
                plex = get_plex()
                if plex:
                    plex.fetchItem(int(rec.playlist_id)).delete()
        except Exception:
            pass  # already gone — still mark dead
        rec.alive = False
        rec.checked_at = datetime.now()
        db.session.commit()
    return redirect(url_for("recently_created"))


@app.route("/recently-created/remove/<int:record_id>", methods=["POST"])
def recently_created_remove(record_id):
    """Remove from DB only — no provider call."""
    rec = CreatedPlaylist.query.get(record_id)
    if rec:
        db.session.delete(rec)
        db.session.commit()
    return redirect(url_for("recently_created"))


@app.route("/recently-created/scan", methods=["POST"])
def recently_created_scan():
    """Force re-check all alive records regardless of VERIFY_TTL."""
    sp   = get_spotify_client()
    plex = get_plex()
    now  = datetime.now()

    records = CreatedPlaylist.query.filter_by(alive=True).all()
    changed = False
    for rec in records:
        try:
            if rec.provider == "spotify" and sp:
                sp.playlist(rec.playlist_id, fields="id")
            elif rec.provider == "plex" and plex:
                plex.fetchItem(int(rec.playlist_id))
            else:
                continue
            rec.alive = True
        except Exception:
            rec.alive = False
        rec.checked_at = now
        changed = True

    if changed:
        db.session.commit()
    return redirect(url_for("recently_created"))


@app.route("/recently-created/clear-dead", methods=["POST"])
def recently_created_clear_dead():
    CreatedPlaylist.query.filter_by(alive=False).delete()
    db.session.commit()
    return redirect(url_for("recently_created"))


# ---------------------------------------------------------------
# Routes — Settings
# ---------------------------------------------------------------

@app.route("/settings", methods=["GET", "POST"])
def settings_page():
    s = get_settings()
    if request.method == "POST":
        try:
            s.cooldown_days      = max(1, min(365, int(request.form["cooldown_days"])))
            s.cooldown_max_plays = max(1, min(99,  int(request.form["cooldown_max_plays"])))
            db.session.commit()
        except (ValueError, TypeError):
            pass
        return redirect(url_for("settings_page"))
    return render_template("settings.html", settings=s)


@app.route("/settings/thaw-all", methods=["POST"])
def settings_thaw_all():
    from sqlalchemy import func as _func
    now = datetime.now()
    rows = db.session.query(
        TrackHistory.provider,
        _func.count(TrackHistory.track_id.distinct()).label("unique")
    ).group_by(TrackHistory.provider).all()
    for provider, unique in rows:
        tally = ThawTally.query.filter_by(year=now.year, month=now.month, provider=provider).first()
        if tally:
            tally.count += unique
        else:
            db.session.add(ThawTally(year=now.year, month=now.month, provider=provider, count=unique))
    count = sum(u for _, u in rows)
    TrackHistory.query.delete()
    db.session.commit()
    flash(f"🌊 All {count} track{'s' if count != 1 else ''} cleared — everything thawed", "success")
    return redirect(url_for("settings_page"))


if __name__ == "__main__":
    app.run(debug=True)

