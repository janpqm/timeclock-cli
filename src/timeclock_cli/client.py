"""HTTP client for timeclock365.com.

The actual endpoints and payload shapes are placeholders until we capture the
real flow from the live web UI (see ``NOTES.md``). The structure below is
intentionally close to what a typical session-cookie web app uses so the
swap-in is mechanical once we have the curl commands.
"""

from __future__ import annotations

import json
import logging
import pickle
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import List, Optional

import requests

from .config import Config, session_path


log = logging.getLogger(__name__)


class TimeclockError(RuntimeError):
    """Any failure raised by the client."""


class AuthenticationError(TimeclockError):
    """Login failed or session is no longer valid."""


@dataclass
class Entry:
    """A single time-clock entry."""

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


class TimeclockClient:
    """Thin wrapper around ``requests.Session`` for timeclock365.

    HTTP details are kept in private ``_…`` methods so we can swap in the real
    endpoints from a single place once the flow is captured.
    """

    def __init__(self, config: Config, *, session_file: Optional[Path] = None) -> None:
        self.config = config
        self.session_file = session_file or session_path()
        self.http = requests.Session()
        self.http.headers.update({
            "User-Agent": "timeclock-cli/0.1 (+https://github.com/jan-henrik-preuss/timeclock-cli)",
            "Accept": "application/json, text/html;q=0.8",
        })
        self._loaded_session = self._load_session()

    # ------------------------------------------------------------------ public

    def login(self, force: bool = False) -> None:
        """Authenticate. Reuses persisted session cookies when possible."""
        if not force and self._loaded_session:
            log.debug("Using cached session from %s", self.session_file)
            return

        if not self.config.username or not self.config.password:
            raise AuthenticationError(
                f"No credentials configured. Edit {self.config.path} and set [auth] username/password."
            )

        log.info("Logging in to %s as %s", self.config.base_url, self.config.username)
        self._do_login()
        self._save_session()

    def punch_in(self, location: Optional[str] = None) -> None:
        location = location or self.config.default_location
        log.info("Punching in%s", f" at {location}" if location else "")
        self._ensure_session()
        self._do_punch_in(location)

    def punch_out(self) -> None:
        log.info("Punching out")
        self._ensure_session()
        self._do_punch_out()

    def list_entries(self, days: int = 5) -> List[Entry]:
        end = date.today()
        start = end - timedelta(days=days - 1)
        log.info("Fetching entries from %s to %s", start, end)
        self._ensure_session()
        return self._do_list_entries(start, end)

    def status(self) -> Status:
        log.debug("Fetching current status")
        self._ensure_session()
        return self._do_status()

    # ---------------------------------------------------------------- internal

    def _ensure_session(self) -> None:
        """Login if we don't have a session yet. Callers wrap this before requests."""
        if not self._loaded_session:
            self.login()

    def _load_session(self) -> bool:
        if not self.session_file.exists():
            return False
        try:
            with self.session_file.open("rb") as fh:
                cookies = pickle.load(fh)
            self.http.cookies.update(cookies)
            return True
        except Exception as exc:  # noqa: BLE001 — best-effort cache load
            log.debug("Could not load cached session: %s", exc)
            return False

    def _save_session(self) -> None:
        self.session_file.parent.mkdir(parents=True, exist_ok=True)
        with self.session_file.open("wb") as fh:
            pickle.dump(self.http.cookies, fh)
        try:
            self.session_file.chmod(0o600)
        except OSError:
            pass
        log.debug("Saved session cookies to %s", self.session_file)

    # -------- placeholders to replace with the real endpoints once captured ---

    def _do_login(self) -> None:
        # TODO(flow-capture): replace with the real POST observed in the browser.
        # Expected to look something like:
        #
        #   r = self.http.post(
        #       f"{self.config.base_url}/en/login",
        #       data={"username": ..., "password": ..., "_token": ...},
        #       allow_redirects=True,
        #   )
        #   if "Set-Cookie" not in r.headers and r.url.endswith("/login"):
        #       raise AuthenticationError("Login rejected")
        raise NotImplementedError(
            "Login flow not yet captured — see NOTES.md. Run the CLI through DevTools "
            "to record the real request, then paste it into the issue."
        )

    def _do_punch_in(self, location: Optional[str]) -> None:
        raise NotImplementedError("Punch-in flow not yet captured — see NOTES.md")

    def _do_punch_out(self) -> None:
        raise NotImplementedError("Punch-out flow not yet captured — see NOTES.md")

    def _do_list_entries(self, start: date, end: date) -> List[Entry]:
        raise NotImplementedError("List flow not yet captured — see NOTES.md")

    def _do_status(self) -> Status:
        raise NotImplementedError("Status flow not yet captured — see NOTES.md")

    # ---- helper kept here so the eventual real implementations can call it ---

    def _request(self, method: str, path: str, **kw) -> requests.Response:
        url = f"{self.config.base_url}{path}"
        log.debug("%s %s", method, url)
        resp = self.http.request(method, url, **kw)
        log.debug("→ %s %s (%d bytes)", resp.status_code, resp.reason, len(resp.content))
        if resp.status_code in (401, 403):
            raise AuthenticationError(f"Server rejected request: {resp.status_code} {resp.reason}")
        resp.raise_for_status()
        return resp

    @staticmethod
    def _safe_json(resp: requests.Response) -> dict:
        try:
            return resp.json()
        except json.JSONDecodeError as exc:
            raise TimeclockError(f"Expected JSON but got: {resp.text[:200]!r}") from exc
