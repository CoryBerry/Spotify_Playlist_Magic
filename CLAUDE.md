# CLAUDE.md — Flask Learning Project

> Briefing file for Claude Code. Read this before touching anything.

---

## What this is

A learning project: Python + Flask, coming from a ColdFusion background.
Goal is to understand modern web dev patterns before building real apps (music app, etc.).

---

## Mental model: CF → Flask/Python

| ColdFusion concept | Flask/Python equivalent |
|---|---|
| `<cfquery>` | `Visit.query.filter_by(...).all()` |
| `<cfinsert>` | `db.session.add(Visit(...)); db.session.commit()` |
| `<cfset session.name = name>` | `session["name"] = name` |
| Datasource | `SQLALCHEMY_DATABASE_URI` (currently SQLite) |
| `<cfif>` / `<cfelse>` | `if` / `else` |
| `<cfloop>` | `{% for x in items %}` in Jinja2 |

---

## Stack

- **Framework:** Flask
- **DB:** SQLAlchemy + SQLite (`instance/visits.db`)
- **Templates:** Jinja2 (extends `base.html`)
- **Frontend:** Bootstrap 5 (via CDN)
- **Session:** Flask built-in (cookie-based, `secret_key` required)

---

## Current app structure

```
app.py              ← main app, routes, models, DB init
templates/
  base.html         ← shared layout (Bootstrap, nav, blocks)
  index.html        ← home page (form, visits table, compliment)
  about.html        ← static about page
instance/
  visits.db         ← SQLite DB (auto-created, don't commit)
```

---

## Models

```python
class Visit(db.Model):
    id         = db.Column(db.Integer, primary_key=True)
    name       = db.Column(db.String(100), nullable=False)
    visited_at = db.Column(db.DateTime, default=datetime.now)
```

---

## Routes

| Route | Methods | What it does |
|---|---|---|
| `/` | GET, POST | Home page. POST saves name to DB (with dupe check) |
| `/logout` | GET | Clears session, redirects to home |
| `/about` | GET | Static about page |

---

## Key patterns learned so far

**Duplicate check before insert:**
```python
existing = Visit.query.filter_by(name=name).first()  # SELECT TOP 1 WHERE name = ?
if existing:
    message = f"{name} is already in the list."
else:
    db.session.add(Visit(name=name))
    db.session.commit()
```

**Passing data to templates:**
```python
return render_template("index.html",
    date=datetime.now().strftime("%D"),
    message=message,
    visits=visits,
    compliment=random.choice(compliments)
)
```

**DB init (runs on startup):**
```python
with app.app_context():
    db.create_all()
```

---

## Things NOT done yet (next steps)

- [ ] User auth (login / signup with password hashing)
- [ ] `redirect()` after POST (prevent form resubmit on refresh)
- [ ] Flash messages (cleaner than passing `message` manually)
- [ ] Multiple models with relationships
- [ ] File uploads
- [ ] Blueprints (when app gets bigger)

---

## Conventions / preferences

- Comments explain the CF equivalent where helpful
- Keep it simple — this is a learning project, not production
- SQLite is fine for now; Postgres later when needed
- Don't over-engineer; explain tradeoffs when suggesting changes
