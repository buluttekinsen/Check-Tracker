"""Regular Check Tracker -- web app (hierarchy CRUD, checks, findings, carry-over, compare, search, backups).

Local only. Everyday use: start.bat (Windows) / start.sh (macOS). Directly,
with the project venv (.venv/bin/python on macOS):

    .venv/Scripts/python.exe app.py               # host/port from config.py
    .venv/Scripts/python.exe -m uvicorn app:app --host 127.0.0.1 --port 8765 --reload
"""

from __future__ import annotations

import csv
import io
import json
import logging
import re
import sqlite3
import unicodedata
from contextlib import asynccontextmanager, closing, contextmanager
from datetime import date, datetime
from itertools import zip_longest
from pathlib import Path
from urllib.parse import urlencode, urlsplit

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markupsafe import Markup, escape
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.trustedhost import TrustedHostMiddleware

import db
import config
from config import (CHANGE_HIGHLIGHT_PERCENT, CHANGE_HIGHLIGHT_PP, CSV_DELIMITER, HOST,
                    PORT, SEARCH_PAGE_SIZE)

log = logging.getLogger("uvicorn.error")

BASE_DIR = Path(__file__).resolve().parent
# Requests must name this machine; blocks DNS-rebinding and cross-site posts.
# Hostnames only: both checks ignore the port, so they work with any PORT.
ALLOWED_HOSTS = ["127.0.0.1", "localhost"]


@asynccontextmanager
async def lifespan(app_: FastAPI):
    try:  # creates the database / backs up and applies pending migrations
        applied = db.init_db()
    except RuntimeError as exc:  # cloud folder, failed migration, version mismatch
        log.error("Cannot start: %s", exc)
        raise
    if applied:
        log.info("Database migrated: %s", ", ".join(applied))
    try:
        target = db.backup_db()  # daily copy to config.BACKUP_DIR, newest BACKUP_KEEP kept
        if target:
            log.info("Backup written: %s", target)
        app_.state.backup_note = latest_backup_note()
    except Exception as exc:  # never block the app, but make the failure visible
        log.error("Automatic backup FAILED: %s", exc)
        app_.state.backup_note = "Backup FAILED (see console)"
    yield


def latest_backup_note() -> str:
    backups = list(config.BACKUP_DIR.glob("checks-*.db"))
    if not backups:
        return "No backup yet"
    newest = max(f.stat().st_mtime for f in backups)
    return f"Last backup {datetime.fromtimestamp(newest):%Y-%m-%d %H:%M}"


# docs/redoc are disabled: their UI pages load assets from a CDN.
app = FastAPI(title="Check Tracker", lifespan=lifespan,
              docs_url=None, redoc_url=None, openapi_url=None)
app.state.backup_note = ""
app.add_middleware(TrustedHostMiddleware, allowed_hosts=ALLOWED_HOSTS)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")

templates = Jinja2Templates(directory=BASE_DIR / "templates")
templates.env.globals.update(
    REPORT_STATUSES=db.REPORT_STATUSES,
    REPORT_STATUS_LABELS=dict(db.REPORT_STATUSES),
    FINDING_CATEGORIES=db.FINDING_CATEGORIES,
    FINDING_STATUSES=db.FINDING_STATUSES,
    CATEGORY_LABELS=db.CATEGORY_LABELS,
    STATUS_LABELS=db.STATUS_LABELS,
)


def static_url(name: str) -> str:
    """/static/<name>?v=<mtime>: a changed file gets a new URL, so the browser
    never pairs fresh markup with a stale cached stylesheet or script."""
    return f"/static/{name}?v={(BASE_DIR / 'static' / name).stat().st_mtime_ns}"


templates.env.globals["static_url"] = static_url


def format_delta(value: float) -> str:
    """+1,234.5 style: sign always shown, thousands separators, no float noise."""
    text = f"{value:+,.4f}".rstrip("0").rstrip(".")
    return "0" if text in ("+0", "-0") else text


templates.env.filters["delta"] = format_delta
templates.env.filters["pct"] = lambda value: f"{value:+.1f}%" if value else "0%"


@app.middleware("http")
async def same_origin_writes(request: Request, call_next):
    """Refuse state-changing requests sent by pages from another origin."""
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        origin = request.headers.get("origin")
        if origin is not None and urlsplit(origin).hostname not in ALLOWED_HOSTS:
            return PlainTextResponse("Cross-origin request refused.", status_code=403)
    return await call_next(request)


@app.exception_handler(StarletteHTTPException)
async def http_error(request: Request, exc: StarletteHTTPException):
    message = exc.detail if exc.status_code != 404 or exc.detail != "Not Found" else "Page not found."
    return render(request, "error.html", status_code=exc.status_code, message=message)


# ---------------------------------------------------------------- helpers

def open_db():
    # One connection per request, opened and closed in the handler's own
    # thread (sqlite3 connections refuse cross-thread use).
    conn = db.connect()
    # Search matching (SQLite's lower()/LIKE only fold ASCII letters).
    conn.create_function("fold", 1, lambda s: fold(s) if s else s, deterministic=True)
    return closing(conn)


def fold(text: str) -> str:
    """Case- and accent-insensitive form for search: 'IŞIK', 'ışık' and 'isik'
    all become 'isik'; 'İstasyonu' and 'istasyonu' match; 'é' matches 'e'."""
    decomposed = unicodedata.normalize("NFKD", text.casefold().replace("ı", "i"))
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def render(request: Request, name: str, status_code: int = 200, **context):
    return templates.TemplateResponse(request, name, context, status_code=status_code)


def redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


def name_error(name: str) -> str | None:
    if not name:
        return "Name is required."
    if len(name) > 200:
        return "Name is too long (200 characters max)."
    return None


def integrity_message(exc: sqlite3.IntegrityError, what: str, name: str) -> str:
    if "UNIQUE" in str(exc):
        return f"{what} named “{name}” already exists."
    return f"Could not save: {exc}"


