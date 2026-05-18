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

On first run `tc` will create a config file at `~/.config/timeclock-cli/config.ini` (Linux/macOS) or the equivalent on Windows. Edit it and fill in your credentials:

```ini
[auth]
username = your.email@example.com
password = your-password

[defaults]
# Optional default location for `tc in` when no location is given
location = office

[server]
# Override only if you use a self-hosted timeclock365
base_url = https://live.timeclock365.com
```

The file is created with mode `0600` (owner read/write only) so other users on the same machine cannot read your password.

## Usage

```bash
tc                       # show current status (clocked in / out, today's hours)
tc --help                # full help
tc status                # same as bare `tc`
tc in                    # punch in (uses default location if set)
tc in office             # punch in with location "office"
tc in home               # punch in with location "home"
tc out                   # punch out
tc list                  # last 5 days of entries
tc list -n 14            # last 14 days
```

Every command logs what it is doing to stderr so you can see exactly which HTTP calls are being made — set `TIMECLOCK_LOG=DEBUG` to see request/response details.

## How it works

`tc` logs in to `live.timeclock365.com` using your username/password, stores the session cookie on disk (so subsequent calls don't re-authenticate), and replays the same API requests the web UI makes for punching, listing, and status checks.

If timeclock365 rotates your session, the next `tc` invocation will silently log in again.

## Project status

Early alpha. The HTTP calls in `timeclock_cli/client.py` are placeholders pending capture of the real flow from the web UI. See `NOTES.md` for the in-progress reverse-engineering notes.

## License

MIT — see [LICENSE](LICENSE).
