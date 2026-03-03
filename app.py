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

    selected_ids = request.form.getlist("playlist_ids")  # list of checked playlist IDs
    block_size   = int(request.form.get("block_size", 4))
    repeats      = int(request.form.get("repeats", 1))

    if len(selected_ids) < 2:
        return redirect(url_for("spotify_playlists"))

    # Fetch all tracks for each playlist upfront — paginate to get the full pool
    all_tracks = {}
    for playlist_id in selected_ids:
        tracks  = []
        results = sp.playlist_tracks(playlist_id, fields="next,items(track(uri))")
        while results:
            tracks.extend(item["track"]["uri"] for item in results["items"] if item["track"])
            results = sp.next(results) if results["next"] else None
        all_tracks[playlist_id] = tracks

    # Build the track list — each repeat cycles through all playlists picking a fresh random block
    track_uris = []
    for _ in range(repeats):
        for playlist_id in selected_ids:
            tracks = all_tracks[playlist_id]
            sample = random.sample(tracks, min(block_size, len(tracks)))
            track_uris.extend(sample)

    # Create a new playlist and add the tracks
    user_id      = sp.me()["id"]
    new_playlist = sp.user_playlist_create(user_id, f"Block Mix {datetime.now().strftime('%m/%d %I:%M%p')}", public=False)
    sp.playlist_add_items(new_playlist["id"], track_uris)

    return render_template("spotify_done.html", playlist=new_playlist, track_count=len(track_uris))


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
