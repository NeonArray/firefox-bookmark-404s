# Bookmark 404 Checker

Scans all bookmarks in Firefox's `places.sqlite`, checks each URL for a live
(HTTP 200) response, and flags dead ones by prefixing the bookmark title
with `(DEAD) `. Can also delete bookmarks already flagged this way.

## Requirements

- Python 3 (no third-party packages needed — stdlib only)
- **Firefox must be completely closed** before running (the database is
  locked while Firefox is open)

## Usage

```bash
python3 check_bookmarks.py [options]
```

A timestamped backup of `places.sqlite` is created automatically before any
changes are written.

## Flags

| Flag | Description |
|---|---|
| `--profile PATH` | Path to a specific Firefox profile directory (containing `places.sqlite`). If omitted, the default profile is auto-detected. |
| `--dry-run` | Only report what would change (dead bookmarks found, or bookmarks that would be deleted) — no writes to the database. |
| `--workers N` | Number of concurrent URL checks to run (default: `16`). |
| `--timeout SECONDS` | Per-request timeout for each URL check (default: `8`). Note: does not currently bound slow DNS resolution. |
| `--delete-dead` | Skip all URL/DNS checks and instead delete bookmarks already titled with the `(DEAD)` prefix from a previous run. |

## Examples

Preview dead bookmarks without changing anything:
```bash
python3 check_bookmarks.py --dry-run
```

Flag dead bookmarks (writes `(DEAD)` prefix to titles):
```bash
python3 check_bookmarks.py
```

Flag dead bookmarks with a longer timeout and more concurrency:
```bash
python3 check_bookmarks.py --timeout 15 --workers 32
```

Preview which already-flagged `(DEAD)` bookmarks would be deleted:
```bash
python3 check_bookmarks.py --delete-dead --dry-run
```

Delete already-flagged `(DEAD)` bookmarks:
```bash
python3 check_bookmarks.py --delete-dead
```

Use a specific profile:
```bash
python3 check_bookmarks.py --profile "/path/to/profile"
```

## Notes

- Bookmarks can appear that you don't remember adding — if Firefox Sync is
  enabled, bookmarks from any device/profile linked to your Firefox Account
  merge into this local database (often under "Mobile Bookmarks").
- Output for dead bookmarks includes their folder location and the date
  they were added, to help trace where they came from.
