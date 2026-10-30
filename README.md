# Check Tracker

A small local web app for logging recurring **store audit checks**. For each
account (customer) and location (store) you record checks over time: the
metrics measured at each check, and the findings (problems) raised, with their
follow-up across later checks.

- **Accounts → locations → checks.** A check has a date, who did it, an optional
  data period, a summary and a draft/complete status.
- **Metrics:** free key/value pairs (`match_rate = 97.8 %`). A new check is
  pre-filled with the previous check's metric names and units, and the
  previous values are shown as hints.
- **Findings:** category, description, suspected cause, change made, status
  (open / fixed / verified / not resolved). Open findings from earlier checks
  show up on the next check at that location ("carry-over"). You can update
  their status there, or log them as a recurrence. Every status change is
  kept as history.
- **Compare** two checks of a location: metric changes (big ones highlighted)
  and what happened to findings in between.
- **Search findings** across all accounts, with filters and a **CSV export**
  for Excel.
- **Automatic backups:** one per day at startup, plus one before every database
  upgrade.

It runs only on your own computer (`http://127.0.0.1:8765`), with its own
database. Nothing is shared between colleagues and nothing is sent anywhere.

> [!WARNING]
> **Audit data stays on your machine.** The database, backups and CSV exports
> contain company audit data. Never put them in a cloud-synced folder:
> OneDrive, iCloud Drive (including a synced Desktop/Documents on macOS),
> Dropbox, Google Drive, Box and the like. The app refuses to start if its
> database or backup folder is in one. Don't commit data to git either:
> `.gitignore` excludes databases, backups and exports. Keep it that way.

## Requirements

