import secrets
from datetime import date, timedelta
from functools import wraps

from flask import (Blueprint, abort, current_app, flash, g, redirect, render_template,
                   request, session, url_for)
from werkzeug.security import check_password_hash

from . import fmt
from .spond import SPOND_EVENT_URL
from .sync import iso, parse_iso, run_sync, utcnow

bp = Blueprint("main", __name__)


def store():
    return current_app.extensions["store"]


# --- auth ------------------------------------------------------------------

def login_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if g.user is None:
            return redirect(url_for("main.login", next=request.path))
        return fn(*args, **kwargs)
    return wrapper


def admin_required(fn):
    @wraps(fn)
    @login_required
    def wrapper(*args, **kwargs):
        if not g.user.get("admin"):
            abort(403)
        return fn(*args, **kwargs)
    return wrapper


@bp.before_app_request
def load_user():
    username = session.get("user")
    users = store().load("users", {})
    g.user = dict(users[username], username=username) if username in users else None
    if g.user:
        maybe_sync()


def maybe_sync():
    """Lazy sync: at most one attempt per SYNC_MINUTES, failures keep the old cache."""
    cfg = current_app.config
    last = parse_iso(store().load("spond", {}).get("last_attempt"))
    if last and utcnow() - last < timedelta(minutes=cfg["SYNC_MINUTES"]):
        return
    try:
        run_sync(cfg, store())
    except Exception:
        current_app.logger.exception("Spond sync failed")


@bp.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        username = request.form.get("username", "").strip().lower()
        user = store().load("users", {}).get(username)
        if user and check_password_hash(user["password_hash"], request.form.get("password", "")):
            session.clear()
            session["user"] = username
            session.permanent = bool(request.form.get("remember"))
            nxt = request.args.get("next", "")
            return redirect(nxt if nxt.startswith("/") and not nxt.startswith("//") else url_for("main.week"))
        error = "Onbekende gebruikersnaam of verkeerd wachtwoord."
    return render_template("login.html", error=error)


@bp.post("/logout")
def logout():
    session.clear()
    return redirect(url_for("main.login"))


# --- view model ------------------------------------------------------------

