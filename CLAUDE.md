# Check Tracker

Local-only web app for logging store audit checks. Tracks recurring checks per
store location, the metrics recorded at each check, and the findings (problems)
raised, including whether a finding from a previous check is still open.
Shared as a private git repo; each user clones it and runs it with their own
database. User-facing docs: `README.md`.

Stack: **FastAPI + SQLite (stdlib `sqlite3`, no ORM) + Jinja2 templates.**
Python 3.12+, SQLite 3.39+ (`FULL OUTER JOIN`). Windows and macOS.

Personal, machine-specific notes (if any) are in `CLAUDE.local.md`
(git-ignored); read it when it exists.

## Hard constraints

- **Local only.** Bind to `127.0.0.1` exclusively. No external network calls at
  runtime, no CDN links in templates: CSS/JS are served from `static/`.
- **No cloud sync.** This is company audit data. Never create databases,
  backups, exports or scratch output in a cloud-synced folder (OneDrive,
  iCloud, Dropbox, Google Drive, ...). `db.require_local()` enforces this for
  the database and backup folder; keep that guard on any new place data is
  written.
- **No hardcoded paths.** Paths come from `config.py` (relative to the project
  folder) or `config_local.py`. Nothing user- or machine-specific in tracked
  files.
- **Use the project venv** (`.venv`) for every install and every run:
  `.venv/Scripts/python.exe` (Windows), `.venv/bin/python` (macOS). Bare
  `python` may be a Store alias or an old system Python.
- **Never commit data:** `.gitignore` excludes `checks.db*`, all `*.db`,
  backups, `*.csv` exports, `screenshots/`, `config_local.py`,
  `CLAUDE.local.md`, `.venv`, `__pycache__`. Check `git status` before every
  commit.
- **Schema changes only via a new migration** (see Migrations). Never edit a
  migration that has been shared.

## Layout

```
README.md          user docs: setup, update, data locations, schema-change how-to
CLAUDE.md
setup.bat/.sh      create .venv, pip install, db.py init (empty DB); safe to re-run
start.bat/.sh      start the server (reads PORT from config) and open the browser
                   when it answers; reuse a running server
app.py             FastAPI app: routes, SQL, host/origin guards, runs on 127.0.0.1
config.py          defaults (tracked): HOST, PORT (8765), DB_PATH, BACKUP_DIR,
                   BACKUP_KEEP, compare thresholds, SEARCH_PAGE_SIZE,
                   CSV_DELIMITER, CLOUD_FOLDER_MARKERS; then imports config_local
config_local.example.py  template for config_local.py (git-ignored overrides)
db.py              connection helper, init_db(), migration runner, rebuild_table(),
                   enum lists, cloud-folder guard, backups, reset, demo seed, CLI
migrations\        NNNN_name.sql|.py, applied in order (0001_initial.sql = schema)
requirements.txt
static\theme.css   ALL colors, fonts, sizes as CSS variables -- restyle here only
static\app.css     layout/components; references theme.css variables, no literals
static\app.js      metric editor only (add/remove rows, unit fill); forms work without it
static\daterange.js  data-period range picker on the check form (plain JS, no library)
templates\         Jinja2: base.html (sidebar shell), _macros.html, _icons.html
                   (inline SVG), accounts\, locations\, checks\, findings\, error.html
checks.db          the database (default location; never commit)
..\<project>-backups\   automatic backups (default BACKUP_DIR), newest 30 kept
```

## Commands

```bash
# Windows: .venv/Scripts/python.exe   macOS: .venv/bin/python
python db.py init           # create DB / apply pending migrations (app start does this too)
python db.py init --demo    # ... plus demo rows (only into an empty database)
python db.py check          # paths, schema version, objects, row counts, integrity
python db.py backup         # timestamped backup now, to config.BACKUP_DIR
python db.py reset          # delete ALL data, keep schema (type RESET; backs up first).
                            # Stop the app first.
python -m pip install -r requirements.txt

start.bat / ./start.sh      # everyday use: server + browser
python app.py               # server only (host/port from config)
python -m uvicorn app:app --host 127.0.0.1 --port 8765 --reload   # development
```

## Configuration

`config.py` holds every default and ends with `from config_local import *`
(if the file exists), then resolves `DB_PATH` / `BACKUP_DIR` relative to the
project folder. New settings go in `config.py` with a default, so they reach
existing installs after a `git pull`; `config_local.example.py` documents the
common overrides. Defaults: `DB_PATH = checks.db` in the project,
`BACKUP_DIR = ../<project folder name>-backups`.

