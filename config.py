"""Settings for Regular Check Tracker; app.py and db.py import these.

This file holds the defaults and is tracked in git. Don't edit it for your own
machine: put overrides in config_local.py (git-ignored; copy
config_local.example.py to start). Anything set there replaces the value here,
and settings added here later still reach you after a git pull.
"""

from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent

# Local only: never change HOST to 0.0.0.0 or a network address.
HOST = "127.0.0.1"
PORT = 8765

# The SQLite database. Default: checks.db in the project folder. A relative
# path is taken relative to the project folder. Must not be in a cloud-synced
# folder (enforced, see CLOUD_FOLDER_MARKERS).
DB_PATH = "checks.db"

# Automatic backup of the database at app start (SQLite backup API), one per
# day, newest BACKUP_KEEP kept; also written before any schema migration.
# Default: a folder next to the project, e.g. C:\dev\audit-log-backups for
# C:\dev\audit-log. Relative paths are taken relative to the project folder.
# Must not be in a cloud-synced folder (enforced).
BACKUP_DIR = f"../{PROJECT_DIR.name}-backups"
BACKUP_KEEP = 30

# Compare view: a numeric metric that moved by at least this many percent
# (either direction) is highlighted. A change away from 0 always counts.
CHANGE_HIGHLIGHT_PERCENT = 10.0
# ... except percentage metrics (unit "%" or value like "97.8%"), which are
# highlighted on their change in percentage points instead: 97% -> 92% is 5 pp.
CHANGE_HIGHLIGHT_PP = 3.0

# Findings search: results per page (the CSV export always has all matches).
SEARCH_PAGE_SIZE = 50
# CSV export column separator. Use ";" if Excel opens the file as one column
# (Excel with e.g. a Dutch or Turkish list separator setting).
CSV_DELIMITER = ","

# Audit data must stay on this machine. The database and backups are refused
# when a folder in their path is named one of these (case-insensitive), or
# starts with one followed by " ", "-", "_" or "(" ("OneDrive - Company",
# "Dropbox (Personal)", "GoogleDrive-name@example.com"); also anywhere under
# the OneDrive* environment variables' folders, and under a macOS Desktop or
# Documents folder that iCloud syncs.
CLOUD_FOLDER_MARKERS = (
    "onedrive", "dropbox", "google drive", "googledrive", "my drive",
    "icloud drive", "iclouddrive", "mobile documents", "cloudstorage",
    "box", "box sync", "nextcloud", "owncloud", "pcloud", "sync.com",
)

# ------------------------------------------------------------ local overrides

try:
    from config_local import *  # noqa: F401,F403
except ModuleNotFoundError as exc:
    if exc.name != "config_local":
        raise


def _project_path(value) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else (PROJECT_DIR / path).resolve()


DB_PATH = _project_path(DB_PATH)
BACKUP_DIR = _project_path(BACKUP_DIR)