class Board:
    """Everything a page needs: Spond cache + swaps, seen from the current user."""

    def __init__(self, user):
        cfg = current_app.config
        self.cfg = cfg
        self.tz = cfg["TZ"]
        self.now = utcnow().astimezone(self.tz)
        spond = store().load("spond", {})
        self.spond = spond
        self.people = spond.get("people", {})
        self.events = spond.get("events", [])
        self.by_id = {e["id"]: e for e in self.events}
        self.swaps = store().load("swaps", [])
        self.users = store().load("users", {})
        self.user = user
        self.me = self.key_for(user)

    def key_for(self, user):
        if user.get("spond_id"):
            return user["spond_id"]
        email = (user.get("email") or "").lower()
        for key, p in self.people.items():
            if email and p.get("email") == email:
                return key
        return None

    # names
    def name(self, key, me_suffix=" (jij)"):
        n = self.people.get(key, {}).get("first_name", "Onbekend")
        return n + me_suffix if key and key == self.me else n

    def start(self, ev):
        return parse_iso(ev["start"]).astimezone(self.tz)

    def local(self, ts):
        return parse_iso(ts).astimezone(self.tz)

    def is_future(self, ev):
        return self.start(ev) > self.now

    def swaps_for(self, event_id, *statuses):
        return [s for s in self.swaps if s["event_id"] == event_id and s["status"] in statuses]

    def escalated(self, swap):
        ev = self.by_id.get(swap["event_id"])
        return bool(ev) and fmt.days_until(self.start(ev), self.now) <= self.cfg["ESCALATE_DAYS"]

    def open_swaps(self):
        out = [s for s in self.swaps if s["status"] == "open"
               and s["event_id"] in self.by_id and self.is_future(self.by_id[s["event_id"]])]
        return sorted(out, key=lambda s: self.by_id[s["event_id"]]["start"])

    # rows
    def row(self, ev):
        start = self.start(ev)
        r = {"id": ev["id"], "title": ev["title"], "day": fmt.day_short(start),
             "date": start.day, "time": f"{start:%H:%M}", "start": start,
             "cls": "", "dot": "g", "status": "Ingeroosterd", "action": None}
        future = start > self.now
        open_ = self.swaps_for(ev["id"], "open")
        accepted = self.swaps_for(ev["id"], "accepted")
        if open_:
            s = open_[0]
            mine = s["requester_key"] == self.me
            note = f" · ‘{s['note']}’" if s.get("note") else ""
            r.update(cls="red", dot="r", hosts=self.name(s["requester_key"]) + note,
                     status="Ruil gevraagd" + (" · geëscaleerd" if self.escalated(s) else ""))
            if future and self.me:
                r["action"] = ("withdraw" if mine else "take", s["id"])
        elif accepted:
            s = accepted[0]
            r.update(cls="amber", dot="a", status="Overgenomen, Spond nog niet bijgewerkt",
                     hosts=f"{self.name(s['requester_key'])} → {self.name(s['taker_key'])}")
        else:
            r["hosts"] = ", ".join(self.name(h) for h in ev["hosts"]) or "Nog geen host"
            if future and self.me in ev["hosts"]:
                r["action"] = ("request", ev["id"])
        return r

    def mine(self, ev):
        """My training: I'm the host and haven't given it away, or I've taken it over."""
        given_away = any(s["requester_key"] == self.me for s in self.swaps_for(ev["id"], "accepted"))
        taken = any(s["taker_key"] == self.me for s in self.swaps_for(ev["id"], "accepted"))
        return (self.me in ev["hosts"] and not given_away) or taken

    def synced_text(self):
        ts = self.spond.get("synced_at")
        return fmt.moment(self.local(ts), self.now).replace("vandaag ", "vandaag om ") if ts else "nog nooit"


@bp.app_context_processor
def inject_nav():
    if not getattr(g, "user", None):
        return {}
    board = Board(g.user)
    return {"open_count": len(board.open_swaps()), "demo": current_app.config["DEMO"],
            "synced": board.synced_text(), "linked": board.me is not None}


# --- pages -----------------------------------------------------------------

@bp.route("/")
@login_required
def week():
    b = Board(g.user)
    try:
        y, w = request.args["w"].split("-W")
        monday = date.fromisocalendar(int(y), int(w), 1)
    except (KeyError, ValueError):
        today = b.now.date()
        monday = today - timedelta(days=today.weekday())
    sunday = monday + timedelta(days=6)
    rows = [b.row(e) for e in b.events if monday <= b.start(e).date() <= sunday]

    def wk(d):
        y, w, _ = d.isocalendar()
        return f"{y}-W{w:02d}"

    return render_template(
        "week.html", rows=rows, week_no=monday.isocalendar()[1], tab="week",
        week_range=fmt.week_range(monday, sunday),
        prev_w=wk(monday - timedelta(weeks=1)), next_w=wk(monday + timedelta(weeks=1)),
    )


@bp.route("/mijn")
@login_required
def mine():
    b = Board(g.user)
    months = []
    for e in b.events:
        if not (b.is_future(e) and b.me and b.mine(e)):
            continue
        r = b.row(e)
        label = fmt.month_name(r["start"]).capitalize()
        if not months or months[-1][0] != label:
            months.append((label, []))
        months[-1][1].append(r)
    return render_template("mine.html", months=months, tab="mine", weeks=b.cfg["SYNC_WEEKS"])


