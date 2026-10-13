# ZPCH host-ruilen

A small Flask app for swapping training-host duties. Trainings and their hosts come from Spond (read-only). Swap requests live in JSON files.

## How it works

1. A host clicks **Ruilen** on one of their trainings → an open request (red).
2. Someone else clicks **Ik neem het over** → accepted, but Spond still shows the old host (amber).
3. An admin changes the host in Spond (**Beheer → Open in Spond**).
4. The next sync sees the taker as host in Spond and marks the swap processed (green).

Open requests for trainings within `ESCALATE_DAYS` count as escalated. Accepted swaps that have waited more than `LATE_AFTER_DAYS` are flagged *Te laat* for admins.

## Run it

```sh
python -m venv .venv && .venv/bin/pip install -r requirements-dev.txt

# Demo mode (no Spond credentials): fake trainings, users pieter/sanne/joost/anna/lies/ruud, password "demo"
.venv/bin/flask --app app seed-demo
.venv/bin/flask --app app run          # http://localhost:5000, log in as pieter (admin)

.venv/bin/pytest
```

### With Spond

Set the variables from `.env.example` (`SPOND_USERNAME`, `SPOND_PASSWORD`, `SPOND_GROUP_ID`, optionally `SPOND_SUBGROUP_ID`). The Spond account must be able to see the group's events and members. Then:

```sh
.venv/bin/flask --app app add-user pieter --name Pieter --email pieter@club.nl --admin
.venv/bin/flask --app app sync
```

Each user is linked to a Spond member through their e-mail address, or through `--spond-id` with the Spond profile id. The app syncs on its own when a page loads and the last sync is older than `SYNC_MINUTES`. Admins can also sync by hand.

## Layout

| Path | What |
|---|---|
| `ruil/spond.py` | Minimal Spond REST client (`auth2/login`, `groups/`, `sponds/`). Event hosts are `owners`. |
| `ruil/sync.py` | Fetch + normalise into `data/spond.json`; reconcile accepted swaps; demo data source |
| `ruil/store.py` | JSON file store (`data/users.json`, `data/swaps.json`, `data/spond.json`) |
| `ruil/views.py` | Routes + the per-user view model |
| `ruil/templates`, `ruil/static` | UI ported from the Host Swap Tracker design |

## MVP limitations

- Runs as a single process because the JSON store isn't safe across processes. A real database comes next.
- There is no CSRF token; the app relies on `SameSite=Lax` cookies.
- The daily 08:00 post in the Spond host chat is not built yet.
- The Spond API is unofficial and can change without notice.
