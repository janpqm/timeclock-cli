"""Tests for the ini-file config loader."""

from __future__ import annotations

import stat
import sys
from pathlib import Path

import pytest

from timeclock_cli import config as cfgmod
from timeclock_cli.config import (
    DEFAULT_BASE_URL,
    DEFAULT_CLIENT_ID,
    SECTION,
    Config,
    load_config,
)


# ---------------------------------------------------------------- first-run path


def test_missing_file_writes_template_with_single_section(tmp_path: Path) -> None:
    cfg_path = tmp_path / "config.ini"

    cfg = load_config(cfg_path)

    assert cfg_path.exists(), "template should be written on first run"
    text = cfg_path.read_text(encoding="utf-8")
    assert f"[{SECTION}]" in text, "template should use the [timeclock] section"
    assert "[auth]" not in text, "template should not use the legacy [auth] section"

    # the placeholder template is treated as "not configured" so we error nicely.
    assert not cfg.is_complete()
    assert cfg.client_id == DEFAULT_CLIENT_ID
    assert cfg.base_url == DEFAULT_BASE_URL


def test_template_secured_to_0600(tmp_path: Path) -> None:
    if sys.platform == "win32":
        pytest.skip("chmod is a no-op on Windows")
    cfg_path = tmp_path / "config.ini"
    load_config(cfg_path)
    mode = cfg_path.stat().st_mode
    assert not (mode & (stat.S_IRGRP | stat.S_IROTH | stat.S_IWGRP | stat.S_IWOTH))


# ---------------------------------------------------------------- happy path


def _write(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")


def test_loads_single_section_format(tmp_path: Path) -> None:
    cfg_path = tmp_path / "config.ini"
    _write(
        cfg_path,
        """\
[timeclock]
username         = me@example.com
password         = hunter2
default_location = home
base_url         = https://example.com/
client_id        = override-me
""",
    )

    cfg = load_config(cfg_path)

    assert cfg.username == "me@example.com"
    assert cfg.password == "hunter2"
    assert cfg.default_location == "home"
    assert cfg.base_url == "https://example.com"  # trailing slash stripped
    assert cfg.client_id == "override-me"
    assert cfg.is_complete()
    assert cfg.path == cfg_path


def test_template_roundtrips(tmp_path: Path) -> None:
    """Write the bundled template, replace the placeholder creds, re-load."""
    cfg_path = tmp_path / "config.ini"
    cfgmod._write_template(cfg_path)
    text = cfg_path.read_text(encoding="utf-8")
    text = text.replace("you@yourcompany.com", "real@example.com")
    text = text.replace("your-password", "real-secret")
    cfg_path.write_text(text, encoding="utf-8")

    cfg = load_config(cfg_path)

    assert cfg.username == "real@example.com"
    assert cfg.password == "real-secret"
    assert cfg.default_location == "office"
    assert cfg.client_id == DEFAULT_CLIENT_ID
    assert cfg.base_url == DEFAULT_BASE_URL


# ---------------------------------------------------------------- edge cases


def test_unedited_placeholders_treated_as_empty(tmp_path: Path) -> None:
    """The template ships with `you@yourcompany.com` / `your-password` as
    placeholders. We don't want those to count as "configured"."""
    cfg_path = tmp_path / "config.ini"
    cfgmod._write_template(cfg_path)
    cfg = load_config(cfg_path)
    assert cfg.username == ""
    assert cfg.password == ""
    assert not cfg.is_complete()


def test_missing_optional_keys_use_defaults(tmp_path: Path) -> None:
    cfg_path = tmp_path / "config.ini"
    _write(
        cfg_path,
        """\
[timeclock]
username = me@example.com
password = hunter2
""",
    )
    cfg = load_config(cfg_path)
    assert cfg.default_location is None
    assert cfg.base_url == DEFAULT_BASE_URL
    assert cfg.client_id == DEFAULT_CLIENT_ID


def test_legacy_multi_section_still_loads(tmp_path: Path) -> None:
    """Older versions wrote [auth]/[defaults]/[server]. Don't break them."""
    cfg_path = tmp_path / "config.ini"
    _write(
        cfg_path,
        """\
[auth]
username = legacy@example.com
password = legacy-pw

[defaults]
location = field

[server]
base_url = https://live.timeclock365.com
client_id = legacy-client-id
""",
    )

    cfg = load_config(cfg_path)
    assert cfg.username == "legacy@example.com"
    assert cfg.password == "legacy-pw"
    assert cfg.default_location == "field"
    assert cfg.client_id == "legacy-client-id"
