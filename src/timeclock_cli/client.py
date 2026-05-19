"""HTTP client for timeclock365.com.

Implements the flow captured in ``NOTES.md``:

1. Two-step login via ``/api/auth/login`` + ``/api/auth/check``.
2. Subsequent calls go to ``/graphql/`` with an ``access-token`` header.

The access token is short-lived (~10 minutes), so we cache it on disk and
silently re-login when it expires.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional

import requests

from .config import Config, session_path


log = logging.getLogger(__name__)


# The web UI uses a fixed client_id when calling /api/auth/login. It's the same
# value across every captured session, so it appears to be a per-app constant
# rather than a per-user / per-browser one.
DEFAULT_CLIENT_ID = (
    "ZWUyYzllNGUzYjlhZDAwNzBiYTgwN2ZjYjU2YWZiNDRhZDhiYTFkNmM5NTkzNzJlNDc1NzRkM2Y5OGFiODc0Nw"
)

LOCATION_TYPES = ("OFFICE", "HOME", "ABROAD", "FIELD")
_LOCATION_ALIASES = {
    "WORK": "OFFICE",
    "HOMEOFFICE": "HOME",
    "HOME-OFFICE": "HOME",
    "REMOTE": "HOME",
    "TRAVEL": "ABROAD",
    "ONSITE": "FIELD",
    "CLIENT": "FIELD",
}


class TimeclockError(RuntimeError):
    """Any failure raised by the client."""


class AuthenticationError(TimeclockError):
    """Login failed or session is no longer valid."""


# ---------------------------------------------------------------- domain models


@dataclass
class Entry:
    """A single time-clock session/entry."""

    started_at: datetime
    ended_at: Optional[datetime]
    location: Optional[str]
    note: Optional[str] = None

    @property
    def is_open(self) -> bool:
        return self.ended_at is None

    @property
    def duration(self) -> timedelta:
        end = self.ended_at or datetime.now()
        return end - self.started_at

    def format_line(self) -> str:
        end_str = self.ended_at.strftime("%H:%M") if self.ended_at else "  …  "
        date_str = self.started_at.strftime("%a %Y-%m-%d")
        hours = self.duration.total_seconds() / 3600
        loc = f" @ {self.location}" if self.location else ""
        return f"{date_str}  {self.started_at.strftime('%H:%M')}–{end_str}  {hours:>5.2f}h{loc}"


@dataclass
class Status:
    clocked_in: bool
    since: Optional[datetime] = None
    location: Optional[str] = None
    today_total: Optional[timedelta] = None
    available_locations: Optional[List[str]] = None
    default_location: Optional[str] = None


@dataclass
class _Session:
    """On-disk cache of the auth response from ``/api/auth/check``."""

    access_token: str
    expires_at: int           # unix timestamp
    refresh_token: str
    refresh_expires_at: int
    user_id: int

    def is_expired(self, slack_seconds: int = 30) -> bool:
        return time.time() >= (self.expires_at - slack_seconds)


# ---------------------------------------------------------------- public helper


def normalize_location(loc: Optional[str]) -> Optional[str]:
    """Map a user-friendly location string to a server-side enum value.

    Returns None when called with None. Raises ``TimeclockError`` on unknown labels.
    """
    if loc is None:
        return None
    u = loc.upper().strip().replace(" ", "")
    if u in LOCATION_TYPES:
        return u
    if u in _LOCATION_ALIASES:
        return _LOCATION_ALIASES[u]
    raise TimeclockError(
        f"Unknown location {loc!r}. Allowed values: {', '.join(LOCATION_TYPES)}"
    )


# ---------------------------------------------------------------- the client


# GraphQL operations we use. Kept as module-level constants so they're easy to
# diff against future captures.

_GQL_STATUS = """
query DashboardWebPunchQuery {
    webPunch {
        ip
        locationTypes
        locationType
        session { id startDate endDate timezone locationType }
        lunchSession { id startDate endDate }
    }
}
"""

_GQL_MAKE_PUNCH = """
mutation makePunchDashboard($punch: InputWebPunch!) {
    makePunchByDashboard(punch: $punch) {
        id startDate endDate timezone locationType
    }
}
"""


class TimeclockClient:
    """Thin wrapper around ``requests.Session`` for timeclock365."""

    def __init__(self, config: Config, *, session_file: Optional[Path] = None) -> None:
        self.config = config
        self.session_file = session_file or session_path()
        self.http = requests.Session()
        self.http.headers.update({
            "User-Agent": "timeclock-cli/0.1 (+https://github.com/jan-henrik-preuss/timeclock-cli)",
            "Accept": "application/json, text/plain, */*",
            "Origin": config.base_url,
            "Referer": f"{config.base_url}/en/login",
        })
        self._session: Optional[_Session] = self._load_session()

    # ----- public API -----

    def login(self, force: bool = False) -> None:
        """Authenticate. Reuses a cached token if it's still valid."""
        if not force and self._session and not self._session.is_expired():
            log.debug("Reusing cached access-token (expires in %ds)",
                      int(self._session.expires_at - time.time()))
            return

        if not self.config.username or not self.config.password:
            raise AuthenticationError(
                f"No credentials configured. Edit {self.config.path} and set [auth] username/password."
            )

        log.info("Logging in to %s as %s", self.config.base_url, self.config.username)

        # --- step 1: /api/auth/login -----------------------------------------
        login_body = {
            "username": self.config.username,
            "client_id": self.config.client_id or DEFAULT_CLIENT_ID,
        }
        r = self.http.post(
            f"{self.config.base_url}/api/auth/login",
            json=login_body,
            headers={"Content-Type": "application/json"},
        )
        log.debug("/api/auth/login → %d", r.status_code)
        if r.status_code != 200:
            raise AuthenticationError(
                f"/api/auth/login returned {r.status_code}: {r.text[:200]}"
            )
        data = _safe_json(r)
        if data.get("status") != "additional_authentication_steps_required":
            raise AuthenticationError(
                f"/api/auth/login returned unexpected status {data.get('status')!r}: {data}"
            )
        auth_token = data.get("auth_token")
        if not auth_token:
            raise AuthenticationError("/api/auth/login response is missing auth_token")
        log.debug("Got auth_token (step 1 complete)")

        # --- step 2: /api/auth/check -----------------------------------------
        r = self.http.post(
            f"{self.config.base_url}/api/auth/check",
            json={"code": self.config.password},
            headers={
                "Content-Type": "application/json",
                "Auth-Token": auth_token,
            },
        )
        log.debug("/api/auth/check → %d", r.status_code)
        if r.status_code != 200:
            raise AuthenticationError(
                f"/api/auth/check returned {r.status_code}: {r.text[:200]}"
            )
        data = _safe_json(r)
        if data.get("status") != "authenticated":
            raise AuthenticationError(
                f"/api/auth/check returned unexpected status {data.get('status')!r}: "
                f"{data.get('message', data)}"
            )
        try:
            self._session = _Session(
                access_token=data["access_token"],
                expires_at=int(data["expires_at"]),
                refresh_token=data["refresh_token"],
                refresh_expires_at=int(data["refresh_expires_at"]),
                user_id=int(data["user_id"]),
            )
        except KeyError as exc:
            raise AuthenticationError(
                f"/api/auth/check response missing field {exc}: {data}"
            ) from exc

        ttl = int(self._session.expires_at - time.time())
        log.info("Authenticated as user_id=%s (token valid for ~%d s)", self._session.user_id, ttl)
        self._save_session()

    def punch_in(self, location: Optional[str] = None) -> Entry:
        loc = normalize_location(location or self.config.default_location)
        # If no location specified anywhere, fall back to whatever the server says is the user's default.
        if loc is None:
            self.login()
            status = self.status()
            loc = status.default_location or "OFFICE"
            log.info("No location given; falling back to server default %s", loc)
        log.info("Punching in at %s", loc)
        return self._make_punch(loc, "PUNCH_IN")

    def punch_out(self) -> Entry:
        # Punch-out still needs a locationType. Use the current open session's
        # location if we have one, otherwise the server default, otherwise OFFICE.
        self.login()
        status = self.status()
        loc = status.location or status.default_location or "OFFICE"
        log.info("Punching out (location=%s)", loc)
        return self._make_punch(loc, "PUNCH_OUT")

    def status(self) -> Status:
        log.debug("Fetching current status")
        self.login()
        data = self._graphql(_GQL_STATUS, operation_name="DashboardWebPunchQuery")
        wp = data.get("webPunch") or {}
        session = wp.get("session")
        clocked_in = bool(session) and not session.get("endDate")

        since = _parse_dt(session.get("startDate")) if session else None
        location = session.get("locationType") if session else None
        today_total = since and (datetime.now() - since) if clocked_in else None

        return Status(
            clocked_in=clocked_in,
            since=since,
            location=location,
            today_total=today_total,
            available_locations=wp.get("locationTypes") or list(LOCATION_TYPES),
            default_location=wp.get("locationType"),
        )

    def list_entries(self, days: int = 5) -> List[Entry]:
        """List recent entries.

        Not yet implemented — we don't have a captured HAR for the timecard /
        history view. To enable this, capture the request the timecard page
        makes on load and share the operation name + query, then add it here.
        """
        raise NotImplementedError(
            "List flow not yet captured — open the Timecard page in the web UI, "
            "export a HAR while it loads, and add the query to NOTES.md / client.py."
        )

    # ----- internals -----

    def _make_punch(self, location: str, punch_type: str) -> Entry:
        self.login()
        data = self._graphql(
            _GQL_MAKE_PUNCH,
            operation_name="makePunchDashboard",
            variables={"punch": {"locationType": location, "punchType": punch_type}},
        )
        node = data.get("makePunchByDashboard")
        if not node:
            raise TimeclockError(f"Punch response missing makePunchByDashboard: {data}")
        return Entry(
            started_at=_parse_dt(node["startDate"]),
            ended_at=_parse_dt(node.get("endDate")),
            location=node.get("locationType"),
        )

    def _graphql(
        self,
        query: str,
        *,
        operation_name: Optional[str] = None,
        variables: Optional[dict] = None,
    ) -> dict:
        assert self._session, "must be logged in"
        body: dict = {"query": query}
        if operation_name:
            body["operationName"] = operation_name
        if variables is not None:
            body["variables"] = variables

        headers = {
            "Content-Type": "application/json",
            "access-token": self._session.access_token,
            "App-Accept-Language": "en",
            "BC-Mode": "swap",
            "Use-Http-Code": "1",
            "Referer": f"{self.config.base_url}/en/dashboard",
        }
        log.debug("→ GraphQL %s vars=%s", operation_name or "?", variables)
        r = self.http.post(f"{self.config.base_url}/graphql/", json=body, headers=headers)
        log.debug("← %d %s (%d bytes)", r.status_code, r.reason, len(r.content))

        if r.status_code in (401, 403):
            # Token might have expired between our check and the call. Try once
            # more with a fresh login.
            log.info("Got %d on GraphQL — re-authenticating and retrying once", r.status_code)
            self.login(force=True)
            headers["access-token"] = self._session.access_token
            r = self.http.post(f"{self.config.base_url}/graphql/", json=body, headers=headers)
            log.debug("← retry %d %s", r.status_code, r.reason)

        if r.status_code >= 400:
            raise TimeclockError(
                f"GraphQL {operation_name or 'request'} failed: {r.status_code} {r.reason} — {r.text[:300]}"
            )

        payload = _safe_json(r)
        if payload.get("errors"):
            raise TimeclockError(
                f"GraphQL {operation_name or 'request'} returned errors: {payload['errors']}"
            )
        # The server wraps responses as { code: 200, data: {...} } when Use-Http-Code is 1.
        return payload.get("data") or payload

    # ----- session persistence -----

    def _load_session(self) -> Optional[_Session]:
        if not self.session_file.exists():
            return None
        try:
            with self.session_file.open("r", encoding="utf-8") as fh:
                raw = json.load(fh)
            return _Session(**raw)
        except Exception as exc:  # noqa: BLE001 — best-effort cache load
            log.debug("Could not load cached session: %s", exc)
            return None

    def _save_session(self) -> None:
        if not self._session:
            return
        self.session_file.parent.mkdir(parents=True, exist_ok=True)
        with self.session_file.open("w", encoding="utf-8") as fh:
            json.dump(asdict(self._session), fh)
        try:
            self.session_file.chmod(0o600)
        except OSError:
            pass
        log.debug("Saved session to %s", self.session_file)


# ---------------------------------------------------------------- module helpers


def _safe_json(resp: requests.Response) -> dict:
    try:
        return resp.json()
    except json.JSONDecodeError as exc:
        raise TimeclockError(f"Expected JSON but got: {resp.text[:200]!r}") from exc


def _parse_dt(value: Optional[str]) -> Optional[datetime]:
    """Parse the ``"2026-05-19 05:38:39"`` format the server returns."""
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        # Fallback: try ISO-8601 just in case the server ever returns it.
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            log.debug("Unparseable datetime from server: %r", value)
            return None
