"""SQLite access layer for Regular Check Tracker: schema migrations, backups, CLI.

Run with the project venv (.venv/Scripts/python.exe on Windows,
.venv/bin/python on macOS):

    db.py init            # create the database / apply pending migrations
    db.py init --demo     # ... and insert a small demo dataset (empty database only)
    db.py check           # schema version, tables/views, row counts, integrity
    db.py backup          # timestamped backup now (also automatic, daily, at app start)
    db.py reset           # delete ALL data (asks to confirm; backs up first)

Schema changes are numbered files in migrations/ (NNNN_short_name.sql or .py),
applied in order, each once, by init_db() -- which the app runs at every start.
The applied versions are recorded in the schema_version table.
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import re
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import config

BASE_DIR = Path(__file__).resolve().parent
MIGRATIONS_DIR = BASE_DIR / "migrations"
DB_PATH = config.DB_PATH

# FULL OUTER JOIN (compare view) needs SQLite 3.39.
MIN_SQLITE = (3, 39, 0)

# Single source of truth for the dropdowns the templates render. Each list is
# also a CHECK constraint in the schema; check_enum_constraints() verifies at
# startup that the two agree. To change one, see "Changing the schema" in README.
FINDING_CATEGORIES: tuple[tuple[str, str], ...] = (
    ("llm_scoring_error", "LLM scoring error"),
    ("config_error", "Config error"),
    ("imbalance", "Imbalance"),
    ("conversion_anomaly", "Conversion anomaly"),
    ("other", "Other"),
)

FINDING_STATUSES: tuple[tuple[str, str], ...] = (
    ("open", "Open"),
    ("fixed", "Fixed"),
    ("verified", "Verified"),
    ("not_resolved", "Not resolved"),
)

REPORT_STATUSES: tuple[tuple[str, str], ...] = (
    ("draft", "Draft"),
    ("complete", "Complete"),
)

# Statuses that still need attention and are carried into the next check.
# Mirrored in the v_open_finding view -- change both together.
OPEN_FINDING_STATUSES: tuple[str, ...] = ("open", "not_resolved")

CATEGORY_LABELS = dict(FINDING_CATEGORIES)
STATUS_LABELS = dict(FINDING_STATUSES)

# (table, column, allowed values) that must match the schema's CHECKs.
ENUM_CONSTRAINTS = (
    ("check_report", "status", REPORT_STATUSES),
    ("finding", "category", FINDING_CATEGORIES),
    ("finding", "status", FINDING_STATUSES),
    ("finding_status_event", "to_status", FINDING_STATUSES),
)


def connect(db_path: Path | str | None = None) -> sqlite3.Connection:
    """Open a connection with the settings this app relies on."""
    conn = sqlite3.connect(db_path or DB_PATH, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def init_db(db_path: Path | str | None = None) -> list[str]:
    """Make the database ready for this version of the code: refuse cloud
    folders and old SQLite builds, back up and apply pending migrations, then
    check the dropdown lists against the schema. Safe to run at every start.
    Returns the names of the migrations applied (empty if none were pending)."""
    if sqlite3.sqlite_version_info < MIN_SQLITE:
        raise RuntimeError(
            f"SQLite {sqlite3.sqlite_version} is too old; this app needs "
            f"{'.'.join(map(str, MIN_SQLITE))}+. Install Python 3.12+ from python.org "
            f"and run setup again.")
    path = Path(db_path or DB_PATH)
    require_local(path, "database")
    require_local(config.BACKUP_DIR, "backup folder")
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(path)
    try:
        applied = migrate(conn, path)
        check_enum_constraints(conn)
    finally:
        conn.close()
    return applied


# -------------------------------------------------------------- migrations

@dataclass(frozen=True)
class Migration:
    version: int
    path: Path

    @property
    def name(self) -> str:
        return self.path.stem


MIGRATION_NAME_RE = re.compile(r"(\d{4})_[a-z0-9_]+\.(sql|py)")


def find_migrations() -> list[Migration]:
    """migrations/NNNN_name.sql|.py in version order. A gap or a duplicate
    number (two people adding the same next number) is an error."""
    found = []
    for f in MIGRATIONS_DIR.iterdir():
        if f.name.startswith(("_", ".")) or f.suffix not in (".sql", ".py"):
            continue
        m = MIGRATION_NAME_RE.fullmatch(f.name)
        if not m:
            raise RuntimeError(f"Bad migration file name {f.name}: use NNNN_short_name.sql or .py")
        found.append(Migration(int(m.group(1)), f))
    found.sort(key=lambda m: m.version)
    for expected, m in enumerate(found, start=1):
        if m.version != expected:
            raise RuntimeError(f"Migrations must be numbered 0001, 0002, ... without gaps or "
                               f"duplicates; found {m.path.name} where {expected:04d} was expected")
    return found


def schema_version(conn: sqlite3.Connection) -> int:
    """Highest applied migration; 0 for a new (or pre-versioning) database."""
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' "
                        "AND name = 'schema_version'").fetchone():
        return 0
    return conn.execute("SELECT COALESCE(MAX(version), 0) FROM schema_version").fetchone()[0]


def migrate(conn: sqlite3.Connection, db_path: Path) -> list[str]:
    """Apply every migration newer than the database, backing the file up
    first when it already holds a schema (a failed backup stops everything).
    Each migration commits on its own, so a failure leaves the database at the
    last version that applied cleanly."""
    migrations = find_migrations()
    current = schema_version(conn)
    latest = migrations[-1].version if migrations else 0
    if current > latest:
        raise RuntimeError(
            f"The database is at schema version {current}, but this code only knows "
            f"up to {latest}. Update the code (git pull) before starting the app.")
    pending = [m for m in migrations if m.version > current]
    if not pending:
        return []
    if conn.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()[0]:
        backup_db(db_path, daily=False, label=f"before-v{pending[-1].version:04d}")
    for m in pending:
        apply_migration(conn, m)
    return [m.name for m in pending]


def apply_migration(conn: sqlite3.Connection, migration: Migration) -> None:
    """Run one migration and record it in schema_version, in one transaction.

    Foreign keys are off while it runs (SQLite's table-rebuild procedure
    requires it, and the pragma is a no-op inside a transaction), and
    foreign_key_check must come back clean before the commit."""
    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute("CREATE TABLE IF NOT EXISTS schema_version ("
                         " version    INTEGER PRIMARY KEY,"
                         " name       TEXT NOT NULL,"
                         " applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)")
            if migration.path.suffix == ".sql":
                for statement in split_sql(migration.path.read_text(encoding="utf-8")):
                    conn.execute(statement)
            else:
                spec = importlib.util.spec_from_file_location(
                    f"migration_{migration.version:04d}", migration.path)
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                module.migrate(conn)
            problems = conn.execute("PRAGMA foreign_key_check").fetchall()
            if problems:
                raise RuntimeError(f"foreign key problems: {[tuple(p) for p in problems]}")
            conn.execute("INSERT INTO schema_version (version, name) VALUES (?, ?)",
                         (migration.version, migration.name))
        except BaseException as exc:
            conn.execute("ROLLBACK")
            raise RuntimeError(f"Migration {migration.name} failed and was rolled back; "
                               f"the database is unchanged by it: {exc}") from exc
        conn.execute("COMMIT")
    finally:
        conn.execute("PRAGMA foreign_keys = ON")


def split_sql(text: str) -> list[str]:
    """SQL script split into complete statements (trigger bodies stay whole)."""
    statements, buf = [], ""
    for line in text.splitlines(keepends=True):
        if not buf.strip() and (not line.strip() or line.lstrip().startswith("--")):
            continue  # comments/blank lines between statements
        buf += line
        if sqlite3.complete_statement(buf):
            statements.append(buf.strip())
            buf = ""
    if buf.strip():
        raise RuntimeError(f"Incomplete SQL statement at the end of the script: {buf.strip()[:80]}")
    return statements


def rebuild_table(conn: sqlite3.Connection, table: str, create_sql: str) -> None:
    """Replace `table` with the definition in `create_sql` ("CREATE TABLE
    <table> (...)"), keeping every row (ids, timestamps, links), the table's
    indexes and triggers, and all views. This is how a migration changes a
    CHECK or other column constraint, which SQLite cannot alter in place.

    Call it only from a .py migration: the runner provides the transaction and
    turns foreign keys off. Columns are copied by name (a column missing from
    the new definition is dropped). A row that breaks the new constraints
    aborts the migration with nothing changed."""
    saved = conn.execute(
        "SELECT type, name, sql FROM sqlite_master WHERE sql IS NOT NULL AND "
        "((type IN ('index', 'trigger') AND tbl_name = ?) OR type = 'view') ORDER BY rowid",
        (table,)).fetchall()
    for view in (r for r in saved if r["type"] == "view"):  # RENAME re-checks every view
        conn.execute(f'DROP VIEW "{view["name"]}"')
    head = re.match(rf'\s*CREATE\s+TABLE\s+(IF\s+NOT\s+EXISTS\s+)?"?{re.escape(table)}"?\s*\(',
                    create_sql, re.IGNORECASE)
    if not head:
        raise ValueError(f"create_sql must start with CREATE TABLE {table} (")
    new = f"{table}__new"
    conn.execute(f'CREATE TABLE "{new}" (' + create_sql[head.end():])
    old_cols = {r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')}
    columns = ", ".join(f'"{r[1]}"' for r in conn.execute(f'PRAGMA table_info("{new}")')
                        if r[1] in old_cols)
    before = conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
    conn.execute(f'INSERT INTO "{new}" ({columns}) SELECT {columns} FROM "{table}"')
    conn.execute(f'DROP TABLE "{table}"')  # also drops its indexes and triggers
    conn.execute(f'ALTER TABLE "{new}" RENAME TO "{table}"')
    after = conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
    if after != before:
        raise RuntimeError(f"rebuild of {table} lost rows: {before} -> {after}")
    for kind in ("index", "trigger", "view"):
        for row in (r for r in saved if r["type"] == kind):
            conn.execute(row["sql"])


def check_enum_constraints(conn: sqlite3.Connection) -> None:
    """Refuse to start when a dropdown list in this file and the matching
    CHECK in the database disagree (e.g. a category added here without a
    migration), instead of failing later on a save."""
    problems = []
    for table, column, choices in ENUM_CONSTRAINTS:
        row = conn.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
                           (table,)).fetchone()
        found = re.search(rf"\b{column}\s+IN\s*\(([^)]*)\)", row[0]) if row else None
        in_db = set(re.findall(r"'([^']+)'", found.group(1))) if found else set()
        in_code = {key for key, _ in choices}
        if in_db != in_code:
            problems.append(f"{table}.{column}: db.py has {sorted(in_code)}, "
                            f"the database has {sorted(in_db)}")
    if problems:
        raise RuntimeError("db.py and the database schema disagree; add a migration for "
                           "the change (see README):\n  " + "\n  ".join(problems))


def parse_number(value: str) -> float | None:
    """Return the numeric reading of a metric value, or None if it isn't one.

    Accepts thousands separators, percent signs and a few common units so that
    '1,234', '98.5%' and '12 h' still compare numerically.
    """
    if value is None:
        return None
    text = value.strip().replace(",", "").replace("%", "").replace(" ", "")
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


# ------------------------------------------------------------ local folders

class CloudFolderError(RuntimeError):
    pass


def _cloud_marker(name: str) -> bool:
    name = name.casefold()
    return any(name == m or (name.startswith(m) and name[len(m)] in " -_(")
               for m in config.CLOUD_FOLDER_MARKERS)


def _inside(path: Path, root: Path) -> bool:
    parts = [p.casefold() for p in path.parts]
    root_parts = [p.casefold() for p in root.parts]
    return parts[:len(root_parts)] == root_parts


def cloud_folder_reason(path: Path | str) -> str | None:
    """Why `path` looks like a cloud-synced location, or None if it doesn't.
    Checks folder names (config.CLOUD_FOLDER_MARKERS), the OneDrive*
    environment variables (Windows) and iCloud's Desktop & Documents sync
    (macOS). A heuristic, and deliberately strict: audit data stays local."""
    resolved = Path(path).expanduser().resolve()
    for part in resolved.parts[1:]:
        if _cloud_marker(part):
            return f'the folder "{part}" looks cloud-synced'
    for var in ("OneDrive", "OneDriveCommercial", "OneDriveConsumer"):
        root = os.environ.get(var)
        if root and _inside(resolved, Path(root).resolve()):
            return f"it is inside OneDrive ({root})"
    home = Path.home()
    icloud = home / "Library" / "Mobile Documents" / "com~apple~CloudDocs"
    for name in ("Desktop", "Documents"):
        if (icloud / name).is_dir() and _inside(resolved, (home / name).resolve()):
            return f"iCloud syncs your {name} folder"
    return None


def require_local(path: Path | str, what: str) -> None:
    reason = cloud_folder_reason(path)
    if reason:
        raise CloudFolderError(
            f"Refusing to use {Path(path)} as the {what}: {reason}. Audit data must stay "
            f"on this machine. Move the project folder to a local folder that is not "
            f"synced (e.g. C:\\dev on Windows, ~/dev on macOS), or point DB_PATH / "
            f"BACKUP_DIR in config_local.py at one.")


# ----------------------------------------------------------------- backups

def backup_db(src_path: Path | str | None = None, dest_dir: Path | str | None = None,
              keep: int | None = None, *, daily: bool = True, label: str = "") -> Path | None:
    """Copy the database with SQLite's online backup API, then prune old copies.

    Safe while the app has the database open: the backup API copies a
    consistent snapshot, including committed WAL pages. The copy is written to
    a .tmp file and renamed, so a half-written backup never has a .db name.

    daily=True gives one file per day (checks-YYYY-MM-DD.db) and does nothing if
    today's already exists, so --reload restarts don't churn the rotation.
    daily=False gives a timestamped file (manual backups, before a reset or a
    migration), with "-<label>" appended when given.
    Returns the new file, or None when today's daily backup already existed.
    """
    src = Path(src_path or DB_PATH)
    dest_dir = Path(dest_dir or config.BACKUP_DIR)
    keep = config.BACKUP_KEEP if keep is None else keep
    require_local(dest_dir, "backup folder")
    if not src.exists():
        raise FileNotFoundError(f"No database to back up at {src}")
    dest_dir.mkdir(parents=True, exist_ok=True)
    now = datetime.now()
    suffix = f"-{label}" if label else ""
    target = dest_dir / (f"checks-{now:%Y-%m-%d}.db" if daily
                         else f"checks-{now:%Y-%m-%d-%H%M%S}{suffix}.db")
    if daily and target.exists():
        return None
    tmp = target.with_name(target.name + ".tmp")
    source = sqlite3.connect(src)
    try:
        dest = sqlite3.connect(tmp)
        try:
            source.backup(dest)
        finally:
            dest.close()
    finally:
        source.close()
    os.replace(tmp, target)
    prune_backups(dest_dir, keep)
    return target


def prune_backups(dest_dir: Path | str, keep: int) -> list[Path]:
    """Delete all but the newest `keep` checks-*.db backups. Returns what was removed."""
    files = sorted(Path(dest_dir).glob("checks-*.db"), key=lambda f: f.stat().st_mtime)
    removed = files[:-keep] if keep > 0 else files
    for f in removed:
        f.unlink()
    return removed


# ------------------------------------------------------------------- reset

# Child tables first so the deletes never depend on cascade order.
DATA_TABLES = ("finding_status_event", "metric", "finding", "check_report",
               "location", "account")


def reset_db(conn: sqlite3.Connection) -> None:
    """Delete every row, keeping the schema. VACUUM so the old rows don't
    linger in free pages of the file."""
    conn.execute("BEGIN")
    try:
        for table in DATA_TABLES:
            conn.execute(f"DELETE FROM {table}")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")
    conn.execute("VACUUM")


# --------------------------------------------------------------- demo data

DEMO_SQL = """
INSERT INTO account (name, notes) VALUES
    ('Northwind Retail', 'Pilot account'),
    ('Baltic Markets', '');

INSERT INTO location (account_id, name, code) VALUES
    ((SELECT id FROM account WHERE name = 'Northwind Retail'), 'Store 101 - Centrum', '101'),
    ((SELECT id FROM account WHERE name = 'Northwind Retail'), 'Store 102 - Zuid', '102'),
    ((SELECT id FROM account WHERE name = 'Baltic Markets'), 'Riga Main', 'RIG-1');

INSERT INTO check_report (location_id, checked_on, checked_by, summary, status) VALUES
    ((SELECT id FROM location WHERE code = '101'), '2026-09-15', 'demo', 'First pass', 'complete'),
    ((SELECT id FROM location WHERE code = '101'), '2026-10-15', 'demo', 'Monthly re-check', 'complete'),
    ((SELECT id FROM location WHERE code = '102'), '2026-10-16', 'demo', '', 'draft');

INSERT INTO metric (report_id, key, value_text, value_num, unit, sort_order) VALUES
    (1, 'baskets_counted', '1240', 1240, '', 1),
    (1, 'match_rate',      '94.2',  94.2, '%', 2),
    (1, 'shelf_lines',     '512',   512,  '', 3),
    (2, 'baskets_counted', '1310', 1310, '', 1),
    (2, 'match_rate',      '97.8',  97.8, '%', 2),
    (2, 'shelf_lines',     '512',   512,  '', 3),
    (2, 'notes_reviewed',  'yes',   NULL, '', 4),
    (3, 'baskets_counted', '880',   880,  '', 1);

INSERT INTO finding (report_id, category, description, suspected_cause, change_made, status) VALUES
    (1, 'config_error', 'Shelf rows 12-18 offset by one line in the planogram export.',
        'Export template changed mid-month', 'Re-mapped rows in the import config', 'fixed'),
    (1, 'imbalance', 'Basket count 40 lower than POS total.',
        'Camera 3 downtime', '', 'open'),
    (1, 'llm_scoring_error', 'Scoring model flagged correct labels as mismatched.',
        'Prompt drift after model update', '', 'not_resolved');

-- Second check verifies one carry-over and reopens another as a new finding.
INSERT INTO finding (report_id, category, description, suspected_cause, change_made, status, parent_finding_id) VALUES
    (2, 'imbalance', 'Basket count still 15 short; smaller gap after camera fix.',
        'Residual camera 3 blind spot', 'Adjusted camera angle', 'open', 2),
    (2, 'imbalance', 'Stock imbalance between back room and floor counts.',
        'Manual restock not logged', '', 'open', NULL);

INSERT INTO finding_status_event (finding_id, report_id, from_status, to_status, note) VALUES
    (1, 1, NULL, 'open', 'Logged during first check'),
    (1, 1, 'open', 'fixed', 'Import config corrected'),
    (2, 1, NULL, 'open', 'Logged during first check'),
    (3, 1, NULL, 'not_resolved', 'Needs vendor follow-up'),
    (4, 2, NULL, 'open', 'Carried over from finding #2'),
    (5, 2, NULL, 'open', 'New this check');
"""


def seed_demo(conn: sqlite3.Connection) -> None:
    conn.executescript(DEMO_SQL)


# ------------------------------------------------------------------- CLI

def _cmd_init(args: argparse.Namespace) -> int:
    path = Path(args.db)
    existed = path.exists()
    applied = init_db(path)
    print(f"{'Database' if existed else 'Created database'} {path.resolve()}")
    print(f"Migrations applied: {', '.join(applied)}" if applied
          else "Schema is up to date.")
    if args.demo:
        conn = connect(path)
        if conn.execute("SELECT COUNT(*) FROM account").fetchone()[0]:
            print("Demo data skipped: database already has accounts.")
        else:
            seed_demo(conn)
            print("Demo data inserted.")
        conn.close()
    return 0


def _cmd_check(args: argparse.Namespace) -> int:
    path = Path(args.db)
    if not path.exists():
        print(f"No database at {path.resolve()}; run: db.py init", file=sys.stderr)
        return 1
    conn = connect(path)
    latest = find_migrations()[-1].version
    print(f"database:       {path.resolve()}")
    print(f"backups:        {config.BACKUP_DIR}")
    print(f"schema version: {schema_version(conn)} (code has migrations up to {latest})\n")
    objs = conn.execute(
        "SELECT type, name FROM sqlite_master "
        "WHERE name NOT LIKE 'sqlite_%' AND type IN ('table','view','trigger','index') "
        "ORDER BY type, name"
    ).fetchall()
    for kind in ("table", "view", "index", "trigger"):
        names = [o["name"] for o in objs if o["type"] == kind]
        print(f"{kind}{'es' if kind == 'index' else 's'} ({len(names)}): {', '.join(names) or '-'}")
    print()
    for table in ("account", "location", "check_report", "metric", "finding",
                  "finding_status_event"):
        n = conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
        print(f"{table:22} {n:>5} rows")
    fk = conn.execute("PRAGMA foreign_key_check").fetchall()
    print(f"\nforeign_key_check: {'OK' if not fk else fk}")
    print(f"integrity_check:   {conn.execute('PRAGMA integrity_check').fetchone()[0]}")
    conn.close()
    return 0


def _cmd_backup(args: argparse.Namespace) -> int:
    target = backup_db(args.db, daily=False)
    print(f"Backup written: {target}")
    return 0


RESET_CONFIRMATION = "RESET"


def _cmd_reset(args: argparse.Namespace) -> int:
    path = Path(args.db)
    if not path.exists():
        print(f"No database at {path.resolve()}", file=sys.stderr)
        return 1
    conn = connect(path)
    counts = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in DATA_TABLES}
    print(f"This deletes ALL data in {path.resolve()}:")
    for table in reversed(DATA_TABLES):
        print(f"  {table:22} {counts[table]:>5} rows")
    print(f"A backup is written to {config.BACKUP_DIR} first. The schema is kept.")
    try:
        answer = input(f"Type {RESET_CONFIRMATION} to continue: ")
    except EOFError:
        answer = ""
    if answer.strip() != RESET_CONFIRMATION:
        print("Cancelled; nothing changed.")
        conn.close()
        return 1
    target = backup_db(path, daily=False)
    print(f"Backup written: {target}")
    reset_db(conn)
    conn.close()
    print("Database is now empty.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default=str(DB_PATH), help="path to the SQLite file")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_init = sub.add_parser("init", help="create the database / apply pending migrations")
    p_init.add_argument("--demo", action="store_true", help="insert demo rows (empty database only)")
    p_init.set_defaults(func=_cmd_init)

    p_check = sub.add_parser("check", help="show schema version, objects and row counts")
    p_check.set_defaults(func=_cmd_check)

    p_backup = sub.add_parser("backup", help="write a timestamped backup now")
    p_backup.set_defaults(func=_cmd_backup)

    p_reset = sub.add_parser("reset", help="delete ALL data (asks to confirm, backs up first)")
    p_reset.set_defaults(func=_cmd_reset)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except RuntimeError as exc:  # cloud folder, failed migration, version mismatch
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
