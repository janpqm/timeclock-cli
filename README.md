# timeclock-cli

A small command-line client for [timeclock365.com](https://live.timeclock365.com/). Punch in, punch out, set your location, and check recent entries without leaving the terminal.

## Install

```bash
pipx install .
```

Once published, you can install directly:

```bash
pipx install timeclock-cli
```

This exposes a `tc` command on your `$PATH`.

## Configure

On first run `tc` will create a config file at `~/.config/timeclock-cli/config.ini` (Linux/macOS) or the equivalent on Windows. Open it and fill in your credentials:

```ini
[timeclock]
username         = you@yourcompany.com
password         = your-password
default_location = office
force_overwrite  = false
list_recent_last = true
break_start      = 12:00
break_duration   = 30
set_start        = 09:00
set_end          = 17:00
base_url         = https://live.timeclock365.com
client_id        = ZWUyYzllNGUzYjlhZDAwNzBiYTgwN2ZjYjU2YWZi…
```

Only `username` and `password` actually need editing — everything else is pre-filled with sensible defaults.

| Key                | Default                         | Meaning                                                                                                                                                |
|--------------------|---------------------------------|--------------------------------------------------------------------------------------------------------------------------------------------------------|
| `username`         | —                               | timeclock365 login                                                                                                                                     |
| `password`         | —                               | timeclock365 password                                                                                                                                  |
| `default_location` | `office`                        | used by `tc in` when no location is given. One of `office`, `home`, `abroad`, `field`. Aliases like `remote` → `home` and `work` → `office` also work. |
| `force_overwrite`  | `false`                         | always overwrite the open session, even without `--force`                                                                                              |
| `list_recent_last` | `true`                          | `tc list` order: `true` = oldest first / newest last (tail-style), `false` = newest first                                                              |
| `break_start`      | `12:00`                         | default lunch-break start for `tc break` and `tc set -b`                                                                                               |
| `break_duration`   | `30`                            | default lunch-break length in minutes for `tc break` and `tc set -b`                                                                                   |
| `set_start`        | `09:00`                         | default day start for `tc set` when no times are given                                                                                                 |
| `set_end`          | `17:00`                         | default day end for `tc set` when no times are given                                                                                                   |
| `base_url`         | `https://live.timeclock365.com` | override only if you have a self-hosted instance                                                                                                       |
| `client_id`        | (the stable web-UI value)       | rotate here without a code change if timeclock365 ever changes it                                                                                      |

The file is created with mode `0600` (owner read/write only) so other users on the same machine cannot read your password.

## Usage

```bash
tc                          # show current status + period totals
tc --help                   # full help
tc status                   # same as bare `tc`
tc in                       # punch in at default_location, right now
tc in office                # punch in at 'office'
tc in 7:30                  # punch in retroactively, today at 07:30
tc in office 7:30           # both — location and a manual time
tc out                      # punch out, right now
tc out 18:30                # punch out at 18:30 today
tc out 18:30 --force        # … and ignore any "you're already out" check
tc break                    # record today's lunch using config defaults
tc break 11:00              # start at 11:00, default duration
tc break 11:15 11:43        # exact start and end
tc set                      # set today using all defaults, no break
tc set -b                   # set today using all defaults, with default break
tc set 2026-06-18           # set a specific date, defaults, no break
tc set 2026-06-18 office 7:00 16:00          # custom location and times
tc set 2026-06-18 7:00 16:00 -b              # with default break
tc set 2026-06-18 7:00 16:00 -b 11:30 12:30  # with explicit break
tc set 2026-06-18 office 7:00 12:00 13:00 16:00  # two sessions in one day
tc list                     # last 5 days, oldest first (newest at the bottom)
tc list -n 14               # last 14 days
```

### `tc set` and `tc break`

`tc break` records a lunch break on the current session. With no args it uses `break_start` + `break_duration`; one time overrides the start; two times set start and end exactly.

`tc set` writes a full day's punch record. The date is optional (defaults to today). Location defaults to `default_location`, work times to `set_start`/`set_end`. Pass start/end times in pairs for multiple sessions. `-b` adds a break: bare `-b` uses `break_start`/`break_duration`, or pass two `HH:MM` values for an explicit break.

`tc set` refuses to touch a day that already has sessions or absences — clear those in the web UI first. Any time in the future produces a warning.

By default, the CLI is quiet — only warnings and errors land on stderr. Pass `-v` for INFO-level progress messages (login, network calls), or set `TIMECLOCK_LOG=DEBUG` for full request/response detail.

### `--force`

`tc in HH:MM` and `tc out HH:MM` check the current punch state first and refuse to clobber it. Pass `-f` / `--force` to override:

```bash
tc in 7:30           # refuses if you're already clocked in
tc in 7:30 --force   # sends the request anyway
```

Set `force_overwrite = true` in `config.ini` to make that the default behaviour.

The requested time must be earlier than the current wall clock — `tc in 18:00` at 09:00 is refused unless `--force` is passed.

> Note: retroactive punches (`tc in HH:MM` / `tc out HH:MM`) are best-effort against an undocumented field on the GraphQL `InputWebPunch` type. The server may reject them — if so, the error is surfaced verbatim and we'll need a HAR capture of editing a punch in the web UI to wire up the right mutation. See [NOTES.md](NOTES.md).

## How it works

`tc` authenticates against `live.timeclock365.com` in two steps (`/api/auth/login` then `/api/auth/check`) and stores the resulting `access-token` in `~/.config/timeclock-cli/session.json`. Subsequent commands reuse the token until it expires (~10 minutes), then silently re-login.

Once authenticated, the CLI talks to the same GraphQL endpoint the web UI uses:

- `DashboardWebPunchQuery` for current status
- `makePunchByDashboard` for punch in / out
- `DetailedAttendanceReport` for `tc list` and the period summary shown by `tc status`

See [NOTES.md](NOTES.md) for the captured request shapes and open questions.

## Development

Clone, set up a virtualenv, install in editable mode with the test extras, then run the suite:

```bash
git clone https://github.com/jan-henrik-preuss/timeclock-cli
cd timeclock-cli
python -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'
pytest
```

The tests are offline-only — they exercise config parsing, location normalization, the date-window helper, and `Entry` formatting. The HTTP layer is reviewed against the HAR captures in `flows/` (not checked in by default).

## License

MIT — see [LICENSE](LICENSE).
