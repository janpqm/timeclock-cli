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
base_url         = https://live.timeclock365.com
client_id        = ZWUyYzllNGUzYjlhZDAwNzBiYTgwN2ZjYjU2YWZi…
```

Only `username` and `password` actually need editing — the other fields are pre-filled with sensible defaults.

`default_location` is one of `office`, `home`, `abroad`, `field` (the locations timeclock365 accepts). Aliases like `remote` → `home` and `work` → `office` also work on the command line.

`client_id` is the constant the web UI sends to `/api/auth/login`. It's pre-filled here so you can update it without a code change if timeclock365 ever rotates it. Leave it alone unless login starts failing.

The file is created with mode `0600` (owner read/write only) so other users on the same machine cannot read your password.

## Usage

```bash
tc                       # show current status + period totals
tc --help                # full help
tc status                # same as bare `tc`
tc in                    # punch in (uses default_location if set)
tc in office             # punch in at 'office'
tc in home               # punch in at 'home'
tc out                   # punch out
tc list                  # last 5 days of entries
tc list -n 14            # last 14 days
```

Every command logs what it is doing to stderr so you can see exactly which HTTP calls are being made. Pass `-v` or set `TIMECLOCK_LOG=DEBUG` for request-level detail.

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
