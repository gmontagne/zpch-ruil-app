"""Pull trainings + hosts from Spond into data/spond.json and reconcile swaps.

The app never writes to Spond. An admin changes the host in Spond by hand;
the next sync sees the new host and marks the accepted swap as processed.
"""

from datetime import datetime, time, timedelta, timezone

from .spond import SpondClient


def utcnow():
    return datetime.now(timezone.utc)


def iso(dt):
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_iso(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")) if s else None


# --- normalisation ---------------------------------------------------------

def person_key(member):
    """Spond event owners reference profile ids; fall back to the member id."""
    return (member.get("profile") or {}).get("id") or member.get("id")


def normalize(group, raw_events):
    people = {}
    for m in group.get("members", []):
        name = " ".join(p for p in (m.get("firstName"), m.get("lastName")) if p).strip()
        people[person_key(m)] = {
            "name": name or "Onbekend",
            "first_name": m.get("firstName") or name or "Onbekend",
            "email": (m.get("email") or "").lower(),
            "member_id": m.get("id"),
        }

    events = []
    for e in raw_events:
        if e.get("cancelled"):
            continue
        events.append({
            "id": e["id"],
            "title": e.get("heading") or "Training",
            "start": e["startTimestamp"],
            "end": e.get("endTimestamp"),
            "hosts": [o["id"] for o in e.get("owners", []) if o.get("id")],
        })
    events.sort(key=lambda ev: ev["start"])
    return {"people": people, "events": events}


# --- sources ---------------------------------------------------------------

def fetch_spond(cfg, now):
    client = SpondClient(cfg["SPOND_USERNAME"], cfg["SPOND_PASSWORD"])
    group = client.get_group(cfg["SPOND_GROUP_ID"])
    events = client.get_events(
        cfg["SPOND_GROUP_ID"],
        min_start=now - timedelta(days=7),
        max_start=now + timedelta(weeks=cfg["SYNC_WEEKS"]),
        subgroup_id=cfg.get("SPOND_SUBGROUP_ID") or None,
    )
    return group, events


DEMO_PEOPLE = ["Pieter", "Sanne", "Joost", "Anna", "Lies", "Ruud"]
DEMO_SCHEDULE = [  # weekday, hh, mm, title, minutes
    (0, 18, 30, "Jeugd", 75), (0, 19, 45, "Masters", 75), (1, 18, 30, "Jeugd", 75),
    (2, 19, 0, "Selectie", 90), (3, 18, 30, "Jeugd", 75), (4, 19, 0, "Selectie", 90),
    (5, 8, 0, "Wedstrijdgroep", 120),
]


def fetch_demo(cfg, now, store):
    """Fake Spond payloads in the real API shape, so normalize() is exercised."""
    tz = cfg["TZ"]
    group = {"id": "demo", "members": [
        {"id": f"m-{n.lower()}", "firstName": n, "lastName": "", "email": f"{n.lower()}@example.org",
         "profile": {"id": f"p-{n.lower()}"}} for n in DEMO_PEOPLE
    ]}
    overrides = store.load("demo_hosts", {})
    today = now.astimezone(tz).date()
    monday = today - timedelta(days=today.weekday())
    events, n = [], len(DEMO_PEOPLE)
    for week in range(cfg["SYNC_WEEKS"] + 1):
        for idx, (wd, hh, mm, title, minutes) in enumerate(DEMO_SCHEDULE):
            day = monday + timedelta(weeks=week, days=wd)
            start = datetime.combine(day, time(hh, mm), tzinfo=tz)
            eid = f"demo-{day.isoformat()}-{hh:02d}{mm:02d}"
            hosts = [f"p-{DEMO_PEOPLE[(week + idx * 5) % n].lower()}"]
            if title == "Selectie" and wd == 4:  # one training with two hosts
                hosts.append(f"p-{DEMO_PEOPLE[(week + idx * 5 + 3) % n].lower()}")
            hosts = overrides.get(eid, hosts)
            events.append({
                "id": eid, "heading": title,
                "startTimestamp": iso(start), "endTimestamp": iso(start + timedelta(minutes=minutes)),
                "owners": [{"id": h, "response": "accepted"} for h in hosts],
            })
    return group, events


# --- sync + reconcile ------------------------------------------------------

def reconcile(swaps, events, now):
    """Accepted swaps become processed once Spond shows the taker as host."""
    by_id = {e["id"]: e for e in events}
    changed = 0
    for s in swaps:
        ev = by_id.get(s["event_id"])
        if s["status"] != "accepted" or not ev:
            continue
        if s.get("taker_key") in ev["hosts"] and s.get("requester_key") not in ev["hosts"]:
            s["status"] = "processed"
            s["processed_at"] = iso(now)
            changed += 1
    return changed


def run_sync(cfg, store, now=None):
    now = now or utcnow()
    meta = store.load("spond", {})
    meta["last_attempt"] = iso(now)
    try:
        if cfg["DEMO"]:
            group, raw = fetch_demo(cfg, now, store)
        else:
            group, raw = fetch_spond(cfg, now)
        data = normalize(group, raw)
    except Exception as exc:  # keep serving the old cache
        meta["last_error"] = str(exc)
        store.save("spond", meta)
        raise
    data.update(synced_at=iso(now), last_attempt=iso(now), last_error=None,
                source="demo" if cfg["DEMO"] else "spond")
    store.save("spond", data)
    return store.update("swaps", [], lambda swaps: reconcile(swaps, data["events"], now))