def get_account(conn: sqlite3.Connection, account_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM account WHERE id = ?", (account_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "Account not found.")
    return row


@contextmanager
def transaction(conn: sqlite3.Connection):
    # connect() uses autocommit, so multi-statement writes open one explicitly.
    conn.execute("BEGIN")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def get_location(conn: sqlite3.Connection, location_id: int) -> sqlite3.Row:
    row = conn.execute(
        "SELECT l.*, a.name AS account_name, a.archived AS account_archived "
        "FROM location l JOIN account a ON a.id = l.account_id WHERE l.id = ?",
        (location_id,),
    ).fetchone()
    if row is None:
        raise HTTPException(404, "Location not found.")
    return row


# --------------------------------------------------------------- accounts

@app.get("/", include_in_schema=False)
def home():
    return redirect("/accounts")


@app.get("/accounts", response_class=HTMLResponse)
def account_list(request: Request, archived: bool = False):
    with open_db() as conn:
        rows = conn.execute(
            """
            SELECT a.*,
                (SELECT COUNT(*) FROM location l
                  WHERE l.account_id = a.id AND l.archived = 0) AS location_count,
                (SELECT MAX(r.checked_on) FROM check_report r
                   JOIN location l ON l.id = r.location_id
                  WHERE l.account_id = a.id) AS last_checked_on,
                (SELECT COUNT(*) FROM v_open_finding o
                   JOIN location l ON l.id = o.location_id
                  WHERE l.account_id = a.id) AS open_finding_count
            FROM account a
            WHERE ? OR a.archived = 0
            ORDER BY a.archived, lower(a.name)
            """,
            (int(archived),),
        ).fetchall()
        archived_count = conn.execute(
            "SELECT COUNT(*) FROM account WHERE archived = 1").fetchone()[0]
    return render(request, "accounts/list.html", accounts=rows,
                  show_archived=archived, archived_count=archived_count)


@app.get("/accounts/new", response_class=HTMLResponse)
def account_new(request: Request):
    return render(request, "accounts/form.html", account=None,
                  values={"name": "", "notes": ""}, error=None)


@app.post("/accounts/new", response_class=HTMLResponse)
def account_create(request: Request, name: str = Form(""), notes: str = Form("")):
    values = {"name": name.strip(), "notes": notes.strip()}
    error = name_error(values["name"])
    if error is None:
        try:
            with open_db() as conn:
                cur = conn.execute("INSERT INTO account (name, notes) VALUES (?, ?)",
                                   (values["name"], values["notes"]))
            return redirect(f"/accounts/{cur.lastrowid}")
        except sqlite3.IntegrityError as exc:
            error = integrity_message(exc, "An account", values["name"])
    return render(request, "accounts/form.html", status_code=422,
                  account=None, values=values, error=error)


@app.get("/accounts/{account_id}", response_class=HTMLResponse)
def account_detail(request: Request, account_id: int, archived: bool = False):
    with open_db() as conn:
        account = get_account(conn, account_id)
        locations = conn.execute(
            """
            SELECT l.*,
                (SELECT COUNT(*) FROM check_report r
                  WHERE r.location_id = l.id) AS check_count,
                (SELECT MAX(r.checked_on) FROM check_report r
                  WHERE r.location_id = l.id) AS last_checked_on,
                (SELECT COUNT(*) FROM v_open_finding o
                  WHERE o.location_id = l.id) AS open_finding_count
            FROM location l
            WHERE l.account_id = ? AND (? OR l.archived = 0)
            ORDER BY l.archived, lower(l.name)
            """,
            (account_id, int(archived)),
        ).fetchall()
        archived_count = conn.execute(
            "SELECT COUNT(*) FROM location WHERE account_id = ? AND archived = 1",
            (account_id,)).fetchone()[0]
    return render(request, "accounts/detail.html", account=account, locations=locations,
                  show_archived=archived, archived_count=archived_count)


@app.get("/accounts/{account_id}/edit", response_class=HTMLResponse)
def account_edit(request: Request, account_id: int):
    with open_db() as conn:
        account = get_account(conn, account_id)
    return render(request, "accounts/form.html", account=account,
                  values={"name": account["name"], "notes": account["notes"]}, error=None)


@app.post("/accounts/{account_id}/edit", response_class=HTMLResponse)
def account_update(request: Request, account_id: int,
                   name: str = Form(""), notes: str = Form("")):
    values = {"name": name.strip(), "notes": notes.strip()}
    with open_db() as conn:
        account = get_account(conn, account_id)
        error = name_error(values["name"])
        if error is None:
            try:
                conn.execute("UPDATE account SET name = ?, notes = ? WHERE id = ?",
                             (values["name"], values["notes"], account_id))
                return redirect(f"/accounts/{account_id}")
            except sqlite3.IntegrityError as exc:
                error = integrity_message(exc, "An account", values["name"])
    return render(request, "accounts/form.html", status_code=422,
                  account=account, values=values, error=error)


@app.post("/accounts/{account_id}/archive")
def account_archive(account_id: int, archived: int = Form(...)):
    with open_db() as conn:
        get_account(conn, account_id)
        conn.execute("UPDATE account SET archived = ? WHERE id = ?",
                     (1 if archived else 0, account_id))
    return redirect(f"/accounts/{account_id}")


# -------------------------------------------------------------- locations

@app.get("/accounts/{account_id}/locations/new", response_class=HTMLResponse)
def location_new(request: Request, account_id: int):
    with open_db() as conn:
        account = get_account(conn, account_id)
    return render(request, "locations/form.html", account=account, location=None,
                  values={"name": "", "code": "", "notes": ""}, error=None)


@app.post("/accounts/{account_id}/locations/new", response_class=HTMLResponse)
def location_create(request: Request, account_id: int, name: str = Form(""),
                    code: str = Form(""), notes: str = Form("")):
    values = {"name": name.strip(), "code": code.strip(), "notes": notes.strip()}
    with open_db() as conn:
        account = get_account(conn, account_id)
        error = name_error(values["name"])
        if error is None:
            try:
                cur = conn.execute(
                    "INSERT INTO location (account_id, name, code, notes) VALUES (?, ?, ?, ?)",
                    (account_id, values["name"], values["code"], values["notes"]))
                return redirect(f"/locations/{cur.lastrowid}")
            except sqlite3.IntegrityError as exc:
                error = integrity_message(exc, "A location in this account", values["name"])
    return render(request, "locations/form.html", status_code=422,
                  account=account, location=None, values=values, error=error)


@app.get("/locations/{location_id}", response_class=HTMLResponse)
def location_detail(request: Request, location_id: int):
    with open_db() as conn:
        location = get_location(conn, location_id)
        checks = conn.execute(
            "SELECT * FROM v_check_report_full WHERE location_id = ? "
            "ORDER BY checked_on DESC, report_id DESC",
            (location_id,),
        ).fetchall()
        open_findings = conn.execute(
            "SELECT COUNT(*) FROM v_open_finding WHERE location_id = ?",
            (location_id,),
        ).fetchone()[0]
    return render(request, "locations/detail.html", location=location,
                  checks=checks, open_findings=open_findings)


@app.get("/locations/{location_id}/edit", response_class=HTMLResponse)
def location_edit(request: Request, location_id: int):
    with open_db() as conn:
        location = get_location(conn, location_id)
        account = get_account(conn, location["account_id"])
    return render(request, "locations/form.html", account=account, location=location,
                  values={k: location[k] for k in ("name", "code", "notes")}, error=None)


@app.post("/locations/{location_id}/edit", response_class=HTMLResponse)
def location_update(request: Request, location_id: int, name: str = Form(""),
                    code: str = Form(""), notes: str = Form("")):
    values = {"name": name.strip(), "code": code.strip(), "notes": notes.strip()}
    with open_db() as conn:
        location = get_location(conn, location_id)
        account = get_account(conn, location["account_id"])
        error = name_error(values["name"])
        if error is None:
            try:
                conn.execute("UPDATE location SET name = ?, code = ?, notes = ? WHERE id = ?",
                             (values["name"], values["code"], values["notes"], location_id))
                return redirect(f"/locations/{location_id}")
            except sqlite3.IntegrityError as exc:
                error = integrity_message(exc, "A location in this account", values["name"])
    return render(request, "locations/form.html", status_code=422,
                  account=account, location=location, values=values, error=error)


@app.post("/locations/{location_id}/archive")
def location_archive(location_id: int, archived: int = Form(...)):
    with open_db() as conn:
        get_location(conn, location_id)
        conn.execute("UPDATE location SET archived = ? WHERE id = ?",
                     (1 if archived else 0, location_id))
    return redirect(f"/locations/{location_id}")

# ------------------------------------------------------------ check helpers

DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
EXTRA_BLANK_METRIC_ROWS = 2


def date_error(value: str, label: str, required: bool = False) -> str | None:
    if not value:
        return f"{label} is required." if required else None
    if not DATE_RE.fullmatch(value):
        return f"{label} must be a date (YYYY-MM-DD)."
    try:
        date.fromisoformat(value)
    except ValueError:
        return f"{label} is not a valid date."
    return None


def collect_metric_rows(keys: list[str], values: list[str], units: list[str],
                        notes: list[str]) -> list[dict]:
    """Zip the repeated metric_* form fields into rows, dropping fully empty ones."""
    rows = []
    for key, value, unit, note in zip_longest(keys, values, units, notes, fillvalue=""):
        row = {"key": key.strip(), "value": value.strip(),
               "unit": unit.strip(), "note": note.strip()}
        if any(row.values()):
            rows.append(row)
    return rows


def check_form_errors(values: dict, metric_rows: list[dict]) -> list[str]:
    errors = [e for e in (
        date_error(values["checked_on"], "Check date", required=True),
        date_error(values["period_start"], "Period start"),
        date_error(values["period_end"], "Period end"),
    ) if e]
    if (not errors and values["period_start"] and values["period_end"]
            and values["period_start"] > values["period_end"]):
        errors.append("Period start must be on or before period end.")
    if values["status"] not in dict(db.REPORT_STATUSES):
        errors.append("Unknown check status.")
    seen: set[str] = set()
    for row in metric_rows:
        if not row["key"]:
            errors.append(f"Metric value “{row['value'] or row['note']}” has no key.")
        elif row["value"]:
            folded = row["key"].lower()
            if folded in seen:
                errors.append(f"Metric “{row['key']}” is listed twice.")
            seen.add(folded)
    return errors


def save_metrics(conn: sqlite3.Connection, report_id: int, metric_rows: list[dict]) -> None:
    """Replace the report's metrics. Rows with a key but no value are not saved."""
    conn.execute("DELETE FROM metric WHERE report_id = ?", (report_id,))
    conn.executemany(
        "INSERT INTO metric (report_id, key, value_text, value_num, unit, note, sort_order) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        [(report_id, r["key"], r["value"], db.parse_number(r["value"]), r["unit"], r["note"], i)
         for i, r in enumerate((r for r in metric_rows if r["key"] and r["value"]), start=1)],
    )