- **Python 3.12 or newer** (3.12 and 3.14 are tested), from
  [python.org](https://www.python.org/downloads/). On macOS you can also use
  Homebrew (`brew install python@3.13`). The `python3` that comes with macOS is
  too old. On Windows the python.org installer also installs the `py`
  launcher, which `setup.bat` uses.
- **Git**, to clone the repository and get updates.
- Windows 10/11 or macOS. A modern browser.

## Setup

Clone the repository into a **local folder that is not synced**, e.g.
`C:\dev\` on Windows or `~/dev/` on macOS. *Not* Documents or Desktop, which
are often synced to OneDrive or iCloud.

**Windows**

```bat
cd C:\dev
git clone <repository-url> audit-log
cd audit-log
setup.bat
```

**macOS**

```sh
mkdir -p ~/dev && cd ~/dev
git clone <repository-url> audit-log
cd audit-log
./setup.sh
```

> [!NOTE]
> The macOS scripts (`setup.sh`, `start.sh`) have been tested in a POSIX shell
> on Windows but **not yet on a real Mac**. If anything doesn't work on macOS,
> please report it (what you ran and the full error message) so it can be fixed.

The setup script creates a virtual environment in `.venv`, installs the
dependencies from `requirements.txt` and creates an **empty** database. Run it
again any time; it only does what is missing.

## Starting the app

- **Windows:** double-click `start.bat` (or run it in a terminal). For a
  Desktop shortcut, right-click `start.bat` → *Show more options* → *Create
  shortcut*, and move the shortcut to your Desktop. The shortcut holds only a
  path, no data.
- **macOS:** run `./start.sh` in Terminal.

The script starts the server and opens `http://127.0.0.1:8765/` in your
browser. If the app is already running, it just opens the browser. To stop
the app, close the console window (Windows) or press Ctrl+C (macOS).

## Updating

```sh
# stop the app first, then:
git pull
setup.bat        # macOS: ./setup.sh   (installs any new requirements)
start.bat        # macOS: ./start.sh
```

If an update changes the database structure, the app upgrades your database
automatically the next time it starts. It **writes a backup first**
(`checks-<date>-<time>-before-v<N>.db`). Each upgrade step either completes
fully or leaves the database unchanged. If the app says the database is newer
than the code, you are running an older version: `git pull` again.

## Where your data is

| What | Default location | Change with |
|---|---|---|
| Database | `checks.db` in the project folder | `DB_PATH` |
| Backups | a folder next to the project: `<project>-backups`, e.g. `C:\dev\audit-log-backups` | `BACKUP_DIR`, `BACKUP_KEEP` (30) |
| CSV exports | wherever your browser saves downloads | your browser settings |

Backups are plain SQLite files. To restore one, stop the app and copy the
backup over `checks.db`. The sidebar footer shows when the last backup was
made.

Moving the project folder? Stop the app and move the whole folder, including
`checks.db`. Then delete `.venv` and run setup again, because a venv can't be
moved. The default backup folder moves with it (`<project>-backups` next to
the new location), so copy the old backups across if you want to keep them.

## Settings

Defaults are in `config.py` (tracked in git, so don't edit it). For your own
settings, copy `config_local.example.py` to `config_local.py` and uncomment
what you need. `config_local.py` is git-ignored, so `git pull` never
overwrites it. Restart the app after a change. Common ones:

- `PORT`: if 8765 is taken on your machine. The start scripts read it too.
- `DB_PATH`, `BACKUP_DIR`: other locations (local disk only).
- `CSV_DELIMITER = ";"`: if Excel opens the export as a single column.

## Command line

Use the venv's Python: `.venv\Scripts\python.exe` (Windows) or
`.venv/bin/python` (macOS).

```sh
python db.py init      # create the database / apply pending upgrades (setup does this)
python db.py check     # database path, schema version, row counts, integrity check
python db.py backup    # write a timestamped backup now
python db.py reset     # delete ALL data, keep the structure (asks you to type RESET;
                       # backs up first). Stop the app first.
python db.py init --demo   # sample data, to try the app out (empty database only)
```

## Troubleshooting

- **"Refusing to use … as the database: … looks cloud-synced"**: the project
  (or `DB_PATH` / `BACKUP_DIR`) is inside OneDrive, iCloud, Dropbox, etc. Move
  the project folder to e.g. `C:\dev` or `~/dev` (then delete `.venv` and run
  setup again).
- **The page doesn't open / "address already in use"**: another program uses
  port 8765. Set `PORT` in `config_local.py`.
- **macOS "permission denied" on `./setup.sh`**: run `chmod +x setup.sh start.sh`
  once, or use `sh setup.sh` / `sh start.sh`.
- **"Python 3.12 or newer was not found"**: install it from python.org, open a
  new terminal, and run setup again.

## For developers

FastAPI + SQLite (stdlib `sqlite3`, no ORM) + Jinja2 templates, no external
network access at runtime. CSS and JS are served from `static/`, and all
colours are in `static/theme.css`. Development server with auto-reload:

```sh
.venv/Scripts/python.exe -m uvicorn app:app --host 127.0.0.1 --port 8765 --reload
```

`CLAUDE.md` describes the data model and design decisions in detail.

### Changing the schema

Never edit a migration that has been shared. Add the next numbered file to
`migrations/` instead:

- `NNNN_short_name.sql`: plain SQL statements, or
- `NNNN_short_name.py` with a `migrate(conn)` function, for anything SQL
  alone can't do.

At startup the app applies every migration newer than the database, in order.
It backs up first, runs each migration in its own transaction, and records it
in the `schema_version` table. Numbers must be consecutive (0001, 0002, …).
If two people add the same number, the app refuses to start until one is
renumbered.

- **Add a column or index:** `ALTER TABLE … ADD COLUMN …` / `CREATE INDEX …` in a
  `.sql` migration.
- **Change a view:** `DROP VIEW IF EXISTS v_x;` then `CREATE VIEW v_x AS …`
  (drop dependent views first, then recreate them).
- **Change a CHECK constraint**, e.g. add or remove a finding category or status.
  SQLite can't alter a CHECK, so the table is rebuilt with `db.rebuild_table`,
  which keeps every row, index, trigger and view:

  ```python
  # migrations/0002_add_category_x.py
  import db

  def migrate(conn):
      db.rebuild_table(conn, "finding", """CREATE TABLE finding (
          ... the full current definition, with the new CHECK list ...
      )""")
  ```

  Copy the current definition from `sqlite_master` or the latest migration
  that defines the table. Also update the matching list in `db.py`
  (`FINDING_CATEGORIES`, `FINDING_STATUSES`, `REPORT_STATUSES`): the app checks
  at startup that `db.py` and the database agree. If a migration removes a
  category that findings still use, it fails and the database is left as it
  was.
