"""Minimal read-only client for Spond's (unofficial) REST API.

Endpoints follow the community `spond` library (github.com/Olen/Spond):
  POST core/v1/auth2/login  -> {"accessToken": {"token": ...}}
  GET  core/v1/groups/      -> groups incl. members and subGroups
  GET  core/v1/sponds/      -> events; event["owners"] are the event hosts
"""

from datetime import datetime, timezone

import requests

API_URL = "https://api.spond.com/core/v1/"
SPOND_EVENT_URL = "https://spond.com/client/sponds/{id}"
TIMEOUT = 20


class SpondError(Exception):
    pass


def _ts(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


class SpondClient:
    def __init__(self, email, password, session=None, api_url=API_URL):
        self.email = email
        self.password = password
        self.api_url = api_url
        self.http = session or requests.Session()
        self.token = None

    def login(self):
        r = self.http.post(
            self.api_url + "auth2/login",
            json={"email": self.email, "password": self.password},
            timeout=TIMEOUT,
        )
        try:
            body = r.json()
        except ValueError:
            body = {}
        token = (body.get("accessToken") or {}).get("token") if isinstance(body, dict) else None
        if not token:
            raise SpondError(f"Spond login failed (HTTP {r.status_code})")
        self.token = token

    def _get(self, path, params=None):
        if not self.token:
            self.login()
        r = self.http.get(
            self.api_url + path,
            params=params,
            headers={"Authorization": f"Bearer {self.token}"},
            timeout=TIMEOUT,
        )
        if not r.ok:
            raise SpondError(f"GET {path} failed (HTTP {r.status_code}): {r.text[:200]}")
        return r.json()

    def get_group(self, group_id):
        for group in self._get("groups/"):
            if group.get("id") == group_id:
                return group
        raise SpondError(f"Group {group_id} not found for this Spond account")

    def get_events(self, group_id, min_start, max_start, subgroup_id=None, max_events=300):
        params = {
            "groupId": group_id,
            "minStartTimestamp": _ts(min_start),
            "maxStartTimestamp": _ts(max_start),
            "max": str(max_events),
            "scheduled": "true",
            "order": "asc",
        }
        if subgroup_id:
            params["subGroupId"] = subgroup_id
        return self._get("sponds/", params)