def metric_suggestions(conn: sqlite3.Connection, location_id: int) -> tuple[list, list, dict]:
    """Metric keys and units used before at this location, for the datalists.

    Keys group by lower(key); the bare columns come from the row holding
    MAX(...), i.e. the most recent check, so the latest spelling and unit win.
    """
    rows = conn.execute(
        """
        SELECT m.key, m.unit, MAX(r.checked_on || printf('%010d', r.id)) AS latest
        FROM metric m JOIN check_report r ON r.id = m.report_id
        WHERE r.location_id = ?
        GROUP BY lower(m.key)
        ORDER BY lower(m.key)
        """,
        (location_id,),
    ).fetchall()
    keys = [r["key"] for r in rows]
    units = sorted({r["unit"] for r in rows if r["unit"]}, key=str.lower)
    unit_by_key = {r["key"].lower(): r["unit"] for r in rows if r["unit"]}
    return keys, units, unit_by_key


def latest_report(conn: sqlite3.Connection, location_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM check_report WHERE location_id = ? "
        "ORDER BY checked_on DESC, id DESC LIMIT 1", (location_id,)).fetchone()


def report_metrics(conn: sqlite3.Connection, report_id: int | None) -> list[sqlite3.Row]:
    if report_id is None:
        return []
    return conn.execute("SELECT * FROM metric WHERE report_id = ? ORDER BY sort_order, id",
                        (report_id,)).fetchall()


def render_check_form(request: Request, conn: sqlite3.Connection, *, location: sqlite3.Row,
                      report: sqlite3.Row | None, values: dict, metric_rows: list[dict],
                      prev_report_id: int | None, errors: list[str], status_code: int = 200):
    keys, units, unit_by_key = metric_suggestions(conn, location["id"])
    prev_values = {m["key"].lower(): m["value_text"] for m in report_metrics(conn, prev_report_id)}
    blanks = [{"key": "", "value": "", "unit": "", "note": ""}] * EXTRA_BLANK_METRIC_ROWS
    open_count = None
    if report is None:
        open_count = conn.execute("SELECT COUNT(*) FROM v_open_finding WHERE location_id = ?",
                                  (location["id"],)).fetchone()[0]
    return render(request, "checks/form.html", status_code=status_code,
                  location=location, report=report, values=values,
                  metric_rows=metric_rows + blanks, prev_values=prev_values,
                  key_suggestions=keys, unit_suggestions=units,
                  unit_by_key_json=json.dumps(unit_by_key),
                  open_count=open_count, errors=errors)


