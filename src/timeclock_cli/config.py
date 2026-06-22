"""Configuration loading.

Reads ``~/.config/timeclock-cli/config.ini`` (or the platform-equivalent
``XDG_CONFIG_HOME``). On first run, writes a template the user can edit.

All keys live under a single ``[timeclock]`` section to keep the file flat
and editable. The previous multi-section layout (``[auth]``/``[defaults]``/
``[server]``) is still read for backwards compatibility, but the template we
write — and the format documented in the README — uses the single section.
"""

from __future__ import annotations

import configparser
import logging
import os
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


log = logging.getLogger(__name__)

SECTION = "timeclock"
DEFAULT_BASE_URL = "https://live.timeclock365.com"

# Stable per-app constant the web UI sends as `client_id` to /api/auth/login.
# It's the same value across every captured session. If timeclock365 ever
# rotates this, users can override it in config.ini without a code change.
DEFAULT_CLIENT_ID = (
    "ZWUyYzllNGUzYjlhZDAwNzBiYTgwN2ZjYjU2YWZiNDRhZDhiYTFkNmM5NTkzNzJlNDc1NzRkM2Y5OGFiODc0Nw"
)

_TEMPLATE = f"""\
# timeclock-cli configuration
# Fill in your credentials below, then run `tc status` to verify.
# Permissions on this file should be 0600 (owner-only); the tool sets that on
# first write and will warn if it gets relaxed later.

[timeclock]
# Your timeclock365 login.
username         = you@yourcompany.com
password         = your-password

# Default location for `tc in` when none is given on the command line.
# Allowed values: office, home, abroad, field (case-insensitive).
default_location = office

# Allow `tc in HH:MM` / `tc out HH:MM` to overwrite the currently-open session
# when one already exists. Per-invocation override: pass `-f` / `--force`.
force_overwrite  = false

# `tc list` ordering. true = oldest first, newest last (like `tail`);
#                   false = newest first, oldest last.
list_recent_last = true

# Default lunch-break start time and duration for `tc break`.
# `tc break` uses both; `tc break HH:MM` overrides the start time only.
break_start    = 12:00
break_duration = 30

# Default start and end times for `tc set` when no times are given.
set_start = 09:00
set_end   = 17:00

# Day of month the payroll period starts on (1–28). The period shown by
# `tc status` runs from this day through the day before it next month.
# Example: 15 → Jun 15 … Jul 14. Set to 1 for calendar-month periods.
period_start_day = 15

# Override only if you point at a self-hosted timeclock365 instance.
base_url         = {DEFAULT_BASE_URL}

# client_id sent to /api/auth/login. This is the stable value the web UI uses.
# If timeclock365 ever rotates it (login starts returning a non-200), capture
# the new value from DevTools and paste it here — no code change needed.
client_id        = {DEFAULT_CLIENT_ID}
"""


@dataclass
class Config:
    username: str
    password: str
    base_url: str
    default_location: Optional[str]
    path: Path
    client_id: str = DEFAULT_CLIENT_ID
    force_overwrite: bool = False
    list_recent_last: bool = True
    break_start: str = "12:00"
    break_duration: int = 30
    set_start: str = "09:00"
    set_end: str = "17:00"
    period_start_day: int = 15

    def is_complete(self) -> bool:
        return bool(self.username and self.password)


def _to_bool(value: str, default: bool = False) -> bool:
    """Parse the usual ini-file truthy/falsy spellings."""
    if value is None:
        return default
    s = str(value).strip().lower()
    if s in {"1", "true", "yes", "on"}:
        return True
    if s in {"0", "false", "no", "off"}:
        return False
    return default


def config_dir() -> Path:
    """Return the directory where the config file lives.

    Honors ``XDG_CONFIG_HOME`` on Linux/macOS and falls back to ``~/.config``.
    On Windows, uses ``%APPDATA%``.
    """
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(base) / "timeclock-cli"
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "timeclock-cli"


def config_path() -> Path:
    return config_dir() / "config.ini"