## Migrations

`db.init_db()` runs at every app start (and from `db.py init` / setup):
1. refuses SQLite < 3.39 and cloud-synced `DB_PATH` / `BACKUP_DIR`;
2. finds `migrations/NNNN_name.sql|.py` (consecutive from 0001, else error);
3. refuses a database whose `schema_version` is newer than the code;
4. if any are pending and the DB already has a schema: `backup_db(daily=False,
   label="before-vNNNN")` (a failed backup stops everything);
5. applies each pending migration in its own `BEGIN IMMEDIATE` transaction with
   foreign keys off, runs `foreign_key_check`, inserts the `schema_version`
   row, commits (on error: rollback, raise, DB unchanged by that migration);
6. `check_enum_constraints()`: the `db.py` lists must equal the CHECK lists in
   the live schema (`ENUM_CONSTRAINTS`), else refuse to start.

`.sql` migrations are split into statements by `split_sql()`
(`sqlite3.complete_statement`, trigger bodies stay whole). `.py` migrations
define `migrate(conn)` and run inside the runner's transaction.

`0001_initial.sql` is the original schema and is **idempotent** (`IF NOT
EXISTS`, `DROP VIEW IF EXISTS`): databases created before versioning existed
have no `schema_version` table and run it as their baseline. Keep it that way.

**Changing a CHECK** (finding categories/statuses, report statuses): a `.py`
migration calling `db.rebuild_table(conn, "finding", "<full new CREATE
TABLE>")`. It saves the table's indexes/triggers and all views, drops the
views, creates `<table>__new`, copies shared columns by name, drops the old
table, renames, verifies the row count and recreates indexes, triggers and
views. A row violating the new CHECK aborts the migration. Update the list in
`db.py` in the same change.

**Changing a view:** a `.sql` migration with `DROP VIEW IF EXISTS` (dependents
first) and `CREATE VIEW`.

## Data model

```
account ──< location ──< check_report ──< metric
                                      └──< finding ──< finding_status_event
                                             └── parent_finding_id (self-ref)
schema_version   one row per applied migration (version, name, applied_at)
```

- `account` — name unique case-insensitively, `archived` flag.
- `location` — belongs to an account; `code` holds the store number; name unique
  per account.
- `check_report` — one check of one location. `checked_on` is a `YYYY-MM-DD`
  date (GLOB-validated), with an optional `period_start`/`period_end` window for
  the data range the check covers. Status `draft` | `complete`.
- `metric` — flexible key/value per report.
- `finding` — category `llm_scoring_error` | `config_error` | `imbalance` |
  `conversion_anomaly` | `other`; status `open` | `fixed` | `verified` |
  `not_resolved`; fields `description`, `suspected_cause`, `change_made`;
  optional `parent_finding_id` linking to an earlier finding.
- `finding_status_event` — audit trail of every status change, recording which
  check (`report_id`) the change was logged during.

Categories and statuses are CHECK constraints in the schema **and** lists in
`db.py` (`FINDING_CATEGORIES`, `FINDING_STATUSES`, `REPORT_STATUSES`). The
templates render dropdowns from `db.py`; startup verifies the two agree.

### Views

- `v_finding_full` — finding joined up to location and account; backs the global
  search/filter screen.
- `v_open_finding` — **the single definition of "needs attention"**: status in
  `open`/`not_resolved` and not superseded by a carry-over child. Every open
  count (account, location, per check) and the carry-over panel read it. Its
  status list mirrors `db.OPEN_FINDING_STATUSES`; change both together.
- `v_check_report_full` — report with location/account, counts, and
  `prev_report_id` (the preceding check at the same location, `NULL` for the
  first); backs the compare view in one lookup. `open_finding_count` counts
  this check's rows in `v_open_finding`.

## Schema decisions

**Carry-over default: update in place.** When starting a new check, the form
shows findings still needing attention from previous checks at that location.
Changing one updates the *existing* `finding` row and appends a
`finding_status_event` tagged with the new check's `report_id`, so history is
preserved without duplicating the finding. A secondary **"log as recurrence"**
action instead creates a new finding in the current report with
`parent_finding_id` pointing at the old one; use it when the problem came back
or needs separate follow-up rather than a status change.

