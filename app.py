# ---------------------------------------------------------------
# app.py — Flask playground
# Learning project: Python + Flask vs ColdFusion
# ---------------------------------------------------------------

from flask import Flask, render_template, request, session
from flask_sqlalchemy import SQLAlchemy
from datetime import datetime
import random

app = Flask(__name__)
app.secret_key = "dev-secret-key"
app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///visits.db"  # file-based DB, lives in /instance

db = SQLAlchemy(app)


# ---------------------------------------------------------------
# Models — like defining a CF datasource + table structure
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
# Routes
# ---------------------------------------------------------------

@app.route("/", methods=["GET", "POST"])
def index():
    message = None

    # Handle form submission — write to DB
    if request.method == "POST":
        name = request.form["name"]
        session["name"] = name
        existing = Visit.query.filter_by(name=name).first()  # like SELECT ... WHERE name = ? in CF
        if existing:
            message = f"{name} is already in the list."
        else:
            db.session.add(Visit(name=name))
            db.session.commit()
            message = f"Hey, {name}! Visit recorded."

    # Read all visits from DB — like <cfquery> SELECT * FROM visits
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
    return render_template("index.html",
        date=datetime.now().strftime("%D"),
        message="You've been logged out.",
        hit_count=Visit.query.count(),
        compliment=random.choice(compliments),
        visits=Visit.query.order_by(Visit.visited_at.desc()).all()
    )


@app.route("/about")
def about():
    return render_template("about.html")


# ---------------------------------------------------------------
# Init DB — creates the tables if they don't exist yet
# ---------------------------------------------------------------

with app.app_context():
    db.create_all()