def session_path() -> Path:
    return config_dir() / "session.json"


def _write_template(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_TEMPLATE, encoding="utf-8")
    # Best-effort 0600. No-op on Windows (chmod is mostly ignored there).
    try:
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass
    log.info("Wrote config template to %s — fill in your credentials and re-run.", path)


def _get(parser: configparser.ConfigParser, *candidates) -> str:
    """Return the first non-empty value across the given (section, key) pairs.

    Lets a single call accept both the modern ``[timeclock]`` layout and the
    older ``[auth]``/``[defaults]``/``[server]`` one for backwards compat.
    """
    for section, key in candidates:
        if parser.has_option(section, key):
            val = parser.get(section, key, fallback="").strip()
            if val:
                return val
    return ""


def load_config(path: Optional[Path] = None) -> Config:
    """Load config from disk. Writes a template and exits if missing."""
    cfg_path = path or config_path()

    if not cfg_path.exists():
        log.warning("Config file not found at %s — creating template.", cfg_path)
        _write_template(cfg_path)
        # Returning an empty Config lets the CLI surface a clean error message
        # rather than crashing with a KeyError.
        return Config(
            username="",
            password="",
            base_url=DEFAULT_BASE_URL,
            default_location=None,
            path=cfg_path,
            client_id=DEFAULT_CLIENT_ID,
        )  # force_overwrite and list_recent_last keep dataclass defaults

    parser = configparser.ConfigParser()
    parser.read(cfg_path, encoding="utf-8")

    username = _get(parser, (SECTION, "username"), ("auth", "username"))
    password = _get(parser, (SECTION, "password"), ("auth", "password"))
    base_url = _get(parser, (SECTION, "base_url"), ("server", "base_url")) or DEFAULT_BASE_URL
    default_location = _get(
        parser,
        (SECTION, "default_location"),
        (SECTION, "location"),
        ("defaults", "location"),
    ) or None
    client_id = _get(parser, (SECTION, "client_id"), ("server", "client_id")) or DEFAULT_CLIENT_ID
    force_overwrite = _to_bool(_get(parser, (SECTION, "force_overwrite")), default=False)
    list_recent_last = _to_bool(_get(parser, (SECTION, "list_recent_last")), default=True)
    break_start = _get(parser, (SECTION, "break_start")) or "12:00"
    _break_duration_raw = _get(parser, (SECTION, "break_duration"))
    try:
        break_duration = int(_break_duration_raw) if _break_duration_raw else 30
    except ValueError:
        break_duration = 30
    set_start = _get(parser, (SECTION, "set_start")) or "09:00"
    set_end = _get(parser, (SECTION, "set_end")) or "17:00"
    _period_start_raw = _get(parser, (SECTION, "period_start_day"))
    try:
        period_start_day = int(_period_start_raw) if _period_start_raw else 15
    except ValueError:
        period_start_day = 15
    period_start_day = max(1, min(28, period_start_day))

    # Don't ignore obvious template placeholders — treat them as "not configured".
    if username in {"you@yourcompany.com", "your.email@example.com"}:
        username = ""
    if password in {"your-password", "your_password"}:
        password = ""

    # Warn about world-readable config (skip on Windows where mode bits are meaningless).
    if sys.platform != "win32":
        mode = cfg_path.stat().st_mode
        if mode & (stat.S_IRGRP | stat.S_IROTH):
            log.warning(
                "Config file %s is readable by group/other. Run: chmod 600 %s",
                cfg_path,
                cfg_path,
            )

    log.debug("Loaded config from %s (user=%s, base_url=%s)", cfg_path, username or "<empty>", base_url)
    return Config(
        username=username,
        password=password,
        base_url=base_url.rstrip("/"),
        default_location=default_location,
        path=cfg_path,
        client_id=client_id,
        force_overwrite=force_overwrite,
        list_recent_last=list_recent_last,
        break_start=break_start,
        break_duration=break_duration,
        set_start=set_start,
        set_end=set_end,
        period_start_day=period_start_day,
    )