def get_report(conn: sqlite3.Connection, report_id: int) -> sqlite3.Row:
    row = conn.execute(
        "SELECT v.*, l.archived AS location_archived FROM v_check_report_full v "
        "JOIN location l ON l.id = v.location_id WHERE v.report_id = ?",
        (report_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "Check not found.")
    return row


def get_finding_for_report(conn: sqlite3.Connection, report: sqlite3.Row,
                           finding_id: int) -> sqlite3.Row:
    """A finding at the same location as `report` (its own or a carried-over one)."""
    row = conn.execute(
        "SELECT f.*, r.checked_on AS found_on, r.location_id FROM finding f "
        "JOIN check_report r ON r.id = f.report_id WHERE f.id = ?",
        (finding_id,)).fetchone()
    if row is None or row["location_id"] != report["location_id"]:
        raise HTTPException(404, "Finding not found at this location.")
    return row


def finding_values(category: str, description: str, suspected_cause: str,
                   change_made: str, status: str, note: str) -> dict:
    return {"category": category, "description": description.strip(),
            "suspected_cause": suspected_cause.strip(), "change_made": change_made.strip(),
            "status": status, "note": note.strip()}


def finding_errors(values: dict) -> list[str]:
    errors = []
    if values["category"] not in db.CATEGORY_LABELS:
        errors.append("Choose a category.")
    if values["status"] not in db.STATUS_LABELS:
        errors.append("Choose a status.")
    if not values["description"]:
        errors.append("Description is required.")
    return errors


def record_status(conn: sqlite3.Connection, finding: sqlite3.Row, new_status: str,
                  note: str, report_id: int) -> bool:
    """Set a finding's status and log the event against the check it happened in.

    A note with an unchanged status is logged too ("re-checked, still open").
    Returns False when there was nothing to record.
    """
    if new_status == finding["status"] and not note:
        return False
    conn.execute("UPDATE finding SET status = ? WHERE id = ?", (new_status, finding["id"]))
    conn.execute(
        "INSERT INTO finding_status_event (finding_id, report_id, from_status, to_status, note) "
        "VALUES (?, ?, ?, ?, ?)",
        (finding["id"], report_id, finding["status"], new_status, note))
    return True


def render_check_detail(request: Request, conn: sqlite3.Connection, report: sqlite3.Row, *,
                        new_finding: dict | None = None, finding_errors_: list[str] = (),
                        carry_error: str | None = None, status_code: int = 200):
    rid = report["report_id"]
    metrics = report_metrics(conn, rid)
    findings = conn.execute(
        """
        SELECT f.*,
               p.report_id  AS parent_report_id,
               pr.checked_on AS parent_checked_on,
               (SELECT c.id FROM finding c WHERE c.parent_finding_id = f.id
                 ORDER BY c.id DESC LIMIT 1) AS child_id,
               (SELECT c.report_id FROM finding c WHERE c.parent_finding_id = f.id
                 ORDER BY c.id DESC LIMIT 1) AS child_report_id
        FROM finding f
        LEFT JOIN finding p       ON p.id = f.parent_finding_id
        LEFT JOIN check_report pr ON pr.id = p.report_id
        WHERE f.report_id = ?
        ORDER BY f.id
        """, (rid,)).fetchall()
    # Carry-over panel: findings from earlier checks at this location that
    # still need attention, plus any earlier finding already updated during
    # this check (so a just-fixed one stays visible here).
    carry_over = conn.execute(
        """
        SELECT f.*, r.checked_on AS found_on,
               EXISTS (SELECT 1 FROM finding_status_event e
                        WHERE e.finding_id = f.id AND e.report_id = :rid) AS touched,
               (SELECT e.note FROM finding_status_event e
                 WHERE e.finding_id = f.id ORDER BY e.id DESC LIMIT 1) AS last_note
        FROM finding f
        JOIN check_report r ON r.id = f.report_id
        WHERE r.location_id = :loc AND f.report_id <> :rid
          AND (
                (f.id IN (SELECT finding_id FROM v_open_finding)
                 AND (r.checked_on < :d OR (r.checked_on = :d AND r.id < :rid)))
             OR EXISTS (SELECT 1 FROM finding_status_event e
                         WHERE e.finding_id = f.id AND e.report_id = :rid)
          )
        ORDER BY r.checked_on, f.id
        """,
        {"rid": rid, "loc": report["location_id"], "d": report["checked_on"]},
    ).fetchall()
    if new_finding is None:
        new_finding = {"category": "", "description": "", "suspected_cause": "",
                       "change_made": "", "status": "open", "note": "",
                       "parent_finding_id": None}
        recur = request.query_params.get("recur", "")
        if recur.isdigit():
            parent = get_finding_for_report(conn, report, int(recur))
            new_finding.update(category=parent["category"], description=parent["description"],
                               suspected_cause=parent["suspected_cause"],
                               parent_finding_id=parent["id"], parent_found_on=parent["found_on"])
    return render(request, "checks/detail.html", status_code=status_code,
                  report=report, metrics=metrics, findings=findings, carry_over=carry_over,
                  new_finding=new_finding, finding_errors=finding_errors_,
                  carry_error=carry_error)


# ----------------------------------------------------------------- checks

@app.get("/locations/{location_id}/checks/new", response_class=HTMLResponse)
def check_new(request: Request, location_id: int):
    with open_db() as conn:
        location = get_location(conn, location_id)
        prev = latest_report(conn, location_id)
        # Start from the previous check's metric keys/units with empty values,
        # since regular checks usually record the same metrics.
        metric_rows = [{"key": m["key"], "value": "", "unit": m["unit"], "note": ""}
                       for m in report_metrics(conn, prev["id"] if prev else None)]
        values = {"checked_on": date.today().isoformat(),
                  "checked_by": prev["checked_by"] if prev else "",
                  "period_start": "", "period_end": "", "summary": "", "status": "draft"}
        return render_check_form(request, conn, location=location, report=None, values=values,
                                 metric_rows=metric_rows,
                                 prev_report_id=prev["id"] if prev else None, errors=[])


def _check_values(checked_on, checked_by, period_start, period_end, summary, status) -> dict:
    return {"checked_on": checked_on.strip(), "checked_by": checked_by.strip(),
            "period_start": period_start.strip(), "period_end": period_end.strip(),
            "summary": summary.strip(), "status": status}


@app.post("/locations/{location_id}/checks/new", response_class=HTMLResponse)
def check_create(request: Request, location_id: int,
                 checked_on: str = Form(""), checked_by: str = Form(""),
                 period_start: str = Form(""), period_end: str = Form(""),
                 summary: str = Form(""), status: str = Form("draft"),
                 metric_key: list[str] = Form([]), metric_value: list[str] = Form([]),
                 metric_unit: list[str] = Form([]), metric_note: list[str] = Form([])):
    values = _check_values(checked_on, checked_by, period_start, period_end, summary, status)
    metric_rows = collect_metric_rows(metric_key, metric_value, metric_unit, metric_note)
    with open_db() as conn:
        location = get_location(conn, location_id)
        errors = check_form_errors(values, metric_rows)
        if not errors:
            with transaction(conn):
                cur = conn.execute(
                    "INSERT INTO check_report (location_id, checked_on, checked_by, period_start, "
                    "period_end, summary, status) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (location_id, values["checked_on"], values["checked_by"],
                     values["period_start"] or None, values["period_end"] or None,
                     values["summary"], values["status"]))
                save_metrics(conn, cur.lastrowid, metric_rows)
            return redirect(f"/checks/{cur.lastrowid}")
        prev = latest_report(conn, location_id)
        return render_check_form(request, conn, location=location, report=None, values=values,
                                 metric_rows=metric_rows,
                                 prev_report_id=prev["id"] if prev else None,
                                 errors=errors, status_code=422)


