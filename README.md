# MangaUpdates List Exporter

[![Python](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](./LICENSE)

A menu-driven Python tool for your [MangaUpdates](https://www.mangaupdates.com/) account: export all your lists (Reading, Wish, Complete, Unfinished, On Hold, and custom lists) to JSON via the official API and see what changed since last time, find related series you don't already track, or find out which of your Wish List series have finished releasing so you know what's safe to start reading.

## Features

- **Menu-driven** — run `main.py`, see your logged-in account and API reachability up front, then pick what to do
- **Option 1 — Scan my lists**: exports every list and reports what was added, removed, or moved since the last run
  - **Dynamic list discovery** — picks up custom lists, not just the 5 defaults
  - **Safe against duplicate list names** — two lists that reduce to the same filename (e.g. `Sci-Fi/Fantasy` and `Sci-Fi_Fantasy`, or `Sci-Fi` and `sci-fi` on a case-insensitive filesystem) are kept on separate files and compared correctly, tracked by a manifest written alongside each export
  - **Auto-rotation** — keeps the last 3 exports, deletes older ones
  - **Crash-safe writes** — an export is built in a temporary folder and only revealed under its final name once every file has been written, so an interrupted run can never leave a half-written folder behind for the next run to compare against
  - **Honest about a broken comparison** — if the previous export exists but can't be read, that's reported explicitly ("previous export could not be read – cannot compare") instead of being treated as an empty export, which would report every current series as newly added
- **Option 2 — Related series**: looks up every series' MangaUpdates "Related Series" section and reports anything not already in one of your lists to `exports/related.txt`, overwritten fresh each run
- **Option 3 — Ready to read**: checks every series on your Wish List against MangaUpdates' own completion flag and reports the ones that have finished releasing (including cancelled/discontinued — nothing more is coming there either) to `exports/ready_to_read.txt`, overwritten fresh each run. A series still mid-release in any format (e.g. an ongoing webtoon re-release of an otherwise-complete print run) is correctly left out. If your account has more than one list literally named "Wish List", this checks the first and warns you which one it picked rather than silently guessing
- **Concurrent lookups** — options 2 and 3 both make one API request per series, run concurrently across `SERIES_LOOKUP_WORKERS` threads; the API doesn't charge for either endpoint, so there's no reason to do it one at a time
- **Concurrent list paging** — page 1 of a list already reports `total_hits`, so there is nothing to discover by walking pages one at a time: every list's first page is fetched at once, then every remaining page at once. Same number of requests, two round trips instead of one list's pages after another's
- **Reports are reproducible, not just correct** — options 2 and 3 look up series concurrently, so results arrive in whatever order the network happens to finish them in. Both reports sort explicitly (title, then id as a tiebreak) before writing, so the same account state always produces byte-identical output, run after run — confirmed live: before this, two runs over identical data produced two differently-ordered reports
- **Retries transient failures**, including rate limiting (`429`), honoring the API's `Retry-After` header when it sends one. Each retry adds a small random delay on top, so concurrent workers that were rate-limited in the same instant don't all retry in the same instant — the wait is never shorter than the server asked for
- **Resilient to unexpected data** — a single malformed API response, missing field, or oddly-shaped list item is skipped and logged, never aborting the whole batch; fuzz-tested against randomized malformed input and 250-series concurrent runs with injected failures
- **Fails loudly where guessing would be worse** — a malformed *list page* or list index aborts the export instead of being worked around, naming the list and page. A silently shortened list would be saved as if it were complete, and the next run would report every missing series as removed
- **Survives its own failures** — one option failing doesn't end the session or lose your login; Ctrl+C stops the current operation and returns you to the menu, discarding partial data. Ctrl+C at the menu prompt itself exits the same way choosing `0` does (exit code 0); a Ctrl+C before the menu is even shown — during login or the reachability check — exits with code 130, as convention expects

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

2. **Create a virtual environment and install dependencies**

   ```bash
   python -m venv .venv
   source .venv/bin/activate        # Linux / macOS
   .venv\Scripts\Activate.ps1       # Windows (PowerShell)

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

### Install it as a command

Building a wheel puts a `mangaupdates-scraper` command on your PATH:

```bash
pip install build
python -m build
pip install dist/mangaupdates_scraper-2.0.0-py3-none-any.whl
```

Two things are worth knowing before you do.

**Give each program its own virtual environment.** This project and its siblings
ship their code as the top-level modules `main` and `config`. Install two of them
into the same environment and the second overwrites the first — the command still
exists, but it silently runs the other program. `pipx` creates an isolated
environment per application and avoids this entirely:

```bash
pipx install .
```

**Tell it where to keep your files.** Once installed, the package lives inside
`site-packages`, which is no place to keep a `.env` you have to edit by hand.
Point `MU_HOME` at a folder you own, and `.env` and the `exports/` and `logs/` folders all move there:

```bash
export MU_HOME=~/mangaupdates                # Linux / macOS
$env:MU_HOME = "$HOME\mangaupdates"          # Windows (PowerShell)

mkdir -p ~/mangaupdates
cp .env.example ~/mangaupdates/.env
```

If you skip that copy, the first run writes the template there for you and
says where it put it -- so an installed copy never leaves you hunting for a
file inside `site-packages`.

`MU_HOME` has to be a real environment variable. It cannot be set inside `.env`,
because it is what tells the program where to find that file in the first place.
Left unset it resolves to the checkout, which is why running from a clone needs
no configuration at all.

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
| `RETRY_DELAY`            | `5`     | Seconds to wait before retrying, when the server does not send a `Retry-After` header. A random spread of up to `RETRY_JITTER` (1s, in `main.py`) is added on top |
| `SERIES_LOOKUP_WORKERS`  | `16`    | Concurrent threads for the per-series lookups options 2 and 3 make. Benchmarked live against the real API; 16 is where the throughput curve flattens. Raise for a faster run, lower to be gentler on a very large account |
| `LIST_PAGE_WORKERS`      | `8`     | Concurrent threads for list page requests (all three options). Page 1 reports `total_hits`, so the whole page range is fetched in one go rather than walked |

One setting lives in the environment rather than in `config/config.py`:
`MU_HOME` decides where `.env`, `exports/` and `logs/` live. Unset, that is this
checkout. Set it when you install the package, so those do not land in
site-packages. It must be a real environment variable — it cannot go in `.env`,
because it is what locates that file.


## Tests

```bash
python -m pytest tests -q
```

Or with the standard library runner:

```bash
python -m unittest discover -s tests
```

Line coverage of `main.py` is 99% — the only uncovered line is the `sys.exit(_run_cli())` call itself, which no test can execute.

Runs offline — no credentials or network access needed. Covers filename collision handling (including the case-insensitive-filesystem variant), the export manifest, crash-safety of `save_exports` (including recovering from a previous crashed run's leftover files), export rotation, the retry/rate-limit logic, related-series discovery (including a genuine wall-clock concurrency proof, not just a correctness check), the Wish List completion check (including the mixed-format trap where one release format says "Complete" while another is still active), resilience to malformed API responses and list items, report-ordering reproducibility, and every process exit code the program can produce.

## Requirements

- **Python 3.11+** — developed and tested on 3.14. `requires-python` in
  `pyproject.toml` enforces 3.11, so pip will refuse anything older. The code
  itself uses nothing newer than a PEP 604 `X | None` annotation evaluated at
  runtime, so 3.10 would very likely work — it is simply not tested.
- A [MangaUpdates](https://www.mangaupdates.com/) account

## Author

Nawid Salehie

## Credits

Data provided by the [MangaUpdates API](https://api.mangaupdates.com/).

## License

MIT — see [LICENSE](LICENSE) for details.
