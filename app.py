# ---------------------------------------------------------------
# app.py — Flask playground
# Learning project: Python + Flask vs ColdFusion
# ---------------------------------------------------------------

from flask import Flask, render_template, request
from datetime import datetime
import random

app = Flask(__name__)


# ---------------------------------------------------------------
# Data — in a real app this would come from a database
# ---------------------------------------------------------------

hit_count = 0  # resets when the server restarts

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
    global hit_count
    hit_count += 1

    date_str = datetime.now().strftime("%D")
    message = None

    # Handle form submission
    if request.method == "POST":
        name = request.form["name"]  # like form.name in CF
        message = f"Hey, {name}!"

    compliment = random.choice(compliments)

    return render_template(
        "index.html",
        date=date_str,
        message=message,
        hit_count=hit_count,
        compliment=compliment
    )


@app.route("/about")
def about():
    return render_template("about.html")
