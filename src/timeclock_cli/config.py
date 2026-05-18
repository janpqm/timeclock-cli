"""Configuration loading.

Reads ``~/.config/timeclock-cli/config.ini`` (or the platform-equivalent
``XDG_CONFIG_HOME``). On first run, writes a template the user can edit.
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

DEFAULT_BASE_URL = "https://live.timeclock365.com"

_TEMPLATE = """\
# timeclock-cli configuration
# Fill in your timeclock365 credentials, then run `tc status` to verify.

[auth]
username =
password =

[defaults]
# Optional default location for `tc in` when none is given on the command line.
# Example: office, home, client-site
location =

[server]
# Override only if you point at a self-hosted timeclock365 instance.
base_url = https://live.timeclock365.com
"""


@dataclass
class Config:
    username: str
    password: str
    base_url: str
    default_location: Optional[str]
    path: Path

    def is_complete(self) -> bool:
        return bool(self.username and self.password)


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
        )

    parser = configparser.ConfigParser()
    parser.read(cfg_path, encoding="utf-8")

    username = parser.get("auth", "username", fallback="").strip()
    password = parser.get("auth", "password", fallback="").strip()
    base_url = parser.get("server", "base_url", fallback=DEFAULT_BASE_URL).strip() or DEFAULT_BASE_URL
    default_location = parser.get("defaults", "location", fallback="").strip() or None

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
    )
