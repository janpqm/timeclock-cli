"""Command-line entry point.

Usage:
    tc                  → status (default)
    tc status           → status
    tc in [location]    → punch in
    tc out              → punch out
    tc list [-n DAYS]   → list recent entries
    tc --version
    tc --help
"""

from __future__ import annotations

import argparse
import logging
import sys
from typing import List, Optional

import requests.exceptions

from . import __version__
from .client import AuthenticationError, TimeclockClient, TimeclockError
from .config import Config, load_config
from .logging_setup import configure_logging


log = logging.getLogger("timeclock_cli.cli")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tc",
        description="Command-line client for timeclock365.com",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  tc                 show current status (default)\n"
            "  tc in              punch in (uses default location if set)\n"
            "  tc in office       punch in at 'office'\n"
            "  tc out             punch out\n"
            "  tc list -n 14      list the last 14 days of entries\n"
        ),
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("-v", "--verbose", action="store_true", help="enable debug logging")

    sub = parser.add_subparsers(dest="command", metavar="{in,out,list,status}")

    p_in = sub.add_parser("in", help="punch in")
    p_in.add_argument(
        "location",
        nargs="?",
        help="optional location label (e.g. office, home). Falls back to [defaults] location in config.ini.",
    )

    sub.add_parser("out", help="punch out")

    p_list = sub.add_parser("list", help="list recent entries")
    p_list.add_argument(
        "-n",
        "--days",
        type=int,
        default=5,
        help="number of days to show (default: 5)",
    )

    sub.add_parser("status", help="show current status (default if no command given)")

    return parser


# ---------------------------------------------------------------- command impls


def _cmd_status(client: TimeclockClient) -> int:
    status = client.status()
    if status.clocked_in:
        when = status.since.strftime("%a %H:%M") if status.since else "?"
        loc = f" @ {status.location.lower()}" if status.location else ""
        print(f"clocked IN since {when}{loc}")
        if status.today_total is not None:
            hours = status.today_total.total_seconds() / 3600
            print(f"running: {hours:.2f}h")
    else:
        print("clocked OUT")
        if status.default_location:
            print(f"default location: {status.default_location.lower()}")

    # Period totals — best-effort. If the report query fails we still want
    # status to succeed, so swallow errors quietly.
    try:
        period = client.period_summary()
    except Exception as exc:  # noqa: BLE001
        log.debug("Could not fetch period summary: %s", exc)
    else:
        # %-d isn't portable to Windows; use day-of-month without padding via int()
        start = f"{period.start.strftime('%b')} {period.start.day}"
        end = f"{period.end.strftime('%b')} {period.end.day}"
        print()
        print(f"period {start} – {end}:")
        print(f"  worked   {period.total_hours:>6.2f}h / {period.standard_hours:.2f}h standard")
        if period.lack_of_hours:
            print(f"  missing  {period.lack_of_hours:>6.2f}h")
        if period.credit_hours:
            print(f"  credit   {period.credit_hours:>6.2f}h")
        if period.absence_hours:
            print(f"  absence  {period.absence_hours:>6.2f}h")
    return 0


def _cmd_in(client: TimeclockClient, location: Optional[str]) -> int:
    entry = client.punch_in(location)
    loc = (entry.location or "").lower()
    when = entry.started_at.strftime("%H:%M") if entry.started_at else "?"
    print(f"punched in @ {loc} at {when}")
    return 0


def _cmd_out(client: TimeclockClient) -> int:
    entry = client.punch_out()
    when = entry.ended_at.strftime("%H:%M") if entry.ended_at else "?"
    hours = entry.duration.total_seconds() / 3600
    print(f"punched out at {when} — {hours:.2f}h")
    return 0


def _cmd_list(client: TimeclockClient, days: int) -> int:
    entries = client.list_entries(days=days)
    if not entries:
        print(f"no entries in the last {days} day(s)")
        return 0
    for entry in entries:
        print(entry.format_line())
    return 0


# --------------------------------------------------------------------- main


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    configure_logging(verbose=args.verbose)
    log.debug("parsed args: %s", args)

    try:
        config: Config = load_config()
    except Exception as exc:  # noqa: BLE001 — surface any config problem clearly
        log.error("Failed to load config: %s", exc)
        return 2

    if not config.is_complete():
        log.error(
            "No credentials configured. Edit %s and set [auth] username/password.",
            config.path,
        )
        return 2

    client = TimeclockClient(config)

    try:
        command = args.command or "status"
        if command == "status":
            return _cmd_status(client)
        if command == "in":
            return _cmd_in(client, args.location)
        if command == "out":
            return _cmd_out(client)
        if command == "list":
            return _cmd_list(client, args.days)
        parser.error(f"unknown command: {command}")  # exits with code 2
        return 2  # unreachable, satisfies the type checker
    except AuthenticationError as exc:
        log.error("Authentication failed: %s", exc)
        return 3
    except NotImplementedError as exc:
        log.error("%s", exc)
        log.error("This part of the flow has not been captured yet — see NOTES.md.")
        return 4
    except requests.exceptions.RequestException as exc:
        log.error("Network error talking to %s: %s", config.base_url, exc)
        return 5
    except TimeclockError as exc:
        log.error("timeclock error: %s", exc)
        return 1
    except KeyboardInterrupt:
        log.info("interrupted")
        return 130


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
