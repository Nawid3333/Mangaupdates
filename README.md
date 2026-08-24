# MangaUpdates List Exporter

[![Python](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](./LICENSE)

A menu-driven Python tool for your [MangaUpdates](https://www.mangaupdates.com/) account: export all your lists (Reading, Wish, Complete, Unfinished, On Hold, and custom lists) to JSON via the official API and see what changed since last time, find related series you don't already track, or find out which of your Wish List series have finished releasing so you know what's safe to start reading.

## Features

- **Menu-driven** — run `main.py`, see your logged-in account and API reachability up front, then pick what to do
- **Option 1 — Scan my lists**: exports every list and reports what was added, removed, or moved since the last run
  - **Dynamic list discovery** — picks up custom lists, not just the 5 defaults
  - **Safe against duplicate list names** — two lists that reduce to the same filename (e.g. `Sci-Fi/Fantasy` and `Sci-Fi_Fantasy`, or `Sci-Fi` and `sci-fi` on a case-insensitive filesystem) are kept on separate files and compared correctly, tracked by a manifest written alongside each export
  - **Auto-rotation** — keeps the last 3 exports, deletes older ones
  - **Crash-safe writes** — an export is built in a temporary folder and only revealed under its final name once every file has been written, so an interrupted run can never leave a half-written folder behind for the next run to compare against
- **Option 2 — Related series**: looks up every series' MangaUpdates "Related Series" section and reports anything not already in one of your lists to `exports/related.txt`, overwritten fresh each run
- **Option 3 — Ready to read**: checks every series on your Wish List against MangaUpdates' own completion flag and reports the ones that have finished releasing (including cancelled/discontinued — nothing more is coming there either) to `exports/ready_to_read.txt`, overwritten fresh each run. A series still mid-release in any format (e.g. an ongoing webtoon re-release of an otherwise-complete print run) is correctly left out
- **Concurrent lookups** — options 2 and 3 both make one API request per series, run concurrently across `SERIES_LOOKUP_WORKERS` threads; the API doesn't charge for either endpoint, so there's no reason to do it one at a time
- **Retries transient failures**, including rate limiting (`429`), honoring the API's `Retry-After` header when it sends one
- **Resilient to unexpected data** — a single malformed API response, missing field, or oddly-shaped list item is skipped and logged, never aborting the whole batch; fuzz-tested against randomized malformed input and 250-series concurrent runs with injected failures

## Example Output

```
============================================================
  MANGAUPDATES LIST EXPORTER & TRACKER
============================================================

Checking MangaUpdates API availability...
  ✓  api.mangaupdates.com — reachable
Logging in as 'YourUsername'...
Login successful
Logged in as: YourUsername

Options:
  1. Scan my lists (export + compare with last run)
  2. Check related series not already in your lists
  3. Check Wish List for finished/cancelled series (ready to read)
  0. Exit

Enter your choice (0-3): 1
Fetching user lists...
Found 5 list(s): Reading List, Wish List, Complete List, Unfinished List, On Hold List
Exporting lists...
  Reading List: 14 item(s)
  Wish List: 7 item(s)
  Complete List: 25 item(s)
  Unfinished List: 3 item(s)
  On Hold List: 5 item(s)
Saving exports...
Exports saved to: exports\10.04.2026_18-30-05

╔════════════════════════════════════════════════════════════════╗
║  📋  Changes since last export (10.04.2026_14-30-05)             ║
╚════════════════════════════════════════════════════════════════╝
  ✎  [Reading List] 12 → 14 (+2)
     + Added:   Solo Leveling
     + Added:   One Punch Man
  ✎  [Wish List] 8 → 7 (-1)
     - Removed: The Delinquent Girl
  ✓  [Complete List] No changes — (25 items)
  ✓  [Unfinished List] No changes — (3 items)
  ✓  [On Hold List] No changes — (5 items)

╔════════════════════════════════════════════════════════════════╗
║  ⚠️  CHANGES DETECTED                                             ║
║     Review the details above                                     ║
╚════════════════════════════════════════════════════════════════╝

╔════════════════════════════════════════════════════════════════╗
║  📊 Summary: 5 list(s), 54 item(s), in 3.2s                      ║
╚════════════════════════════════════════════════════════════════╝

Options:
  1. Scan my lists (export + compare with last run)
  2. Check related series not already in your lists
  3. Check Wish List for finished/cancelled series (ready to read)
  0. Exit

Enter your choice (0-3): 0
Goodbye!
Logged out
Done!
```

## Setup

1. **Clone the repository**

   ```bash
   git clone https://github.com/Nawid3333/Mangaupdates.git
   cd Mangaupdates
   ```

2. **Install dependencies**

   ```bash
   pip install -r requirements.txt
   ```

3. **Configure credentials**

   ```bash
   cp .env.example .env
   ```

   Edit `.env` and fill in your MangaUpdates username and password:

   ```
   MU_USERNAME=your_username
   MU_PASSWORD=your_password
   ```

## Usage

```bash
python main.py
```

You'll see your logged-in account and whether the API is reachable, then a menu to pick option 1, 2, or 3 (repeatable — it returns to the menu after each one, until you choose `0`).

Option 1's exports are saved to `exports/<timestamp>/` with one JSON file per list, plus a `_manifest.json` recording which file each list was saved to. Options 2 and 3 each write a single stable, always-overwritten report: `exports/related.txt` and `exports/ready_to_read.txt`.

## Project Structure

```
├── .env.example         # Credentials template
├── .gitignore
├── LICENSE              # MIT
├── README.md            # This file
├── main.py              # Menu, login, export/compare/rotate, related-series, finished-series check
├── requirements.txt     # Python dependencies
├── ruff.toml            # Lint/format configuration
├── config/
│   └── config.py            # Paths, API settings, retry/export tuning, logging
└── tests/
    ├── __init__.py
    └── test_mangaupdates.py # Unit + regression tests
```

Directories created at runtime (`exports/`, `logs/`) and your `.env` are not part of the repository.

## Configuration

Settings can be adjusted in `config/config.py`:

| Setting                 | Default | Description                                          |
| ------------------------ | ------- | ----------------------------------------------------- |
| `MAX_EXPORTS`            | `3`     | Number of export snapshots to keep (option 1)          |
| `ITEMS_PER_PAGE`         | `100`   | Items per API page request                             |
| `MAX_RETRIES`            | `3`     | Attempts per API request before giving up               |
| `RETRY_DELAY`            | `5`     | Seconds to wait before retrying, when the server does not send a `Retry-After` header |
| `SERIES_LOOKUP_WORKERS`  | `10`    | Concurrent threads for the per-series lookups options 2 and 3 make. Raise for a faster run, lower to be gentler on a very large account |

## Tests

```bash
python -m pytest tests -q
```

Runs offline — no credentials or network access needed. Covers filename collision handling (including the case-insensitive-filesystem variant), the export manifest, crash-safety of `save_exports` (including recovering from a previous crashed run's leftover files), export rotation, the retry/rate-limit logic, related-series discovery (including a genuine wall-clock concurrency proof, not just a correctness check), the Wish List completion check (including the mixed-format trap where one release format says "Complete" while another is still active), and resilience to malformed API responses and list items.

## Requirements

- Python 3.10+ — developed and tested on 3.14. The 3.10 floor comes from a PEP 604 `X | None` annotation evaluated at runtime; earlier 3.x versions are not tested.
- A [MangaUpdates](https://www.mangaupdates.com/) account

## Author

Nawid Salehie

## Credits

Data provided by the [MangaUpdates API](https://api.mangaupdates.com/).

## License

MIT — see [LICENSE](LICENSE) for details.