@app.get("/checks/{report_id}", response_class=HTMLResponse)
def check_detail(request: Request, report_id: int):
    with open_db() as conn:
        return render_check_detail(request, conn, get_report(conn, report_id))


@app.get("/checks/{report_id}/edit", response_class=HTMLResponse)
def check_edit(request: Request, report_id: int):
    with open_db() as conn:
        report = get_report(conn, report_id)
        location = get_location(conn, report["location_id"])
        values = {"checked_on": report["checked_on"], "checked_by": report["checked_by"],
                  "period_start": report["period_start"] or "",
                  "period_end": report["period_end"] or "",
                  "summary": report["summary"], "status": report["report_status"]}
        metric_rows = [{"key": m["key"], "value": m["value_text"], "unit": m["unit"],
                        "note": m["note"]} for m in report_metrics(conn, report_id)]
        return render_check_form(request, conn, location=location, report=report, values=values,
                                 metric_rows=metric_rows,
                                 prev_report_id=report["prev_report_id"], errors=[])


@app.post("/checks/{report_id}/edit", response_class=HTMLResponse)
def check_update(request: Request, report_id: int,
                 checked_on: str = Form(""), checked_by: str = Form(""),
                 period_start: str = Form(""), period_end: str = Form(""),
                 summary: str = Form(""), status: str = Form("draft"),
                 metric_key: list[str] = Form([]), metric_value: list[str] = Form([]),
                 metric_unit: list[str] = Form([]), metric_note: list[str] = Form([])):
    values = _check_values(checked_on, checked_by, period_start, period_end, summary, status)
    metric_rows = collect_metric_rows(metric_key, metric_value, metric_unit, metric_note)
    with open_db() as conn:
        report = get_report(conn, report_id)
        errors = check_form_errors(values, metric_rows)
        if not errors:
            with transaction(conn):
                conn.execute(
                    "UPDATE check_report SET checked_on = ?, checked_by = ?, period_start = ?, "
                    "period_end = ?, summary = ?, status = ? WHERE id = ?",
                    (values["checked_on"], values["checked_by"], values["period_start"] or None,
                     values["period_end"] or None, values["summary"], values["status"],
                     report_id))
                save_metrics(conn, report_id, metric_rows)
            return redirect(f"/checks/{report_id}")
        location = get_location(conn, report["location_id"])
        return render_check_form(request, conn, location=location, report=report, values=values,
                                 metric_rows=metric_rows,
                                 prev_report_id=report["prev_report_id"],
                                 errors=errors, status_code=422)


@app.post("/checks/{report_id}/delete")
def check_delete(report_id: int):
    with open_db() as conn:
        report = get_report(conn, report_id)
        if report["report_status"] != "draft":
            raise HTTPException(409, "Only draft checks can be deleted.")
        conn.execute("DELETE FROM check_report WHERE id = ?", (report_id,))
    return redirect(f"/locations/{report['location_id']}")


# --------------------------------------------------------------- findings

@app.post("/checks/{report_id}/findings", response_class=HTMLResponse)
def finding_create(request: Request, report_id: int,
                   category: str = Form(""), description: str = Form(""),
                   suspected_cause: str = Form(""), change_made: str = Form(""),
                   status: str = Form("open"), note: str = Form(""),
                   parent_finding_id: str = Form("")):
    values = finding_values(category, description, suspected_cause, change_made, status, note)
    with open_db() as conn:
        report = get_report(conn, report_id)
        parent = None
        if parent_finding_id.isdigit():
            parent = get_finding_for_report(conn, report, int(parent_finding_id))
        values["parent_finding_id"] = parent["id"] if parent else None
        if parent:
            values["parent_found_on"] = parent["found_on"]
        errors = finding_errors(values)
        if not errors:
            with transaction(conn):
                cur = conn.execute(
                    "INSERT INTO finding (report_id, category, description, suspected_cause, "
                    "change_made, status, parent_finding_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (report_id, values["category"], values["description"],
                     values["suspected_cause"], values["change_made"], values["status"],
                     values["parent_finding_id"]))
                default_note = (f"Recurrence of finding #{parent['id']}" if parent
                                else "Logged during this check")
                conn.execute(
                    "INSERT INTO finding_status_event (finding_id, report_id, from_status, "
                    "to_status, note) VALUES (?, ?, NULL, ?, ?)",
                    (cur.lastrowid, report_id, values["status"], values["note"] or default_note))
            return redirect(f"/checks/{report_id}#finding-{cur.lastrowid}")
        return render_check_detail(request, conn, report, new_finding=values,
                                   finding_errors_=errors, status_code=422)


@app.post("/checks/{report_id}/findings/{finding_id}/status", response_class=HTMLResponse)
def finding_quick_status(request: Request, report_id: int, finding_id: int,
                         status: str = Form(""), note: str = Form("")):
    """Carry-over default: update the existing finding in place, tagged with this check."""
    with open_db() as conn:
        report = get_report(conn, report_id)
        finding = get_finding_for_report(conn, report, finding_id)
        if status not in db.STATUS_LABELS:
            return render_check_detail(request, conn, report, status_code=422,
                                       carry_error="Choose a status.")
        with transaction(conn):
            record_status(conn, finding, status, note.strip(), report_id)
    return redirect(f"/checks/{report_id}#carry-{finding_id}")