@bp.route("/open")
@login_required
def open_requests():
    b = Board(g.user)
    rows = []
    for s in b.open_swaps():
        ev = b.by_id[s["event_id"]]
        r = b.row(ev)
        days = fmt.days_until(r["start"], b.now)
        r.update(
            swap_id=s["id"], note=s.get("note"), escalated=b.escalated(s),
            mine=s["requester_key"] == b.me,
            by="jou" if s["requester_key"] == b.me else b.name(s["requester_key"], ""),
            when="vandaag" if days == 0 else "morgen" if days == 1 else f"over {days} dagen",
        )
        rows.append(r)
    escalated = sum(r["escalated"] for r in rows)
    return render_template("open.html", rows=rows, escalated=escalated, tab="open")


@bp.route("/overgenomen")
@login_required
def accepted():
    b = Board(g.user)
    waiting, done = [], []
    for s in b.swaps:
        ev = b.by_id.get(s["event_id"])
        if not ev or not b.me or s.get("taker_key") != b.me:
            continue
        r = b.row(ev)
        frm = b.name(s["requester_key"], "")
        if s["status"] == "accepted":
            hosts = ", ".join(b.name(h, "") for h in ev["hosts"]) or "niemand"
            r.update(hosts=f"Van {frm} · overgenomen {fmt.moment(b.local(s['accepted_at']), b.now)}",
                     status=f"Spond toont nog {hosts}", cls="amber", dot="a")
            waiting.append(r)
        elif s["status"] == "processed":
            r.update(hosts=f"Van {frm}", status="Jij bent host in Spond", cls="", dot="g")
            done.append(r)
    waiting.sort(key=lambda r: r["start"])
    done.sort(key=lambda r: r["start"], reverse=True)
    return render_template("accepted.html", waiting=waiting, done=done[:20], tab="accepted")


# --- actions ---------------------------------------------------------------

def _event_or_404(b, event_id):
    ev = b.by_id.get(event_id)
    if not ev:
        abort(404)
    return ev


@bp.route("/ruilen/<event_id>", methods=["GET", "POST"])
@login_required
def request_swap(event_id):
    b = Board(g.user)
    ev = _event_or_404(b, event_id)
    if not (b.me and b.me in ev["hosts"] and b.is_future(ev)):
        flash("Je kunt alleen je eigen, komende trainingen ruilen.", "error")
        return redirect(url_for("main.week"))
    if b.swaps_for(event_id, "open", "accepted"):
        flash("Voor deze training loopt al een ruil.", "error")
        return redirect(url_for("main.week"))
    if request.method == "POST":
        swap = {
            "id": secrets.token_hex(6), "event_id": event_id, "status": "open",
            "requester": g.user["username"], "requester_key": b.me,
            "note": request.form.get("note", "").strip()[:140] or None,
            "created_at": iso(utcnow()),
        }
        store().update("swaps", [], lambda swaps: swaps.append(swap))
        flash("Ruilverzoek geplaatst.", "ok")
        return redirect(url_for("main.open_requests"))
    return render_template("request.html", r=b.row(ev), tab="mine")


def _change_swap(swap_id, check, apply):
    """Atomically load swaps, validate with check(swap, board) -> error|None, apply."""
    b = Board(g.user)
    result = {}

    def fn(swaps):
        s = next((s for s in swaps if s["id"] == swap_id), None)
        if s is None:
            abort(404)
        ev = b.by_id.get(s["event_id"])
        result["error"] = check(s, b, ev)
        if not result["error"]:
            apply(s, b)

    store().update("swaps", [], fn)
    return result["error"]


@bp.post("/swaps/<swap_id>/overnemen")
@login_required
def take_swap(swap_id):
    def check(s, b, ev):
        if s["status"] != "open":
            return "Deze ruil is al overgenomen of ingetrokken."
        if not b.me:
            return "Je account is nog niet gekoppeld aan Spond. Vraag een beheerder."
        if s["requester_key"] == b.me:
            return "Je kunt je eigen verzoek niet overnemen."
        if not ev or not b.is_future(ev):
            return "Deze training is al geweest."

    def apply(s, b):
        s.update(status="accepted", taker=g.user["username"], taker_key=b.me, accepted_at=iso(utcnow()))

    error = _change_swap(swap_id, check, apply)
    flash(error or "Overgenomen. Een beheerder zet het in Spond.", "error" if error else "ok")
    return redirect(request.referrer or url_for("main.week"))