**`not_resolved` counts as still-open.** Carry-over queries use
`v_open_finding` / `db.OPEN_FINDING_STATUSES = ("open", "not_resolved")`, not
`status = 'open'`. `fixed` and `verified` are the closed states.

**Show only the newest link in a carry-over chain.** A finding that has been
superseded by a child (`EXISTS (SELECT 1 FROM finding c WHERE
c.parent_finding_id = f.id)`) is excluded from the carry-over panel, so a chain
appears once. Walk a full chain with a recursive CTE over `parent_finding_id`.
A trigger rejects a `parent_finding_id` pointing at another location's finding.

**Metrics match across checks by `lower(key)`.** The unique index is
`(report_id, lower(key))` and the compare view joins on `lower(key)`, so
`Match_Rate` and `match_rate` are the same metric rather than two rows.

**Metric values are stored twice.** `value_text` keeps exactly what was typed
(so `"yes"` or `"98.5%"` survive); `value_num` holds the numeric reading from
`db.parse_number()` when one exists, which is what the compare view subtracts
for deltas. Non-numeric metrics simply show no delta.

**Timestamps** are UTC ISO-8601 text via `CURRENT_TIMESTAMP` (second
resolution); `updated_at` is maintained by per-table triggers. Order events by
`id`, not timestamp, when ties matter.

## Features (how they work)

**App skeleton and hierarchy.** Account and location create/edit/archive (no
hard delete), check list per location.
- `TrustedHostMiddleware` (127.0.0.1/localhost only) + an Origin check on
  POSTs guard against DNS rebinding / cross-site posts. `/docs` is disabled
  because Swagger UI loads from a CDN.
- sqlite3 connections are opened inside each handler (`open_db()`), not via a
  FastAPI dependency: sync deps and handlers can run on different threads.
  Multi-statement writes use `transaction(conn)` (connections are autocommit).
- Theme: dark navy sidebar, white rounded cards, segmented Active/All switch,
  uppercase grey table headers. No logos or brand names. Restyle via
  `static/theme.css` only.

**The check form.**
- `/locations/{id}/checks/new` and `/checks/{id}/edit`: check fields plus a
  metric table. A new check is pre-filled with the previous check's metric
  keys/units (values empty, previous value shown as placeholder) and the
  previous `checked_by`. Rows without a value are not saved. Saving replaces
  the report's metric rows in one transaction.
- Metric key autocomplete: native `<datalist>` from `metric_suggestions()`:
  keys used at this location, grouped by `lower(key)`, latest spelling/unit
  winning (SQLite bare columns with `MAX()`). `app.js` fills the unit from
  the same data when a known key is picked.