def _finding_page(request, conn, report, finding, values, errors, status_code=200):
    history = conn.execute(
        """
        SELECT e.*, r.checked_on AS report_checked_on
        FROM finding_status_event e LEFT JOIN check_report r ON r.id = e.report_id
        WHERE e.finding_id = ? ORDER BY e.id
        """, (finding["id"],)).fetchall()
    # Whole carry-over chain: ancestors (negative pos), this finding (0), descendants.
    chain = conn.execute(
        """
        WITH RECURSIVE
          up(id, pos) AS (
            SELECT parent_finding_id, -1 FROM finding
             WHERE id = :fid AND parent_finding_id IS NOT NULL
            UNION ALL
            SELECT f.parent_finding_id, up.pos - 1 FROM finding f JOIN up ON f.id = up.id
             WHERE f.parent_finding_id IS NOT NULL AND up.pos > -100),
          down(id, pos) AS (
            SELECT id, 1 FROM finding WHERE parent_finding_id = :fid
            UNION ALL
            SELECT f.id, down.pos + 1 FROM finding f JOIN down ON f.parent_finding_id = down.id
             WHERE down.pos < 100),
          links(id, pos) AS (SELECT * FROM up UNION ALL SELECT :fid, 0 UNION ALL SELECT * FROM down)
        SELECT f.id, f.status, f.description, f.report_id, r.checked_on, links.pos
        FROM links JOIN finding f ON f.id = links.id JOIN check_report r ON r.id = f.report_id
        ORDER BY links.pos, f.id
        """, {"fid": finding["id"]}).fetchall()
    return render(request, "findings/form.html", status_code=status_code, report=report,
                  finding=finding, values=values, errors=errors, history=history,
                  chain=chain if len(chain) > 1 else [])


@app.get("/checks/{report_id}/findings/{finding_id}/edit", response_class=HTMLResponse)
def finding_edit(request: Request, report_id: int, finding_id: int):
    with open_db() as conn:
        report = get_report(conn, report_id)
        finding = get_finding_for_report(conn, report, finding_id)
        values = {k: finding[k] for k in ("category", "description", "suspected_cause",
                                          "change_made", "status")}
        values["note"] = ""
        return _finding_page(request, conn, report, finding, values, [])


@app.post("/checks/{report_id}/findings/{finding_id}/edit", response_class=HTMLResponse)
def finding_update(request: Request, report_id: int, finding_id: int,
                   category: str = Form(""), description: str = Form(""),
                   suspected_cause: str = Form(""), change_made: str = Form(""),
                   status: str = Form(""), note: str = Form("")):
    values = finding_values(category, description, suspected_cause, change_made, status, note)
    with open_db() as conn:
        report = get_report(conn, report_id)
        finding = get_finding_for_report(conn, report, finding_id)
        errors = finding_errors(values)
        if errors:
            return _finding_page(request, conn, report, finding, values, errors, 422)
        with transaction(conn):
            conn.execute(
                "UPDATE finding SET category = ?, description = ?, suspected_cause = ?, "
                "change_made = ? WHERE id = ?",
                (values["category"], values["description"], values["suspected_cause"],
                 values["change_made"], finding_id))
            record_status(conn, finding, values["status"], values["note"], report_id)
    anchor = "finding" if finding["report_id"] == report_id else "carry"
    return redirect(f"/checks/{report_id}#{anchor}-{finding_id}")


@app.post("/checks/{report_id}/findings/{finding_id}/delete")
def finding_delete(report_id: int, finding_id: int):
    with open_db() as conn:
        report = get_report(conn, report_id)
        finding = get_finding_for_report(conn, report, finding_id)
        if finding["report_id"] != report_id:
            raise HTTPException(409, "Only findings logged in this check can be deleted here.")
        conn.execute("DELETE FROM finding WHERE id = ?", (finding_id,))
    return redirect(f"/checks/{report_id}")


# ---------------------------------------------------------------- compare

CLOSED_FINDING_STATUSES = ("fixed", "verified")

# Buckets for "what happened to findings between the two checks", in display order.
FINDING_CHANGE_GROUPS: tuple[tuple[str, str], ...] = (
    ("opened", "Opened"),
    ("recurrence", "Logged as recurrence"),
    ("closed", "Closed (fixed / verified)"),
    ("not_resolved", "Marked not resolved"),
    ("reopened", "Reopened"),
    ("other", "Other updates"),
)


def metric_change(r: sqlite3.Row) -> dict:
    """One compare row: kind is new | missing | changed | same.

    Deltas need both values numeric and the same unit; pct needs a non-zero
    old value. A move away from 0 has no pct and always counts as large.
    """
    row = dict(r)
    row.update(key=r["new_key"] or r["old_key"], kind="same", delta=None, pct=None,
               large=False, unit_changed=False, points=False)
    if r["old_key"] is None:
        row["kind"] = "new"
        return row
    if r["new_key"] is None:
        row["kind"] = "missing"
        return row
    row["unit_changed"] = r["old_unit"].strip().lower() != r["new_unit"].strip().lower()
    old, new = r["old_num"], r["new_num"]
    if old is not None and new is not None and not row["unit_changed"]:
        changed = new != old
        row["delta"] = new - old
        if old != 0:
            row["pct"] = round((new - old) / abs(old) * 100, 6)
        # A change of a percentage is in percentage points, and is judged on
        # those (97% -> 92% is 5 pp but only -5.2% relative).
        row["points"] = any(v.strip() == "%" for v in (r["old_unit"], r["new_unit"])) or any(
            v.strip().endswith("%") for v in (r["old_text"], r["new_text"]))
        if row["points"]:
            row["large"] = changed and round(abs(row["delta"]), 6) >= CHANGE_HIGHLIGHT_PP
        else:
            row["large"] = changed and (row["pct"] is None
                                        or abs(row["pct"]) >= CHANGE_HIGHLIGHT_PERCENT)
    else:
        changed = (row["unit_changed"]
                   or r["old_text"].strip() != r["new_text"].strip())
    if changed:
        row["kind"] = "changed"
    return row


def compare_metrics(conn: sqlite3.Connection, before_id: int, after_id: int) -> list[dict]:
    rows = conn.execute(
        """
        SELECT o.key AS old_key, o.value_text AS old_text, o.value_num AS old_num,
               o.unit AS old_unit, o.note AS old_note,
               n.key AS new_key, n.value_text AS new_text, n.value_num AS new_num,
               n.unit AS new_unit, n.note AS new_note
        FROM (SELECT * FROM metric WHERE report_id = :before) o
        FULL OUTER JOIN (SELECT * FROM metric WHERE report_id = :after) n
          ON lower(o.key) = lower(n.key)
        ORDER BY n.sort_order IS NULL, n.sort_order, o.sort_order,
                 lower(COALESCE(n.key, o.key))
        """,
        {"before": before_id, "after": after_id},
    ).fetchall()
    return [metric_change(r) for r in rows]