@bp.post("/swaps/<swap_id>/intrekken")
@login_required
def withdraw_swap(swap_id):
    def check(s, b, ev):
        if s["requester"] != g.user["username"]:
            return "Dit is niet jouw verzoek."
        if s["status"] != "open":
            return "Dit verzoek is al overgenomen of ingetrokken."

    def apply(s, b):
        s.update(status="cancelled", cancelled_at=iso(utcnow()), cancelled_by=g.user["username"])

    error = _change_swap(swap_id, check, apply)
    flash(error or "Verzoek ingetrokken.", "error" if error else "ok")
    return redirect(request.referrer or url_for("main.week"))


# --- admin -----------------------------------------------------------------

@bp.route("/beheer")
@admin_required
def admin():
    b = Board(g.user)
    todo, escalated = [], []
    for s in b.swaps:
        ev = b.by_id.get(s["event_id"])
        if not ev:
            continue
        r = b.row(ev)
        if s["status"] == "accepted":
            acc = b.local(s["accepted_at"])
            age = (b.now.date() - acc.date()).days
            r.update(swap_id=s["id"], spond_url=SPOND_EVENT_URL.format(id=ev["id"]),
                     change=f"{b.name(s['requester_key'], '')} → {b.name(s['taker_key'], '')}",
                     late=age > b.cfg["LATE_AFTER_DAYS"],
                     meta=f"overgenomen {fmt.moment(acc, b.now)}" + (f", {age} dagen geleden" if age > 1 else ""))
            todo.append(r)
        elif s["status"] == "open" and b.is_future(ev) and b.escalated(s):
            days = fmt.days_until(r["start"], b.now)
            note = f" · ‘{s['note']}’" if s.get("note") else ""
            r.update(swap_id=s["id"], change=b.name(s["requester_key"], "") + note,
                     meta=f"nog {days} dagen")
            escalated.append(r)
    todo.sort(key=lambda r: r["start"])
    escalated.sort(key=lambda r: r["start"])
    spond = b.spond
    return render_template("admin.html", todo=todo, escalated=escalated,
                           last_error=spond.get("last_error"),
                           next_sync=b.local(spond["last_attempt"]) + timedelta(minutes=b.cfg["SYNC_MINUTES"])
                           if spond.get("last_attempt") else None)


@bp.post("/swaps/<swap_id>/annuleren")
@admin_required
def cancel_swap(swap_id):
    def check(s, b, ev):
        if s["status"] != "open":
            return "Alleen open verzoeken kunnen geannuleerd worden."

    def apply(s, b):
        s.update(status="cancelled", cancelled_at=iso(utcnow()), cancelled_by=g.user["username"])

    error = _change_swap(swap_id, check, apply)
    flash(error or "Verzoek geannuleerd.", "error" if error else "ok")
    return redirect(url_for("main.admin"))


@bp.post("/beheer/sync")
@admin_required
def admin_sync():
    try:
        n = run_sync(current_app.config, store())
        flash(f"Gesynchroniseerd met Spond. {n} ruil(en) verwerkt.", "ok")
    except Exception as exc:
        flash(f"Synchronisatie mislukt: {exc}", "error")
    return redirect(url_for("main.admin"))


@bp.post("/beheer/demo-verwerk/<swap_id>")
@admin_required
def demo_process(swap_id):
    """Demo mode only: pretend an admin changed the host in Spond."""
    if not current_app.config["DEMO"]:
        abort(404)
    s = next((s for s in store().load("swaps", []) if s["id"] == swap_id), None)
    if not s:
        abort(404)
    b = Board(g.user)
    ev = _event_or_404(b, s["event_id"])
    hosts = [s["taker_key"] if h == s["requester_key"] else h for h in ev["hosts"]]
    store().update("demo_hosts", {}, lambda o: o.__setitem__(ev["id"], hosts))
    return admin_sync()
