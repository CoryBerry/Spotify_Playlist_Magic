# ---------------------------------------------------------------
# app.py — Spotify Tools
# ---------------------------------------------------------------

from flask import Flask, render_template, request, session, redirect, url_for, jsonify
from flask_sqlalchemy import SQLAlchemy
from datetime import datetime, timedelta
from dotenv import load_dotenv
import random
import json
import os
import spotipy
from spotipy.oauth2 import SpotifyOAuth

try:
    from plexapi.server import PlexServer as _PlexServer
    PLEX_AVAILABLE = True
except ImportError:
    PLEX_AVAILABLE = False

load_dotenv()

def _now_label():
    return datetime.now().strftime('%m/%d %I:%M%p')

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-key")
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


with app.app_context():
    db.create_all()


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


CACHE_TTL = timedelta(hours=1)

def get_cached_playlists(sp, user_id):
    """Return (playlists, updated_at, from_cache).
    Serves from DB cache if under 1 hour old; otherwise re-fetches from Spotify."""
    cache = PlaylistCache.query.filter_by(user_id=user_id).first()
    now   = datetime.now()

    if cache and (now - cache.updated_at) < CACHE_TTL:
        return json.loads(cache.data), cache.updated_at, True

    # Fetch fresh from Spotify
    playlists = []
    results   = sp.current_user_playlists(limit=50)
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


@app.route("/about")
def about():
    return render_template("about.html")


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

    user_id = sp.me()["id"]
    playlists, cache_updated_at, _ = get_cached_playlists(sp, user_id)

    # Only show playlists with enough tracks to be useful for block mixing
    playlists = [p for p in playlists if p["tracks"]["total"] >= 20]

    # Pass tag data so Block Mix can filter by tag
    all_tags = PlaylistTag.query.filter_by(user_id="local").all()
    tag_map  = {}
    for t in all_tags:
        tag_map.setdefault(t.playlist_id, []).append(t.tag)

    all_tag_names = sorted({t.tag for t in all_tags})

    return render_template("spotify_playlists.html", playlists=playlists, tag_map=tag_map,
                           all_tags=all_tag_names, user_id=user_id,
                           cache_updated_at=cache_updated_at,
                           cache_refresh_url=url_for("cache_refresh") + "?next=" + request.path)


@app.route("/spotify/build", methods=["POST"])
def spotify_build():
    sp = get_spotify_client()
    if not sp:
        return redirect(url_for("spotify_login"))

    selected_ids = request.form.getlist("playlist_ids")
    block_size   = int(request.form.get("block_size", 4))
    repeats      = int(request.form.get("repeats", 1))
    mood         = request.form.get("mood", "none")

    if len(selected_ids) < 2:
        return redirect(url_for("spotify_playlists"))

    # Fetch all tracks for each playlist — paginate to get the full pool
    all_tracks     = {}
    playlist_names = {}
    for playlist_id in selected_ids:
        playlist_names[playlist_id] = request.form.get(f"name_{playlist_id}", playlist_id)
        tracks  = []
        results = sp.playlist_tracks(playlist_id, fields="next,items(track(uri))")
        while results:
            tracks.extend(item["track"]["uri"] for item in results["items"] if item["track"])
            results = sp.next(results) if results["next"] else None
        all_tracks[playlist_id] = tracks

    # Mood filter disabled — Spotify restricted /audio-features for new apps in late 2024
    fallbacks = []
    mood      = "none"

    # Randomize playlist order so blocks aren't always in the same sequence
    random.shuffle(selected_ids)

    # Cover art fix — Spotify generates a 2x2 grid from the first 4 tracks.
    # Pull one track from each of the first 4 playlists so the cover art represents all genres.
    cover_ids  = selected_ids[:4]
    cover_uris = [random.choice(all_tracks[pid]) for pid in cover_ids if all_tracks[pid]]

    # Build the main block list
    block_uris = []
    for _ in range(repeats):
        for playlist_id in selected_ids:
            tracks = all_tracks[playlist_id]
            sample = random.sample(tracks, min(block_size, len(tracks)))
            block_uris.extend(sample)

    # Prepend cover tracks, then blocks — exclude cover tracks from blocks to avoid dupes
    cover_set  = set(cover_uris)
    block_uris = [uri for uri in block_uris if uri not in cover_set]
    track_uris = cover_uris + block_uris

    # Create playlist — include mood in name if active
    user_id      = sp.me()["id"]
    mood_label   = f"{mood.title()} " if mood != "none" else ""
    new_playlist = sp.user_playlist_create(
        user_id, f"{mood_label}Block Mix {_now_label()}", public=False
    )
    # Add tracks in batches of 100 — Spotify's API limit per request
    for i in range(0, len(track_uris), 100):
        sp.playlist_add_items(new_playlist["id"], track_uris[i:i + 100])

    return render_template(
        "spotify_done.html",
        playlist=new_playlist,
        track_count=len(track_uris),
        mood=mood,
        fallbacks=fallbacks
    )


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

    return render_template("spotify_album_blaster.html", playlists=playlists, user_id=user_id,
                           tag_map=tag_map, cache_updated_at=cache_updated_at,
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

    return render_template("spotify_manage.html", playlists=playlists, user_id=user_id,
                           tag_map=tag_map, cache_updated_at=cache_updated_at,
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
    next_url = request.args.get("next") or url_for("spotify_playlists")
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
                 if p.playlistType == "audio" and p.leafCount >= PLEX_MIN_TRACKS]
    return render_template("plex_playlists.html", playlists=playlists)


@app.route("/plex/build", methods=["POST"])
def plex_build():
    plex, err = _plex_or_bust()
    if err:
        return err

    selected_keys = request.form.getlist("playlist_ids")
    block_size    = int(request.form.get("block_size", 4))
    repeats       = int(request.form.get("repeats", 1))

    if len(selected_keys) < 2:
        return redirect(url_for("plex_playlists"))

    # Fetch all tracks from each selected playlist — skip fetchItem, go direct to items endpoint
    all_tracks = {}
    for key in selected_keys:
        all_tracks[key] = plex.fetchItems(f"/playlists/{key}/items")

    random.shuffle(selected_keys)

    block_tracks = []
    for _ in range(repeats):
        for key in selected_keys:
            tracks = all_tracks[key]
            sample = random.sample(tracks, min(block_size, len(tracks)))
            block_tracks.extend(sample)

    title        = f"Block Mix {_now_label()}"
    new_playlist = plex.createPlaylist(title, items=block_tracks)

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

    return render_template("plex_album_blast_done.html",
                           playlist=new_playlist,
                           track_count=len(all_tracks),
                           album_names=album_names)