def compare_findings(conn: sqlite3.Connection, location_id: int,
                     before: sqlite3.Row, after: sqlite3.Row) -> dict[str, list[dict]]:
    """Net status change per finding over the status events logged during the
    checks after `before`, up to and including `after`."""
    events = conn.execute(
        """
        SELECT e.id, e.finding_id, e.report_id, e.from_status, e.to_status, e.note,
               r.checked_on AS event_checked_on,
               f.category, f.description, f.parent_finding_id, f.status AS current_status,
               f.report_id AS found_report_id
        FROM finding_status_event e
        JOIN check_report r ON r.id = e.report_id
        JOIN finding f      ON f.id = e.finding_id
        WHERE r.location_id = :loc
          AND (r.checked_on, r.id) >  (:b_date, :b_id)
          AND (r.checked_on, r.id) <= (:a_date, :a_id)
        ORDER BY e.finding_id, e.id
        """,
        {"loc": location_id, "b_date": before["checked_on"], "b_id": before["report_id"],
         "a_date": after["checked_on"], "a_id": after["report_id"]},
    ).fetchall()
    by_finding: dict[int, list[sqlite3.Row]] = {}
    for e in events:
        by_finding.setdefault(e["finding_id"], []).append(e)

    groups: dict[str, list[dict]] = {key: [] for key, _ in FINDING_CHANGE_GROUPS}
    for evs in by_finding.values():
        first, last = evs[0], evs[-1]
        start, end = first["from_status"], last["to_status"]
        if start is None:
            group = "recurrence" if first["parent_finding_id"] else "opened"
        elif start not in CLOSED_FINDING_STATUSES and end in CLOSED_FINDING_STATUSES:
            group = "closed"
        elif end == "not_resolved" and start != "not_resolved":
            group = "not_resolved"
        elif start in CLOSED_FINDING_STATUSES and end not in CLOSED_FINDING_STATUSES:
            group = "reopened"
        else:
            group = "other"
        # Status sequence across the window, e.g. open -> fixed -> open.
        path = [start] if start else []
        for e in evs:
            if not path or path[-1] != e["to_status"]:
                path.append(e["to_status"])
        groups[group].append({
            "id": first["finding_id"], "category": first["category"],
            "description": first["description"],
            "parent_finding_id": first["parent_finding_id"],
            "found_report_id": first["found_report_id"],
            "start": start, "end": end, "path": path,
            "last_report_id": last["report_id"], "last_checked_on": last["event_checked_on"],
            "notes": [e["note"] for e in evs if e["note"]],
        })
    return groups


@app.get("/locations/{location_id}/compare", response_class=HTMLResponse)
def compare(request: Request, location_id: int, before: int | None = None,
            after: int | None = None, changes: bool = False):
    with open_db() as conn:
        location = get_location(conn, location_id)
        checks = conn.execute(
            "SELECT * FROM v_check_report_full WHERE location_id = ? "
            "ORDER BY checked_on, report_id", (location_id,)).fetchall()
        context = dict(location=location, checks=list(reversed(checks)), before=None,
                       after=None, metrics=[], groups={}, notice=None, error=None,
                       changes_only=changes, threshold=CHANGE_HIGHLIGHT_PERCENT,
                       threshold_pp=CHANGE_HIGHLIGHT_PP,
                       FINDING_CHANGE_GROUPS=FINDING_CHANGE_GROUPS)
        if len(checks) < 2:
            context["error"] = "Comparing needs at least two checks at this location."
            return render(request, "checks/compare.html", **context)

        picked_both = before is not None and after is not None
        position = {c["report_id"]: i for i, c in enumerate(checks)}
        for picked in (before, after):
            if picked is not None and picked not in position:
                raise HTTPException(404, "Check not found at this location.")
        # Defaults: newest check vs. the one before it; with one side picked,
        # the other is its neighbour.
        if after is None:
            if before is None:
                after = checks[-1]["report_id"]
            else:
                i = position[before]
                after = checks[i + 1 if i + 1 < len(checks) else i - 1]["report_id"]
        if before is None:
            i = position[after]
            before = checks[i - 1 if i > 0 else 1]["report_id"]
        if before == after:
            context.update(before=checks[position[before]], after=checks[position[after]],
                           error="Pick two different checks.")
            return render(request, "checks/compare.html", **context)
        if position[before] > position[after]:
            before, after = after, before
            if picked_both:
                context["notice"] = "Swapped so the earlier check is on the left."

        b, a = checks[position[before]], checks[position[after]]
        metrics = compare_metrics(conn, before, after)
        groups = compare_findings(conn, location_id, b, a)
        summary = {kind: sum(1 for m in metrics if m["kind"] == kind)
                   for kind in ("changed", "new", "missing", "same")}
        summary["large"] = sum(1 for m in metrics if m["large"])
        context.update(before=b, after=a, groups=groups, summary=summary,
                       between=position[after] - position[before] - 1,
                       metrics=[m for m in metrics if m["kind"] != "same"] if changes else metrics)
        return render(request, "checks/compare.html", **context)


# ----------------------------------------------------------------- search

SEARCH_TEXT_FIELDS = ("description", "suspected_cause", "change_made")
NEEDS_ATTENTION = "needs_attention"
SEARCH_STATUS_OPTIONS = ((NEEDS_ATTENTION, "Needs attention (open + not resolved)"),
                         *db.FINDING_STATUSES)

CSV_COLUMNS = (
    ("finding_id", "Finding #"), ("account_name", "Account"), ("location_name", "Location"),
    ("location_code", "Store no."), ("checked_on", "Check date"), ("checked_by", "Checked by"),
    ("report_status", "Check status"), ("category", "Category"), ("status", "Status"),
    ("description", "Description"), ("suspected_cause", "Suspected cause"),
    ("change_made", "Change made"), ("parent_finding_id", "Continues finding #"),
    ("report_id", "Check #"), ("finding_updated_at", "Finding updated (UTC)"),
)
_PLAIN_NUMBER_RE = re.compile(r"[+-]?\d[\d,.]*%?")


def search_terms(q: str) -> list[str]:
    """Split the free text into folded terms; "quoted phrases" stay whole."""
    terms = (fold(phrase or word) for phrase, word in re.findall(r'"([^"]+)"|(\S+)', q))
    return [t for t in terms if t]