- `/checks/{id}`: details, carry-over panel ("From earlier checks": open
  findings from earlier checks at this location, plus any earlier finding
  already updated during this check), metrics, findings in this check, and
  an "Add finding" form. Carry-over row: quick status + note (updates in
  place, event tagged with this check; a note with unchanged status is still
  logged), Edit, and "Log as recurrence" (`?recur=<id>` pre-fills the add
  form with `parent_finding_id`; the parent's status is left unchanged).
- `/checks/{rid}/findings/{fid}/edit`: edit any finding at the location in
  the context of check `rid`; shows the carry-over chain (recursive CTE) and
  status history.
- Deletes: only draft checks, and only findings from the check being viewed.
- Data period: one range field (`static/daterange.js`, popover markup in a
  `<template>` in `checks/form.html`, colors `--color-range-*` in
  `theme.css`). A popup with two months side by side (stacked below 640px),
  each with its own < > (pushing one into the other moves both);
  Monday-first 6-week grids, neighbouring-month days grey, days after today
  disabled and struck through. First click = start, second = end (swapped if
  earlier), hover previews the band. Footer presets Yesterday / Last 7 days /
  Last 30 days / Last month count back from the check date (today if none or
  if it is in the future); OK closes (one click then OK = a one-day range);
  Esc or clicking outside close and drop a half-finished pick. The field shows
  dd/mm/yyyy – dd/mm/yyyy with an × to clear. It still posts
  `period_start`/`period_end` as YYYY-MM-DD: the page renders the two native
  date inputs and the script turns them into hidden inputs, so without JS (or
  if the script fails or a saved value can't be parsed) the two date inputs
  are used.
- Static files are linked with `static_url()` (`/static/x?v=<mtime>`), so a
  changed CSS/JS file always reaches the browser instead of a cached copy.

**Compare view.** `/locations/{id}/compare?before=&after=[&changes=1]`.
Defaults: newest check vs. the one before it; with one side given, the other
is its neighbour. If both are picked in the wrong order they are swapped (with
a notice). Entry points: "Compare with previous" on the check page (hidden for
a location's first check) and "Compare checks" on the location page.
- Metrics: `FULL OUTER JOIN` of the two reports' metrics on `lower(key)`
  (`compare_metrics()` / `metric_change()`). Kind new | missing | changed |
  same. Change and % change need both `value_num`s and the same unit
  (case-insensitive); otherwise "Unit changed" / "Changed" without numbers.
  `%` metrics (unit `%` or a value ending in `%`, either side) show the
  change in percentage points ("pp") and are highlighted when |pp change| >=
  `config.CHANGE_HIGHLIGHT_PP` (3.0). Other numeric metrics are highlighted
  when |% change| >= `config.CHANGE_HIGHLIGHT_PERCENT` (10), or when the old
  value is 0 and the new one isn't ("from 0").
- Findings (`compare_findings()`): status events logged during the checks
  after `before` up to and including `after`, per finding by event `id`,
  bucketed by net change: opened, logged as recurrence, closed
  (→ fixed/verified), marked not resolved, reopened, other updates. The full
  status path is shown.
- Colors for highlighted / new / missing rows: `--color-compare-*` in
  `theme.css`.

**Search, export, backups, reset.**
- `/findings` (sidebar "Search findings"): query-string filters `q`,
  `account`, `location`, `category`, `status` (a finding status, or
  `needs_attention` = `v_open_finding`), `date_from`/`date_to` (on the check
  date), `page` (`config.SEARCH_PAGE_SIZE`). Over `v_finding_full`, newest
  check first. Archived accounts/locations are included and labelled.
- Free text: every term must match description, suspected cause or change
  made; `"quoted phrases"` stay whole. Matching uses a `fold()` SQL function
  registered in `open_db()` (casefold + strip accents + dotless ı -> i), so
  `isik` / `IŞIK` / `ışık` match each other and `%` / `_` are literal. Not
  `LIKE`, which only folds ASCII. The `highlight` filter marks matches using
  the same folding.
- `/findings.csv`: same filters, all matches (no paging), labels instead of
  codes, UTF-8 with BOM for Excel, `config.CSV_DELIMITER`. Cells starting
  with `= + - @` (that aren't plain numbers) get a leading `'` so Excel
  doesn't run them as formulas.
- Backups: app startup (lifespan), after migrations, calls `db.backup_db()`:
  SQLite online backup API to a `.tmp`, then rename; one `checks-YYYY-MM-DD.db`
  per day (skipped if today's exists, so `--reload` restarts don't churn);
  newest `BACKUP_KEEP` kept by mtime (pre-migration and manual backups count
  too). A failed daily backup is logged and shown in the sidebar footer but
  doesn't stop the app; the footer shows "Last backup ...".
- `db.py reset`: shows row counts, requires typing `RESET` (EOF or other
  input cancels), writes a timestamped backup, deletes all rows child-first,
  then `VACUUM`s so deleted rows don't linger in the file.

**Cloud-folder guard** (`db.cloud_folder_reason()`): a path is refused when a
folder in it is named one of `config.CLOUD_FOLDER_MARKERS` or starts with one
followed by space/`-`/`_`/`(` (so "OneDrive - Company" and "Dropbox
(Personal)" match but "sandbox" doesn't); when it is under a folder named by
the `OneDrive*` environment variables; or under `~/Desktop` / `~/Documents`
when iCloud syncs them (`~/Library/Mobile Documents/com~apple~CloudDocs/
Desktop|Documents` exists).

**Launchers.** `start.bat` reads `config.PORT` via the venv Python, checks
`/accounts` with `curl.exe` (built into Windows 10+). If the server is already
up it only opens the browser; otherwise it starts a background copy of itself
(`:open-when-ready`, inheriting `URL`) that polls and opens the browser, and
runs `app.py` in the window. `start.sh` does the same with `curl` and
`open`/`xdg-open`. Both `.sh` scripts also accept a Windows-style venv
(`.venv/Scripts`) so they can be tested in Git Bash. `.gitattributes` keeps
`*.bat` CRLF and `*.sh` LF; the `.sh` files are committed executable.
