# ---------------------------------------------------------------
# app.py — Flask playground
# Learning project: Python + Flask vs ColdFusion
# ---------------------------------------------------------------

from flask import Flask, render_template, request, session, redirect, url_for
from flask_sqlalchemy import SQLAlchemy
from datetime import datetime
from dotenv import load_dotenv
import random
import os
import spotipy
from spotipy.oauth2 import SpotifyOAuth

load_dotenv()  # loads values from .env into os.environ

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-key")
app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///visits.db"

db = SQLAlchemy(app)


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


# ---------------------------------------------------------------
# Models
# ---------------------------------------------------------------

class Visit(db.Model):
    id         = db.Column(db.Integer, primary_key=True)
    name       = db.Column(db.String(100), nullable=False)
    visited_at = db.Column(db.DateTime, default=datetime.now)


# ---------------------------------------------------------------
# Data
# ---------------------------------------------------------------

compliments = [
    "You write really clean code.",
    "You ask great questions.",
    "Your variable names are surprisingly readable.",
    "You would have caught that bug eventually.",
    "Honestly, ColdFusion wasn't that bad.",
]


# ---------------------------------------------------------------
# Routes — General
# ---------------------------------------------------------------

@app.route("/", methods=["GET", "POST"])
def index():
    message = None

    if request.method == "POST":
        name = request.form["name"]
        session["name"] = name
        existing = Visit.query.filter_by(name=name).first()
        if existing:
            message = f"{name} is already in the list."
        else:
            db.session.add(Visit(name=name))
            db.session.commit()
            message = f"Hey, {name}! Visit recorded."

    visits = Visit.query.order_by(Visit.visited_at.desc()).all()
    hit_count = Visit.query.count()

    return render_template(
        "index.html",
        date=datetime.now().strftime("%D"),
        message=message,
        hit_count=hit_count,
        compliment=random.choice(compliments),
        visits=visits
    )


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

    # Paginate through all playlists (Spotify returns max 50 at a time)
    playlists = []
    results = sp.current_user_playlists(limit=50)
    while results:
        playlists.extend(results["items"])
        results = sp.next(results) if results["next"] else None

    return render_template("spotify_playlists.html", playlists=playlists)


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
        user_id, f"{mood_label}Block Mix {datetime.now().strftime('%m/%d %I:%M%p')}", public=False
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


@app.route("/spotify/manage")
def spotify_manage():
    sp = get_spotify_client()
    if not sp:
        return redirect(url_for("spotify_login"))

    user_id   = sp.me()["id"]
    playlists = []
    results   = sp.current_user_playlists(limit=50)
    while results:
        playlists.extend(results["items"])
        results = sp.next(results) if results["next"] else None

    return render_template("spotify_manage.html", playlists=playlists, user_id=user_id)


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


@app.route("/spotify/delete", methods=["POST"])
def spotify_delete():
    sp = get_spotify_client()
    if not sp:
        return redirect(url_for("spotify_login"))

    playlist_ids = request.form.getlist("playlist_ids")
    for playlist_id in playlist_ids:
        sp.current_user_unfollow_playlist(playlist_id)  # unfollow = delete for owned playlists

    return redirect(url_for("spotify_manage"))


# ---------------------------------------------------------------
# Init DB
# ---------------------------------------------------------------

with app.app_context():
    db.create_all()