def parse_search(q: str, account: str, location: str, category: str, status: str,
                 date_from: str, date_to: str) -> tuple[dict, list[str]]:
    """Normalise the query-string filters. Invalid values are dropped with a message."""
    errors = []
    filters = {
        "q": q.strip(), "terms": search_terms(q),
        "account": int(account) if account.isdigit() else None,
        "location": int(location) if location.isdigit() else None,
        "category": category if category in db.CATEGORY_LABELS else "",
        "status": status if status in dict(SEARCH_STATUS_OPTIONS) else "",
        "date_from": date_from.strip(), "date_to": date_to.strip(),
    }
    for key, label in (("date_from", "From date"), ("date_to", "To date")):
        error = date_error(filters[key], label)
        if error:
            errors.append(error)
            filters[key] = ""
    if filters["date_from"] and filters["date_to"] and filters["date_from"] > filters["date_to"]:
        errors.append("From date must be on or before to date.")
    return filters, errors


def search_where(filters: dict) -> tuple[str, list]:
    """WHERE clause over v_finding_full. Every term must appear (case-insensitive,
    accent-insensitive via fold()) in description, suspected cause or change made."""
    clauses, args = [], []
    for term in filters["terms"]:
        clauses.append("(" + " OR ".join(f"instr(fold({col}), ?) > 0"
                                         for col in SEARCH_TEXT_FIELDS) + ")")
        args += [term] * len(SEARCH_TEXT_FIELDS)
    for key, column in (("account", "account_id"), ("location", "location_id"),
                        ("category", "category")):
        if filters[key]:
            clauses.append(f"{column} = ?")
            args.append(filters[key])
    if filters["status"] == NEEDS_ATTENTION:
        clauses.append("finding_id IN (SELECT finding_id FROM v_open_finding)")
    elif filters["status"]:
        clauses.append("status = ?")
        args.append(filters["status"])
    if filters["date_from"]:
        clauses.append("checked_on >= ?")
        args.append(filters["date_from"])
    if filters["date_to"]:
        clauses.append("checked_on <= ?")
        args.append(filters["date_to"])
    return " AND ".join(clauses) or "1", args


SEARCH_ORDER = "ORDER BY checked_on DESC, finding_id DESC"


def highlight(text: str | None, terms: list[str]) -> Markup:
    """Escape text and wrap search-term matches in <mark>, matching the same
    way as the search (on fold()), then mapping back to the original text."""
    if not text:
        return Markup("")
    if not terms:
        return escape(text)
    folded, origin = [], []  # origin[i] = index in text of folded char i
    for i, ch in enumerate(text):
        f = fold(ch)
        folded.append(f)
        origin.extend([i] * len(f))
    haystack = "".join(folded)
    spans = []
    for term in terms:
        start = haystack.find(term)
        while start != -1:
            spans.append((origin[start], origin[start + len(term) - 1] + 1))
            start = haystack.find(term, start + 1)
    merged: list[list[int]] = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    parts, pos = [], 0
    for start, end in merged:
        parts += [escape(text[pos:start]), Markup("<mark>%s</mark>") % text[start:end]]
        pos = end
    parts.append(escape(text[pos:]))
    return Markup("").join(parts)


templates.env.filters["highlight"] = highlight


@app.get("/findings", response_class=HTMLResponse)
def finding_search(request: Request, q: str = "", account: str = "", location: str = "",
                   category: str = "", status: str = "", date_from: str = "",
                   date_to: str = "", page: str = "1"):
    filters, errors = parse_search(q, account, location, category, status, date_from, date_to)
    where, args = search_where(filters)
    with open_db() as conn:
        total = conn.execute(f"SELECT COUNT(*) FROM v_finding_full WHERE {where}",
                             args).fetchone()[0]
        pages = max(1, -(-total // SEARCH_PAGE_SIZE))
        page = min(max(int(page) if page.isdigit() else 1, 1), pages)
        rows = conn.execute(
            f"SELECT * FROM v_finding_full WHERE {where} {SEARCH_ORDER} LIMIT ? OFFSET ?",
            (*args, SEARCH_PAGE_SIZE, (page - 1) * SEARCH_PAGE_SIZE)).fetchall()
        accounts = conn.execute(
            "SELECT id, name, archived FROM account ORDER BY lower(name)").fetchall()
        locations = conn.execute(
            "SELECT l.id, l.name, l.code, l.archived, a.name AS account_name "
            "FROM location l JOIN account a ON a.id = l.account_id "
            "WHERE ? IS NULL OR l.account_id = ? ORDER BY lower(a.name), lower(l.name)",
            (filters["account"], filters["account"])).fetchall()
    query = {k: v for k, v in request.query_params.items() if k != "page" and v}
    return render(request, "findings/search.html", nav="search", filters=filters,
                  errors=errors, rows=rows, total=total, page=page, pages=pages,
                  accounts=accounts, locations=locations,
                  status_options=SEARCH_STATUS_OPTIONS,
                  query_string=urlencode(query),
                  first_index=(page - 1) * SEARCH_PAGE_SIZE + 1)


def csv_cell(value) -> str:
    """Stringify for CSV; neutralise text Excel would run as a formula."""
    if value is None:
        return ""
    text = str(value)
    if text[:1] in ("=", "+", "-", "@", "\t", "\r") and not _PLAIN_NUMBER_RE.fullmatch(text):
        return "'" + text
    return text


@app.get("/findings.csv")
def finding_search_csv(q: str = "", account: str = "", location: str = "",
                       category: str = "", status: str = "", date_from: str = "",
                       date_to: str = ""):
    filters, _ = parse_search(q, account, location, category, status, date_from, date_to)
    where, args = search_where(filters)
    with open_db() as conn:
        rows = conn.execute(f"SELECT * FROM v_finding_full WHERE {where} {SEARCH_ORDER}",
                            args).fetchall()
    out = io.StringIO()
    writer = csv.writer(out, delimiter=CSV_DELIMITER, lineterminator="\r\n")
    writer.writerow(label for _, label in CSV_COLUMNS)
    labels = {"category": db.CATEGORY_LABELS, "status": db.STATUS_LABELS,
              "report_status": dict(db.REPORT_STATUSES)}
    for row in rows:
        writer.writerow(csv_cell(labels[col].get(row[col], row[col]) if col in labels else row[col])
                        for col, _ in CSV_COLUMNS)
    filename = f"findings-{date.today().isoformat()}.csv"
    # BOM so Excel reads the file as UTF-8 (accented / Turkish characters).
    return Response("﻿" + out.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app:app", host=HOST, port=PORT)
