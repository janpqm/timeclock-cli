"""HTTP client for timeclock365.com.

Implements the flow captured in ``NOTES.md``:

1. Two-step login via ``/api/auth/login`` + ``/api/auth/check``.
2. Subsequent calls go to ``/graphql/`` with an ``access-token`` header.

The access token is short-lived (~10 minutes), so we cache it on disk and
silently re-login when it expires.

Timezones
---------
The server is inconsistent about what timezone it returns datetimes in:

* ``DashboardWebPunchQuery.session.startDate`` / ``endDate`` and
  ``makePunchByDashboard.startDate`` / ``endDate`` are **UTC** strings even
  though the response also carries a ``timezone`` field.
* ``DetailedAttendanceReport.records[*].allSessions[*].startDate`` and
  the lunchSession ``startDateInUserTimezone`` / ``endDateInUserTimezone``
  fields are **already in the user's timezone**.

We convert everything to timezone-aware ``datetime`` objects at parse time
and let the CLI format them in the user's local timezone for display.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional, Tuple

import requests

try:  # zoneinfo is stdlib on Python 3.9+. Windows may need the tzdata wheel.
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore[assignment]

from .config import Config, session_path


log = logging.getLogger(__name__)


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


class ConflictError(TimeclockError):
    """The requested punch would conflict with the current state.

    Raised when the user tries to punch in while already in (or punch out
    while already out) without ``--force`` / ``force_overwrite=true``.
    """


# ---------------------------------------------------------------- domain models


@dataclass
class Entry:
    """A single time-clock session/entry.

    ``absence_name`` is set for absence-style records (Vacation, Sick leave,
    Public Holiday, …) instead of ``location``. ``is_lunch=True`` marks lunch
    break entries (no location either, just start/end times).

    ``session_id`` and ``timezone_name`` are populated by ``_make_punch`` so
    that callers can pass them straight to ``update_session`` without a second
    round-trip.
    """

    started_at: datetime
    ended_at: Optional[datetime]
    location: Optional[str]
    note: Optional[str] = None
    absence_name: Optional[str] = None
    is_lunch: bool = False
    session_id: Optional[int] = None
    timezone_name: Optional[str] = None

    @property
    def is_open(self) -> bool:
        return self.ended_at is None and self.absence_name is None and not self.is_lunch

    @property
    def is_absence(self) -> bool:
        return self.absence_name is not None

    @property
    def duration(self) -> timedelta:
        if self.ended_at is not None:
            return self.ended_at - self.started_at
        # Open entry: compute against "now", matching the tz-awareness of start.
        if self.started_at.tzinfo is None:
            return datetime.now() - self.started_at
        return _now_aware(self.started_at.tzinfo) - self.started_at

    def format_line(self) -> str:
        date_str = self.started_at.strftime("%a %Y-%m-%d")
        hours = self.duration.total_seconds() / 3600
        if self.is_absence:
            return f"{date_str}  {' ' * 12}  {hours:>5.2f}h  ↳ {self.absence_name}"
        end_str = self.ended_at.strftime("%H:%M") if self.ended_at else "  …  "
        if self.is_lunch:
            return f"{date_str}  {self.started_at.strftime('%H:%M')}–{end_str}  {hours:>5.2f}h  ↳ lunch break"
        loc = f" @ {self.location.lower():<12}" if self.location else " " * 9
        return f"{date_str}  {self.started_at.strftime('%H:%M')}–{end_str}  {hours:>5.2f}h{loc}"


@dataclass
class LunchBreak:
    """An in-progress or completed lunch break inside the current session."""

    started_at: datetime
    ended_at: Optional[datetime]

    @property
    def is_open(self) -> bool:
        return self.ended_at is None

    @property
    def duration(self) -> timedelta:
        if self.ended_at is not None:
            return self.ended_at - self.started_at
        if self.started_at.tzinfo is None:
            return datetime.now() - self.started_at
        return _now_aware(self.started_at.tzinfo) - self.started_at


@dataclass
class Status:
    clocked_in: bool
    since: Optional[datetime] = None
    location: Optional[str] = None
    today_total: Optional[timedelta] = None
    available_locations: Optional[List[str]] = None
    default_location: Optional[str] = None
    timezone_name: Optional[str] = None
    lunch: Optional[LunchBreak] = None
    session_id: Optional[int] = None


@dataclass
class PeriodSummary:
    """Aggregate totals for the current payroll period."""

    start: datetime
    end: datetime
    total_hours: float
    standard_hours: float
    credit_hours: float
    lack_of_hours: float
    absence_hours: float
    days_worked: int
    timezone: str


@dataclass
class _Session:
    """On-disk cache of the auth response from ``/api/auth/check``.

    ``company_id`` is fetched lazily on first list/report call and persisted
    here so subsequent runs don't repeat the lookup.
    """

    access_token: str
    expires_at: int           # unix timestamp
    refresh_token: str
    refresh_expires_at: int
    user_id: int
    company_id: Optional[int] = None

    def is_expired(self, slack_seconds: int = 30) -> bool:
        return time.time() >= (self.expires_at - slack_seconds)


# ---------------------------------------------------------------- public helpers


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


_CLOCK_RE = re.compile(r"^(\d{1,2}):(\d{2})$")


def looks_like_clock_time(s: str) -> bool:
    """True if ``s`` matches the ``HH:MM`` (or ``H:MM``) clock-time pattern."""
    return bool(_CLOCK_RE.match(s.strip())) if s else False


def parse_clock_time(s: str, *, allow_future: bool = False) -> datetime:
    """Parse an ``HH:MM`` string as **today** in the system's local timezone.

    Returns a timezone-aware datetime. Raises ``TimeclockError`` if the time
    is invalid or, when ``allow_future`` is False, lies in the future.
    """
    m = _CLOCK_RE.match(s.strip() if s else "")
    if not m:
        raise TimeclockError(f"Expected time as HH:MM (e.g. 7:30 or 18:00), got {s!r}")
    h, mn = int(m.group(1)), int(m.group(2))
    if not (0 <= h <= 23 and 0 <= mn <= 59):
        raise TimeclockError(f"Invalid clock time {s!r} — must be 0:00 … 23:59")
    now_local = datetime.now().astimezone()
    candidate = now_local.replace(hour=h, minute=mn, second=0, microsecond=0)
    if not allow_future and candidate > now_local:
        raise TimeclockError(
            f"Time {h:02d}:{mn:02d} is in the future "
            f"(now is {now_local.strftime('%H:%M')}). Refusing — pass --force to override."
        )
    return candidate


def parse_clock_time_on_date(s: str, d) -> datetime:
    """Parse ``HH:MM`` on a specific ``datetime.date``, returning a tz-aware datetime."""
    from datetime import date as _date  # local import avoids shadowing the module-level name
    m = _CLOCK_RE.match(s.strip() if s else "")
    if not m:
        raise TimeclockError(f"Expected HH:MM (e.g. 7:30 or 18:00), got {s!r}")
    h, mn = int(m.group(1)), int(m.group(2))
    if not (0 <= h <= 23 and 0 <= mn <= 59):
        raise TimeclockError(f"Invalid clock time {s!r} — must be 0:00 … 23:59")
    return datetime.now().astimezone().replace(
        year=d.year, month=d.month, day=d.day,
        hour=h, minute=mn, second=0, microsecond=0,
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

_GQL_UPDATE_SESSION = """
mutation updateSession($id: Int!, $session: InputActivityLog!) {
    updateSession(id: $id, session: $session) {
        id
    }
}
"""

_GQL_CREATE_LUNCH = """
mutation CreateLunch($lunch: InputLunch!) {
    createLunch(lunch: $lunch) {
        id
    }
}
"""

_GQL_CREATE_SESSION = """
mutation createSession($session: InputActivityLog!) {
    createSession(session: $session) {
        id
    }
}
"""

# Minimal slice of currentUserContext to discover the user's company id.
# Used once per session (cached in session.json) so we can plug it into the
# DetailedAttendanceReport request.
_GQL_USER_COMPANY = """
query MeAndCompany {
    currentUserContext {
        user {
            id
            company { id }
        }
    }
}
"""

# DetailedAttendanceReport — per-day attendance breakdown plus period totals.
# Captured verbatim from the web UI then stripped down to the fields we use,
# keeping the schema query name and variable shape intact.
_GQL_DETAILED_REPORT = """
query DetailedAttendanceReport(
    $pageNumber: Int
    $pageSize: Int
    $reportSettings: InputReportSettings!
) {
    detailedAttendanceReport(
        pageNumber: $pageNumber
        pageSize: $pageSize
        reportSettings: $reportSettings
    ) {
        elements {
            startDate
            endDate
            timezone
            totalDaysWorked
            totalHours
            totalLackOfHours
            creditHours
            totalStandardHours
            totalAbsenceHours
            user {
                id
                fullName
                company { id name }
            }
            records {
                date
                weekDay
                totalSeconds
                standardSeconds
                isWorkDay
                allSessions {
                    id
                    activityLogId
                    startDate
                    endDate
                    activityLog {
                        locationType
                        lunchSessions {
                            id
                            startDateInUserTimezone
                            endDateInUserTimezone
                        }
                    }
                }
                allAbsenceTimes {
                    absenceName
                    seconds
                    durationType
                }
            }
        }
        paginationInfo { pagesCount currentPageNumber pageSize }
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
        # Tracks whether we've issued DashboardWebPunchQuery this run; some
        # backends require it as a "warm-up" before they'll serve
        # DetailedAttendanceReport. See _ensure_warm.
        self._warmed_up = False

    # ----- public API -----

    def login(self, force: bool = False) -> None:
        """Authenticate. Reuses a cached token if it's still valid."""
        if not force and self._session and not self._session.is_expired():
            log.debug("Reusing cached access-token (expires in %ds)",
                      int(self._session.expires_at - time.time()))
            return

        if not self.config.username or not self.config.password:
            raise AuthenticationError(
                f"No credentials configured. Edit {self.config.path} and set "
                "username/password under the [timeclock] section."
            )

        log.info("Logging in to %s as %s", self.config.base_url, self.config.username)

        # --- step 1: /api/auth/login -----------------------------------------
        login_body = {
            "username": self.config.username,
            "client_id": self.config.client_id,
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
        # Re-login invalidates any previous warm-up.
        self._warmed_up = False
        self._save_session()

    def punch_in(
        self,
        location: Optional[str] = None,
        *,
        at: Optional[datetime] = None,
        force: bool = False,
    ) -> Entry:
        """Punch in.

        ``location`` accepts user-friendly forms (``office``, ``remote``, …).
        ``at`` optionally specifies a retroactive start time (tz-aware datetime).
        ``force`` skips the "already punched in" check and lets the server reject.

        When already clocked in and ``location`` or ``at`` is given, the open
        session is updated in place instead of raising ConflictError.
        """
        loc = normalize_location(location or self.config.default_location)

        if not (force or self.config.force_overwrite):
            status = self.status()
            if status.clocked_in:
                if location is not None or at is not None:
                    update_loc = (
                        normalize_location(location) if location is not None
                        else (status.location or "OFFICE")
                    )
                    log.info("Already clocked in — updating open session (loc=%s)", update_loc)
                    return self._update_open_session(status, update_loc, at)
                raise ConflictError(
                    f"Already clocked in since {status.since.astimezone().strftime('%H:%M')} "
                    f"@ {status.location}. Pass --force to overwrite."
                )
            if loc is None:
                loc = status.default_location or "OFFICE"
                log.info("No location given; falling back to server default %s", loc)
        elif loc is None:
            status = self.status()
            loc = status.default_location or "OFFICE"
            log.info("No location given; falling back to server default %s", loc)

        # InputWebPunch doesn't accept startDate; punch in now, then patch via updateSession.
        log.info("Punching in at %s%s", loc, f" (manual start={at:%H:%M})" if at else "")
        entry = self._make_punch(loc, "PUNCH_IN")
        if at is not None and entry.session_id is not None:
            tz_name = entry.timezone_name or "UTC"
            entry = self.update_session(
                entry.session_id,
                start_ts=_to_server_ts(at, tz_name),
                end_ts=None,
                location=loc,
                date_str=at.strftime("%Y-%m-%d"),
                timezone_name=tz_name,
            )
        return entry

    def punch_out(self, *, at: Optional[datetime] = None, force: bool = False) -> Entry:
        """Punch out, with optional retroactive time and force flag.

        When already clocked out and ``at`` is given, the most recent session's
        end time is updated in place instead of raising ConflictError.

        Note: ``InputWebPunch`` does not accept ``endDate``, so a custom end
        time is always applied via ``updateSession`` after the initial punch.
        """
        status = self.status()
        loc = status.location or status.default_location or "OFFICE"

        if not (force or self.config.force_overwrite):
            if not status.clocked_in:
                if at is not None:
                    log.info("Already clocked out — updating last session end time")
                    return self._update_last_session_end(at)
                raise ConflictError(
                    "Not currently clocked in — nothing to punch out. "
                    "Pass --force to send the request anyway."
                )
        elif not status.clocked_in and at is not None:
            log.info("Already clocked out (force) — updating last session end time")
            return self._update_last_session_end(at)

        # InputWebPunch doesn't accept endDate; punch out at current time,
        # then patch the end time via updateSession if a custom time was given.
        log.info("Punching out (location=%s)%s", loc, f" (manual end={at:%H:%M})" if at else "")
        entry = self._make_punch(loc, "PUNCH_OUT")
        if at is not None:
            entry = self._update_last_session_end(at)
        return entry

    def status(self) -> Status:
        log.debug("Fetching current status")
        self.login()
        data = self._graphql(_GQL_STATUS, operation_name="DashboardWebPunchQuery")
        self._warmed_up = True
        wp = data.get("webPunch") or {}
        session = wp.get("session") or {}
        tz_name = session.get("timezone") if session else None
        clocked_in = bool(session) and not session.get("endDate")

        # Status endpoint returns UTC strings (despite the timezone field).
        since = _parse_utc(session.get("startDate")) if session else None
        location = session.get("locationType") if session else None

        if clocked_in and since is not None:
            today_total = _now_aware(since.tzinfo) - since
        else:
            today_total = None

        lunch_node = wp.get("lunchSession")
        lunch = None
        if lunch_node:
            lstart = _parse_utc(lunch_node.get("startDate"))
            if lstart is not None:
                lunch = LunchBreak(
                    started_at=lstart,
                    ended_at=_parse_utc(lunch_node.get("endDate")),
                )

        session_id = int(session["id"]) if session.get("id") else None
        return Status(
            clocked_in=clocked_in,
            since=since,
            location=location,
            today_total=today_total,
            available_locations=wp.get("locationTypes") or list(LOCATION_TYPES),
            default_location=wp.get("locationType"),
            timezone_name=tz_name,
            lunch=lunch,
            session_id=session_id,
        )

    def list_entries(self, days: int = 5) -> List[Entry]:
        """Return recent punch sessions, lunches, and absences.

        Ordering follows the ``list_recent_last`` config option:
        true → oldest first (newest last), false → newest first.
        """
        end_dt, start_dt = _window_for_days(days)
        report = self._fetch_period_report(int(start_dt.timestamp()), int(end_dt.timestamp()))
        tz_name = report.get("timezone")
        entries: List[Entry] = []
        for record in report.get("records", []):
            for sess in record.get("allSessions") or []:
                started = _parse_in_zone(sess.get("startDate"), tz_name)
                if started is None:
                    continue
                activity = sess.get("activityLog") or {}
                entries.append(Entry(
                    started_at=started,
                    ended_at=_parse_in_zone(sess.get("endDate"), tz_name),
                    location=activity.get("locationType"),
                    session_id=int(sess["activityLogId"]) if sess.get("activityLogId") else None,
                    timezone_name=tz_name,
                ))
                for lunch in activity.get("lunchSessions") or []:
                    lstart = _parse_in_zone(lunch.get("startDateInUserTimezone"), tz_name)
                    if lstart is None:
                        continue
                    entries.append(Entry(
                        started_at=lstart,
                        ended_at=_parse_in_zone(lunch.get("endDateInUserTimezone"), tz_name),
                        location=None,
                        is_lunch=True,
                    ))
            for absence in record.get("allAbsenceTimes") or []:
                day = _parse_in_zone(record.get("date"), tz_name)
                if day is None:
                    continue
                seconds = int(absence.get("seconds") or 0)
                entries.append(Entry(
                    started_at=day,
                    ended_at=day + timedelta(seconds=seconds) if seconds else None,
                    location=None,
                    absence_name=absence.get("absenceName"),
                ))

        # Filter to the requested window — the server sometimes returns the
        # whole payroll period regardless of our timestamps.
        cutoff = start_dt.astimezone(entries[0].started_at.tzinfo) if entries else start_dt
        entries = [e for e in entries if e.started_at >= cutoff]

        recent_last = self.config.list_recent_last
        entries.sort(key=lambda e: e.started_at, reverse=not recent_last)
        return entries

    def create_lunch(self, start_dt: datetime, end_dt: datetime) -> None:
        """Record a completed lunch break on the work session that contains it.

        Raises ``TimeclockError`` if no work session exists today or a lunch
        break has already been recorded for today's session.
        """
        today = start_dt.astimezone().date()

        # status() is the reliable source for session_id — it comes straight
        # from the webPunch query which always returns the current session id.
        # list_entries() is used only for the duplicate-lunch check.
        status = self.status()
        session_id: Optional[int] = status.session_id

        if session_id is None:
            # Punched out — find today's session id from list_entries.
            entries = self.list_entries(days=1)
            today_work = [
                e for e in entries
                if not e.is_lunch and not e.is_absence
                and e.started_at.astimezone().date() == today
            ]
            if not today_work:
                raise TimeclockError("No work session found for today — punch in first.")
            session_id = today_work[0].session_id
            if session_id is None:
                raise TimeclockError("Could not determine today's session ID.")
            # Duplicate check from full entry list.
            today_lunch = [
                e for e in entries
                if e.is_lunch and e.started_at.astimezone().date() == today
            ]
        else:
            # Currently punched in — duplicate check via list_entries.
            entries = self.list_entries(days=1)
            today_lunch = [
                e for e in entries
                if e.is_lunch and e.started_at.astimezone().date() == today
            ]

        if today_lunch:
            existing = today_lunch[0]
            end_str = existing.ended_at.astimezone().strftime("%H:%M") if existing.ended_at else "…"
            raise TimeclockError(
                f"Lunch break already recorded today "
                f"({existing.started_at.astimezone().strftime('%H:%M')}–{end_str})."
            )

        start_utc = start_dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        end_utc = end_dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        log.info("CreateLunch session=%d %s – %s UTC", session_id, start_utc, end_utc)
        self.login()
        data = self._graphql(
            _GQL_CREATE_LUNCH,
            operation_name="CreateLunch",
            variables={"lunch": {
                "sessionId": session_id,
                "startPunchDate": start_utc,
                "endPunchDate": end_utc,
            }},
        )
        node = data.get("createLunch")
        if not node or not node.get("id"):
            raise TimeclockError(f"createLunch returned unexpected response: {data}")

    def set_day(
        self,
        date,
        location: str,
        time_pairs: List[Tuple[datetime, datetime]],
        break_pair: Optional[Tuple[datetime, datetime]] = None,
    ) -> None:
        """Create sessions for a day. Raises TimeclockError if entries already exist."""
        from datetime import date as _date  # avoid shadowing module name
        local_tz = datetime.now().astimezone().tzinfo
        start_of_day = datetime(date.year, date.month, date.day, 0, 0, 0, tzinfo=local_tz)
        end_of_day = datetime(date.year, date.month, date.day, 23, 59, 59, tzinfo=local_tz)

        try:
            report = self._fetch_period_report(
                int(start_of_day.timestamp()), int(end_of_day.timestamp())
            )
        except TimeclockError as exc:
            if "no elements" in str(exc).lower():
                report = {}
            else:
                raise

        date_str = str(date)
        tz_name = report.get("timezone") or "UTC"

        for record in report.get("records", []):
            rec_date = (record.get("date") or "")[:10]
            if rec_date != date_str:
                continue
            sessions = record.get("allSessions") or []
            absences = record.get("allAbsenceTimes") or []
            if sessions:
                raise TimeclockError(
                    f"{date_str} already has {len(sessions)} session(s). "
                    "Clear the day first before using `tc set`."
                )
            if absences:
                names = ", ".join(a.get("absenceName", "?") for a in absences)
                raise TimeclockError(
                    f"{date_str} has absence entry ({names}). "
                    "Clear the day first before using `tc set`."
                )

        first_session_id: Optional[int] = None
        for i, (start_dt, end_dt) in enumerate(time_pairs):
            session_id = self._create_session(date_str, start_dt, end_dt, location, tz_name)
            if i == 0:
                first_session_id = session_id

        if break_pair is not None and first_session_id is not None:
            b_start, b_end = break_pair
            start_utc = b_start.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            end_utc = b_end.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            log.info("CreateLunch (set_day) session=%d %s – %s UTC", first_session_id, start_utc, end_utc)
            self.login()
            data = self._graphql(
                _GQL_CREATE_LUNCH,
                operation_name="CreateLunch",
                variables={"lunch": {
                    "sessionId": first_session_id,
                    "startPunchDate": start_utc,
                    "endPunchDate": end_utc,
                }},
            )
            node = data.get("createLunch")
            if not node or not node.get("id"):
                raise TimeclockError(f"createLunch returned unexpected response: {data}")

    def _create_session(
        self,
        date_str: str,
        start_dt: datetime,
        end_dt: datetime,
        location: str,
        timezone_name: str,
    ) -> int:
        """Call createSession mutation. Returns the new session id."""
        self.login()
        assert self._session is not None
        variables = {
            "session": {
                "date": date_str,
                "startDateTs": _to_server_ts(start_dt, timezone_name),
                "endDateTs": _to_server_ts(end_dt, timezone_name),
                "userId": self._session.user_id,
                "locationType": location,
                "timezone": timezone_name,
            }
        }
        log.info(
            "createSession %s %s–%s @ %s",
            date_str, start_dt.strftime("%H:%M"), end_dt.strftime("%H:%M"), location,
        )
        data = self._graphql(_GQL_CREATE_SESSION, operation_name="createSession", variables=variables)
        node = data.get("createSession")
        if not node or not node.get("id"):
            raise TimeclockError(f"createSession returned unexpected response: {data}")
        return int(node["id"])

    def period_summary(self) -> PeriodSummary:
        """Aggregate totals for the current payroll period (server-defined)."""
        end_dt, start_dt = _window_for_days(31)  # any window inside the period works
        report = self._fetch_period_report(int(start_dt.timestamp()), int(end_dt.timestamp()))
        tz_name = report.get("timezone")
        return PeriodSummary(
            start=_parse_in_zone(report.get("startDate"), tz_name) or start_dt.astimezone(),
            end=_parse_in_zone(report.get("endDate"), tz_name) or end_dt.astimezone(),
            total_hours=float(report.get("totalHours") or 0),
            standard_hours=float(report.get("totalStandardHours") or 0),
            credit_hours=float(report.get("creditHours") or 0),
            lack_of_hours=float(report.get("totalLackOfHours") or 0),
            absence_hours=float(report.get("totalAbsenceHours") or 0),
            days_worked=int(report.get("totalDaysWorked") or 0),
            timezone=str(tz_name or ""),
        )

    def update_session(
        self,
        session_id: int,
        *,
        start_ts: int,
        end_ts: Optional[int],
        location: str,
        date_str: str,
        timezone_name: str,
    ) -> Entry:
        """Call the updateSession GraphQL mutation directly."""
        self.login()
        assert self._session is not None
        variables = {
            "id": session_id,
            "session": {
                "date": date_str,
                "startDateTs": start_ts,
                "endDateTs": end_ts,
                "userId": self._session.user_id,
                "locationType": location,
                "timezone": timezone_name,
            },
        }
        log.info("updateSession id=%d loc=%s start=%d end=%s", session_id, location, start_ts, end_ts)
        data = self._graphql(
            _GQL_UPDATE_SESSION,
            operation_name="updateSession",
            variables=variables,
        )
        node = data.get("updateSession")
        if not node or not node.get("id"):
            raise TimeclockError(f"updateSession returned unexpected response: {data}")
        local_tz = _resolve_tz(timezone_name)
        return Entry(
            started_at=datetime.fromtimestamp(start_ts, tz=timezone.utc).replace(tzinfo=local_tz),
            ended_at=datetime.fromtimestamp(end_ts, tz=timezone.utc).replace(tzinfo=local_tz) if end_ts else None,
            location=location,
        )

    def _update_open_session(
        self,
        status: "Status",
        new_location: str,
        new_start: Optional[datetime],
    ) -> Entry:
        """Update the currently-open session's location and/or start time."""
        if status.session_id is None:
            raise TimeclockError("Cannot update session: session ID not available from status")
        if status.since is None:
            raise TimeclockError("Cannot update session: current session has no start time")
        tz_name = status.timezone_name or "UTC"
        tz_obj = _resolve_tz(tz_name)
        start_ts = (
            _to_server_ts(new_start, tz_name)
            if new_start is not None
            else _to_server_ts(status.since, tz_name)
        )
        date_str = status.since.astimezone(tz_obj).strftime("%Y-%m-%d")
        return self.update_session(
            status.session_id,
            start_ts=start_ts,
            end_ts=None,
            location=new_location,
            date_str=date_str,
            timezone_name=tz_name,
        )

    def _update_last_session_end(self, new_end: datetime) -> Entry:
        """Update the end time of today's most recent completed session."""
        today_end, today_start = _window_for_days(1)
        report = self._fetch_period_report(int(today_start.timestamp()), int(today_end.timestamp()))
        tz_name = report.get("timezone")

        last_sess: Optional[dict] = None
        last_start_dt: Optional[datetime] = None
        for record in report.get("records", []):
            for sess in record.get("allSessions") or []:
                if not sess.get("endDate"):
                    continue
                started = _parse_in_zone(sess.get("startDate"), tz_name)
                if started and (last_start_dt is None or started > last_start_dt):
                    last_sess = sess
                    last_start_dt = started

        if last_sess is None or last_start_dt is None:
            raise TimeclockError("No completed session found today to update.")

        raw_id = last_sess.get("activityLogId") or last_sess.get("id")
        if not raw_id:
            raise TimeclockError("Cannot update session: no ID found in report response.")
        session_id = int(raw_id)
        activity = last_sess.get("activityLog") or {}
        location = activity.get("locationType") or "OFFICE"
        start_ts = _to_server_ts(last_start_dt, tz_name)
        end_ts = _to_server_ts(new_end, tz_name)
        tz_obj = _resolve_tz(tz_name)
        date_str = last_start_dt.astimezone(tz_obj).strftime("%Y-%m-%d")
        return self.update_session(
            session_id,
            start_ts=start_ts,
            end_ts=end_ts,
            location=location,
            date_str=date_str,
            timezone_name=tz_name or "UTC",
        )

    # ----- internals -----

    def _ensure_warm(self) -> None:
        """Some backends 400 on DetailedAttendanceReport unless we've called
        DashboardWebPunchQuery first this run. Issue it once, idempotently."""
        if self._warmed_up:
            return
        log.debug("Issuing warm-up DashboardWebPunchQuery before report call")
        try:
            self._graphql(_GQL_STATUS, operation_name="DashboardWebPunchQuery")
        except Exception as exc:  # noqa: BLE001 — warm-up is best-effort
            log.debug("Warm-up failed (continuing anyway): %s", exc)
        finally:
            self._warmed_up = True

    def _fetch_period_report(self, start_ts: int, end_ts: int) -> dict:
        """Run DetailedAttendanceReport and return the single element block."""
        self.login()
        self._ensure_warm()
        company_id = self._ensure_company_id()
        assert self._session is not None
        variables = {
            "pageNumber": 1,
            "pageSize": 1,
            "reportSettings": {
                "userPickMode": "PICK_SELECT",
                "startDateTs": start_ts,
                "endDateTs": end_ts,
                "companies": [company_id],
                "companyBranches": [],
                "groups": [],
                "absences": [],
                "contracts": [],
                "users": [self._session.user_id],
                "specifyPeriod": False,
                "filterByAbsences": False,
                "onlyAddedOrEditedHours": False,
                "onlyEmptyDays": False,
                "onlyExceptions": False,
                "onlyEmptyEntries": False,
                "onlyAutomaticPunchOut": False,
                "notApprovedByEmployee": False,
                "notApprovedByManager": False,
                "includeTerminated": True,
                "onlyTerminated": False,
                "showOnlyDaysWithoutStandardHours": False,
                "sortByUserFullName": False,
                "weekDays": [],
                "filterByWeekDays": False,
                "showBonusColumn": False,
                "showTravelDurationAndDistance": False,
            },
        }
        log.info(
            "Fetching DetailedAttendanceReport for %s … %s",
            datetime.fromtimestamp(start_ts).strftime("%Y-%m-%d"),
            datetime.fromtimestamp(end_ts).strftime("%Y-%m-%d"),
        )
        data = self._graphql(
            _GQL_DETAILED_REPORT,
            operation_name="DetailedAttendanceReport",
            variables=variables,
        )
        elements = (data.get("detailedAttendanceReport") or {}).get("elements") or []
        if not elements:
            raise TimeclockError(
                "DetailedAttendanceReport returned no elements — server may not have data for this period."
            )
        return elements[0]

    def _ensure_company_id(self) -> int:
        """Return the cached company_id, fetching it from the server if needed."""
        assert self._session is not None
        if self._session.company_id is not None:
            return self._session.company_id
        log.debug("Company id not cached — fetching via currentUserContext")
        data = self._graphql(_GQL_USER_COMPANY, operation_name="MeAndCompany")
        try:
            company_id = int(data["currentUserContext"]["user"]["company"]["id"])
        except (KeyError, TypeError, ValueError) as exc:
            raise TimeclockError(
                f"Could not extract company id from currentUserContext response: {data}"
            ) from exc
        self._session.company_id = company_id
        self._save_session()
        log.debug("Cached company_id=%d", company_id)
        return company_id

    def _make_punch(
        self,
        location: str,
        punch_type: str,
        *,
        at: Optional[datetime] = None,
    ) -> Entry:
        self.login()
        punch: dict = {"locationType": location, "punchType": punch_type}
        if at is not None:
            # The server expects UTC datetimes elsewhere; mirror that convention.
            utc_str = at.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            field = "startDate" if punch_type == "PUNCH_IN" else "endDate"
            punch[field] = utc_str
            log.info(
                "Sending retroactive %s with %s=%s UTC (— if the server rejects, "
                "the InputWebPunch schema needs a HAR capture; see NOTES.md)",
                punch_type, field, utc_str,
            )
        try:
            data = self._graphql(
                _GQL_MAKE_PUNCH,
                operation_name="makePunchDashboard",
                variables={"punch": punch},
            )
        except TimeclockError as exc:
            if at is not None:
                raise TimeclockError(
                    "Retroactive punch failed — most likely the InputWebPunch "
                    "schema doesn't accept the field we tried. Capture a HAR of "
                    "editing a punch time in the web UI so we can wire up the "
                    f"right mutation. Server said: {exc}"
                ) from exc
            raise

        node = data.get("makePunchByDashboard")
        if not node:
            raise TimeclockError(f"Punch response missing makePunchByDashboard: {data}")
        return Entry(
            started_at=_parse_utc(node["startDate"]),
            ended_at=_parse_utc(node.get("endDate")),
            location=node.get("locationType"),
            session_id=int(node["id"]) if node.get("id") else None,
            timezone_name=node.get("timezone"),
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
            # Tolerate unknown future fields and old caches missing newer ones.
            known = {f for f in _Session.__dataclass_fields__}
            cleaned = {k: v for k, v in raw.items() if k in known}
            return _Session(**cleaned)
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


def _resolve_tz(name: Optional[str]):
    """Return a tzinfo for ``name`` or the system local one if unavailable."""
    if name and ZoneInfo is not None:
        try:
            return ZoneInfo(name)
        except Exception:  # noqa: BLE001 — bad name or missing tzdata
            log.debug("Could not resolve timezone %r — falling back to local", name)
    return datetime.now().astimezone().tzinfo


def _parse_naive(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            log.debug("Unparseable datetime from server: %r", value)
            return None


def _parse_utc(value: Optional[str]) -> Optional[datetime]:
    """Parse a server datetime as UTC and return a tz-aware datetime."""
    naive = _parse_naive(value)
    if naive is None:
        return None
    if naive.tzinfo is None:
        return naive.replace(tzinfo=timezone.utc)
    return naive.astimezone(timezone.utc)


def _parse_in_zone(value: Optional[str], tz_name: Optional[str]) -> Optional[datetime]:
    """Parse a server datetime that's already in ``tz_name`` (or local) wall clock."""
    naive = _parse_naive(value)
    if naive is None:
        return None
    if naive.tzinfo is not None:
        return naive
    return naive.replace(tzinfo=_resolve_tz(tz_name))


def _parse_dt(value: Optional[str]) -> Optional[datetime]:
    """Backwards-compatible alias for ``_parse_naive`` — used by tests."""
    return _parse_naive(value)


def _now_aware(tz=None) -> datetime:
    """Current time as a tz-aware ``datetime``. Uses the given tz or the local one."""
    if tz is None:
        return datetime.now().astimezone()
    return datetime.now(tz=tz)


def _to_server_ts(dt: datetime, tz_name: Optional[str] = None) -> int:
    """Convert a tz-aware datetime to the server's timestamp format.

    The server's startDateTs/endDateTs fields expect epoch(local_time_as_UTC):
    to store 08:22 CEST, send epoch(08:22 UTC). Verified from HAR captures.
    """
    local = dt.astimezone(_resolve_tz(tz_name))
    return int(local.replace(tzinfo=timezone.utc).timestamp())


def _window_for_days(days: int) -> Tuple[datetime, datetime]:
    """Return ``(end_dt, start_dt)`` covering the last ``days`` calendar days.

    Both bounds are timezone-aware (system local). The window is inclusive on
    both ends: today 00:00 through today 23:59:59 is one day.
    """
    days = max(1, int(days))
    now = datetime.now().astimezone()
    end_dt = now.replace(hour=23, minute=59, second=59, microsecond=0)
    start_dt = (now - timedelta(days=days - 1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return end_dt, start_dt
