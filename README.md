# MangaUpdates List Exporter

[![Python](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](./LICENSE)

A one-click Python tool that exports all your [MangaUpdates](https://www.mangaupdates.com/) lists (Reading, Wish, Complete, Unfinished, On Hold, and custom lists) to JSON files via the official API.

## Features

- **One-click export** — run `main.py` and all lists are saved automatically
- **Dynamic list discovery** — picks up custom lists, not just the 5 defaults
- **Change tracking** — shows what was added/removed since the last export
- **Auto-rotation** — keeps the last 3 exports, deletes older ones
- **Timestamped folders** — each run creates a uniquely named snapshot

## Example Output

```
==================================================
MangaUpdates List Exporter
==================================================
Logging in as 'YourUsername'...
Login successful
Fetching user lists...
Found 5 list(s): Reading List, Wish List, Complete List, Unfinished List, On Hold List
Exporting lists...
  Reading List: 14 item(s)
  Wish List: 7 item(s)
  Complete List: 25 item(s)
  Unfinished List: 3 item(s)
  On Hold List: 5 item(s)
Saving exports...
Exports saved to: exports\2026_04_10_18-30-05

==================================================
Changes since last export (2026_04_10_14-30-05)
==================================================
  [Reading List] 12 -> 14 (+2)
    + Added: Solo Leveling
    + Added: One Punch Man
  [Wish List] 8 -> 7 (-1)
    - Removed: The Delinquent Girl
  [Complete List] 24 -> 25 (+1)
    + Added: The Delinquent Girl
  [Unfinished List] No changes (3 items)
  [On Hold List] No changes (5 items)
Logged out
Done!
```

## Setup

1. **Clone the repository**

   ```bash
   git clone https://github.com/YOUR_USERNAME/mangaupdates-list-exporter.git
   cd mangaupdates-list-exporter
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

Exports are saved to `exports/<timestamp>/` with one JSON file per list.

## Project Structure

```
├── main.py              # Main script — login, export, compare, rotate
├── config/
│   └── config.py        # Configuration — paths, API settings, logging
├── .env.example         # Credentials template
├── .env                 # Your credentials (git-ignored)
├── requirements.txt     # Python dependencies
├── exports/             # Export output (git-ignored)
│   └── 2026_04_10_18-30-05/
│       ├── Reading List.json
│       ├── Wish List.json
│       └── ...
└── logs/                # Log files (git-ignored)
```

## Configuration

Settings can be adjusted in `config/config.py`:

| Setting          | Default | Description                        |
| ---------------- | ------- | ---------------------------------- |
| `MAX_EXPORTS`    | `3`     | Number of export snapshots to keep |
| `ITEMS_PER_PAGE` | `100`   | Items per API page request         |

## Requirements

- Python 3.10+
- A [MangaUpdates](https://www.mangaupdates.com/) account

## Author

Nawid Salehie

## Credits

Data provided by the [MangaUpdates API](https://api.mangaupdates.com/).

## License

MIT
