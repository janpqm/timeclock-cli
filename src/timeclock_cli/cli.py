"""Command-line entry point.

Usage:
    tc                       → status (default)
    tc status                → status
    tc in [location] [HH:MM] → punch in (optionally retroactive)
    tc out [HH:MM]           → punch out (optionally retroactive)
    tc list [-n DAYS]        → list recent entries
    tc --version
    tc --help

``-f`` / ``--force`` overrides the "already punched in / out" safety check
on the ``in`` / ``out`` subcommands. The same effect can be made permanent
via ``force_overwrite = true`` in ``config.ini``.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import timedelta
from itertools import groupby
from typing import List, Optional, Tuple

import requests.exceptions

from . import __version__
from .client import (
    AuthenticationError,
    ConflictError,
    LunchBreak,
    TimeclockClient,
    TimeclockError,
    looks_like_clock_time,
    normalize_location,
    parse_clock_time,
)
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
            "  tc                       show current status (default)\n"
            "  tc in                    punch in at the default location, now\n"
            "  tc in office             punch in at 'office', now\n"
            "  tc in 7:30               punch in retroactively at 07:30 today\n"
            "  tc in office 7:30        punch in at 'office', 07:30 today\n"
            "  tc in home               update current session location to 'home'\n"
            "  tc out                   punch out, now\n"
            "  tc out 16:00             update last punch-out time to 16:00\n"
            "  tc out 18:30 --force     punch out at 18:30 today, override safety check\n"
            "  tc list -n 14            list the last 14 days of entries\n"
            "\n"
            " Locations: (\"OFFICE\", \"HOME\", \"ABROAD\" or \"FIELD\")"
        ),
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("-v", "--verbose", action="store_true", help="enable verbose logging")

    sub = parser.add_subparsers(dest="command", metavar="{in,out,list,status}")

    p_in = sub.add_parser("in", help="punch in")
    p_in.add_argument(
        "args",
        nargs="*",
        metavar="LOCATION|HH:MM",
        help="optional location and/or HH:MM start time, in any order",
    )
    p_in.add_argument(
        "-f", "--force", action="store_true",
        help="overwrite the open session if one already exists",
    )

    p_out = sub.add_parser("out", help="punch out")
    p_out.add_argument(
        "time",
        nargs="?",
        metavar="HH:MM",
        help="optional end time (HH:MM) to record",
    )
    p_out.add_argument(
        "-f", "--force", action="store_true",
        help="ignore the 'already punched out' check",
    )

    p_list = sub.add_parser("list", help="list recent entries")
    p_list.add_argument(
        "-n",
        "--days",
        type=int,
        default=5,
        help="number of days to show (default: 5)",
    )

    sub.add_parser("status", help="show current status (default if no command given)")

    p_break = sub.add_parser(
        "break",
        help="record today's lunch break",
        description=(
            "Record a lunch break.\n"
            "  tc break               use default start time and duration from config\n"
            "  tc break 11:00         start at 11:00, use default duration\n"
            "  tc break 11:15 11:43   exact start and end times"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_break.add_argument(
        "times",
        nargs="*",
        metavar="HH:MM",
        help="start time, or start and end time",
    )

    return parser


# ---------------------------------------------------------------- command impls


def _split_in_args(raw: List[str]) -> Tuple[Optional[str], Optional[str]]:
    """Split ``tc in`` positional arguments into ``(location, time_str)``.

    Accepted forms:
        []                          → (None, None)
        ["office"]                  → ("office", None)
        ["7:30"]                    → (None, "7:30")
        ["office", "7:30"]          → ("office", "7:30")
        ["7:30", "office"]          → ("office", "7:30")
    """
    if not raw:
        return None, None
    times = [a for a in raw if looks_like_clock_time(a)]
    others = [a for a in raw if not looks_like_clock_time(a)]
    if len(times) > 1 or len(others) > 1:
        raise TimeclockError(
            "Too many positional arguments to `tc in`. Expected at most one "
            "location and one HH:MM time."
        )
    return (others[0] if others else None, times[0] if times else None)


def _lunch_line(lunch: LunchBreak) -> str:
    """Format the in-progress lunch break shown by `tc` / `tc status`."""
    start = lunch.started_at.astimezone().strftime("%H:%M")
    if lunch.ended_at is None:
        return f"on lunch since {start}"
    end = lunch.ended_at.astimezone().strftime("%H:%M")
    minutes = lunch.duration.total_seconds() / 60
    return f"lunch: {start}–{end} ({minutes:.0f} min)"


def _cmd_status(client: TimeclockClient) -> int:
    status = client.status()
    if status.clocked_in:
        # since is tz-aware UTC; .astimezone() flips to local for display.
        when = status.since.astimezone().strftime("%a %H:%M") if status.since else "?"
        loc = f" @ {status.location.lower()}" if status.location else ""
        print(f"clocked IN since {when}{loc}")
        if status.today_total is not None:
            hours = status.today_total.total_seconds() / 3600
            print(f"running: {hours:.2f}h")
        if status.lunch is not None:
            print(_lunch_line(status.lunch))
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


def _cmd_in(client: TimeclockClient, raw_args: List[str], force: bool) -> int:
    location, time_str = _split_in_args(raw_args)
    # Validate the location string early (catches typos before we hit network).
    if location is not None:
        normalize_location(location)
    at = parse_clock_time(time_str, allow_future=force) if time_str else None
    entry = client.punch_in(location, at=at, force=force)
    loc = (entry.location or "").lower()
    when = entry.started_at.astimezone().strftime("%H:%M") if entry.started_at else "?"
    print(f"punched in @ {loc} at {when}")
    return 0


def _cmd_out(client: TimeclockClient, time_str: Optional[str], force: bool) -> int:
    at = parse_clock_time(time_str, allow_future=force) if time_str else None
    entry = client.punch_out(at=at, force=force)
    when = entry.ended_at.astimezone().strftime("%H:%M") if entry.ended_at else "?"
    hours = entry.duration.total_seconds() / 3600
    print(f"punched out at {when} — {hours:.2f}h")
    return 0


def _cmd_break(client: TimeclockClient, times: List[str], config: Config) -> int:
    if len(times) > 2:
        raise TimeclockError("Too many arguments — expected at most two HH:MM times.")
    for t in times:
        if not looks_like_clock_time(t):
            raise TimeclockError(f"Expected HH:MM, got {t!r}.")

    if len(times) == 2:
        start_dt = parse_clock_time(times[0], allow_future=False)
        end_dt = parse_clock_time(times[1], allow_future=False)
    elif len(times) == 1:
        start_dt = parse_clock_time(times[0], allow_future=False)
        end_dt = start_dt + timedelta(minutes=config.break_duration)
    else:
        start_dt = parse_clock_time(config.break_start, allow_future=False)
        end_dt = start_dt + timedelta(minutes=config.break_duration)

    if end_dt <= start_dt:
        raise TimeclockError(
            f"Break end ({end_dt.strftime('%H:%M')}) must be after start ({start_dt.strftime('%H:%M')})."
        )

    client.create_lunch(start_dt, end_dt)
    duration_min = int((end_dt - start_dt).total_seconds() / 60)
    print(
        f"break recorded {start_dt.strftime('%H:%M')}–{end_dt.strftime('%H:%M')}"
        f" ({duration_min} min)"
    )
    return 0


def _cmd_list(client: TimeclockClient, days: int) -> int:
    entries = client.list_entries(days=days)
    if not entries:
        print(f"no entries in the last {days} day(s)")
        return 0
    groups = [
        (date, list(day_entries))
        for date, day_entries in groupby(
            entries, key=lambda e: e.started_at.astimezone().date()
        )
    ]
    for i, (_date, day_entries) in enumerate(groups):
        work = [e for e in day_entries if not e.is_lunch and not e.is_absence]
        lunch = [e for e in day_entries if e.is_lunch]
        show_total = len(work) > 1 or (work and lunch)
        total_suffix = ""
        if show_total:
            total_secs = (
                sum(e.duration.total_seconds() for e in work)
                - sum(e.duration.total_seconds() for e in lunch)
            )
            total_suffix = f" {total_secs / 3600:>5.2f}h/d"
        for j, entry in enumerate(day_entries):
            line = entry.format_line()
            if show_total and j == len(day_entries) - 1:
                line += total_suffix
            print(line)
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
            "No credentials configured. Edit %s and set username/password "
            "under the [timeclock] section.",
            config.path,
        )
        return 2

    client = TimeclockClient(config)

    try:
        command = args.command or "status"
        if command == "status":
            return _cmd_status(client)
        if command == "in":
            return _cmd_in(client, args.args, args.force)
        if command == "out":
            return _cmd_out(client, args.time, args.force)
        if command == "list":
            return _cmd_list(client, args.days)
        if command == "break":
            return _cmd_break(client, args.times, config)
        parser.error(f"unknown command: {command}")  # exits with code 2
        return 2  # unreachable, satisfies the type checker
    except AuthenticationError as exc:
        log.error("Authentication failed: %s", exc)
        return 3
    except ConflictError as exc:
        log.error("%s", exc)
        return 6
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
