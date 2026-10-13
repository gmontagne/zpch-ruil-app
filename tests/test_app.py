from datetime import datetime, timedelta, timezone

import pytest
from werkzeug.security import generate_password_hash

from ruil import create_app
from ruil.spond import SpondClient, SpondError
from ruil.sync import normalize, reconcile, run_sync


@pytest.fixture
def app(tmp_path):
    app = create_app({"DATA_DIR": str(tmp_path), "DEMO": True, "TESTING": True})
    store = app.extensions["store"]
    store.save("users", {
        n: {"name": n.capitalize(), "email": f"{n}@example.org", "spond_id": None,
            "admin": n == "pieter", "password_hash": generate_password_hash("pw")}
        for n in ("pieter", "sanne", "joost", "anna", "lies", "ruud")
    })
    run_sync(app.config, store)
    return app


def login(client, username):
    return client.post("/login", data={"username": username, "password": "pw"})


def future_event_hosted_by(app, key):
    now = datetime.now(timezone.utc) + timedelta(hours=1)
    for e in app.extensions["store"].load("spond", {})["events"]:
        if key in e["hosts"] and datetime.fromisoformat(e["start"].replace("Z", "+00:00")) > now:
            return e
    raise AssertionError("no event")


def test_login_required_and_bad_password(app):
    c = app.test_client()
    assert c.get("/").status_code == 302
    r = c.post("/login", data={"username": "pieter", "password": "nope"})
    assert "verkeerd wachtwoord" in r.get_data(as_text=True)


def test_full_swap_flow(app):
    store = app.extensions["store"]
    ev = future_event_hosted_by(app, "p-sanne")

    sanne = app.test_client()
    login(sanne, "sanne")
    assert sanne.get(f"/ruilen/{ev['id']}").status_code == 200
    sanne.post(f"/ruilen/{ev['id']}", data={"note": "ouderavond"})
    [swap] = store.load("swaps", [])
    assert swap["status"] == "open" and swap["requester_key"] == "p-sanne"
    assert "ouderavond" in sanne.get("/open").get_data(as_text=True)

    # Sanne can't take her own request; Anna can.
    sanne.post(f"/swaps/{swap['id']}/overnemen")
    assert store.load("swaps", [])[0]["status"] == "open"
    anna = app.test_client()
    login(anna, "anna")
    anna.post(f"/swaps/{swap['id']}/overnemen")
    swap = store.load("swaps", [])[0]
    assert swap["status"] == "accepted" and swap["taker_key"] == "p-anna"
    assert "Wacht op Spond" in anna.get("/overgenomen").get_data(as_text=True)

    # Admin sees it, "changes Spond" (demo), sync marks it processed.
    admin = app.test_client()
    login(admin, "pieter")
    assert "Sanne → Anna" in admin.get("/beheer").get_data(as_text=True)
    assert anna.get("/beheer").status_code == 403
    admin.post(f"/beheer/demo-verwerk/{swap['id']}")
    assert store.load("swaps", [])[0]["status"] == "processed"
    assert "Jij bent host in Spond" in anna.get("/overgenomen").get_data(as_text=True)


def test_withdraw_only_by_requester(app):
    store = app.extensions["store"]
    ev = future_event_hosted_by(app, "p-joost")
    joost, lies = app.test_client(), app.test_client()
    login(joost, "joost")
    login(lies, "lies")
    joost.post(f"/ruilen/{ev['id']}")
    sid = store.load("swaps", [])[0]["id"]
    lies.post(f"/swaps/{sid}/intrekken")
    assert store.load("swaps", [])[0]["status"] == "open"
    joost.post(f"/swaps/{sid}/intrekken")
    assert store.load("swaps", [])[0]["status"] == "cancelled"


def test_cannot_request_someone_elses_training(app):
    ev = future_event_hosted_by(app, "p-ruud")
    if "p-lies" in ev["hosts"]:
        pytest.skip("lies co-hosts")
    lies = app.test_client()
    login(lies, "lies")
    lies.post(f"/ruilen/{ev['id']}")
    assert app.extensions["store"].load("swaps", []) == []


def test_pages_render(app):
    c = app.test_client()
    login(c, "pieter")
    for path in ("/", "/?w=2026-W43", "/mijn", "/open", "/overgenomen", "/beheer"):
        assert c.get(path).status_code == 200, path


# --- Spond parsing -----------------------------------------------------------

GROUP = {"id": "g1", "members": [
    {"id": "m1", "firstName": "Anna", "lastName": "de Vries", "email": "Anna@X.nl", "profile": {"id": "p1"}},
    {"id": "m2", "firstName": "Ruud", "lastName": "", "email": "ruud@x.nl"},  # no Spond profile
]}
EVENTS = [
    {"id": "e2", "heading": "Selectie", "startTimestamp": "2026-10-21T17:00:00Z", "owners": [{"id": "m2"}]},
    {"id": "e1", "heading": "Jeugd", "startTimestamp": "2026-10-19T16:30:00Z", "owners": [{"id": "p1", "response": "accepted"}]},
    {"id": "e3", "heading": "Weg", "startTimestamp": "2026-10-22T16:30:00Z", "cancelled": True, "owners": []},
]


def test_normalize():
    data = normalize(GROUP, EVENTS)
    assert [e["id"] for e in data["events"]] == ["e1", "e2"]
    assert data["events"][0]["hosts"] == ["p1"]
    assert data["people"]["p1"]["email"] == "anna@x.nl"
    assert data["people"]["m2"]["first_name"] == "Ruud"


def test_reconcile():
    events = normalize(GROUP, EVENTS)["events"]
    swaps = [
        {"event_id": "e2", "status": "accepted", "requester_key": "p1", "taker_key": "m2"},
        {"event_id": "e1", "status": "accepted", "requester_key": "p1", "taker_key": "m2"},
    ]
    assert reconcile(swaps, events, datetime.now(timezone.utc)) == 1
    assert [s["status"] for s in swaps] == ["processed", "accepted"]


class FakeResp:
    def __init__(self, status, body):
        self.status_code, self._body, self.ok, self.text = status, body, status < 400, str(body)

    def json(self):
        return self._body


class FakeHttp:
    def __init__(self, login_body):
        self.login_body, self.calls = login_body, []

    def post(self, url, json, timeout):
        self.calls.append(("POST", url, json))
        return FakeResp(200, self.login_body)

    def get(self, url, params, headers, timeout):
        self.calls.append(("GET", url, params, headers))
        return FakeResp(200, [GROUP] if url.endswith("groups/") else EVENTS)


def test_spond_client_login_and_events():
    http = FakeHttp({"accessToken": {"token": "tok"}})
    c = SpondClient("a@b.c", "pw", session=http)
    assert c.get_group("g1")["id"] == "g1"
    now = datetime(2026, 10, 13, tzinfo=timezone.utc)
    c.get_events("g1", now, now + timedelta(weeks=8), subgroup_id="sg")
    assert http.calls[0][1].endswith("auth2/login")
    _, url, params, headers = http.calls[-1]
    assert url.endswith("sponds/") and params["groupId"] == "g1" and params["subGroupId"] == "sg"
    assert params["minStartTimestamp"] == "2026-10-13T00:00:00.000Z"
    assert headers["Authorization"] == "Bearer tok"


def test_spond_client_bad_login():
    with pytest.raises(SpondError):
        SpondClient("a@b.c", "pw", session=FakeHttp({"error": "nope"})).login()
