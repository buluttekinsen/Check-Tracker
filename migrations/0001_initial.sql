-- Regular Check Tracker -- migration 0001: the initial schema.
--
-- Applied once per database by db.init_db() (see db.py). Never edit this file
-- after it has been shared: change the schema in a new numbered migration.
-- Every statement here must stay idempotent (IF NOT EXISTS / DROP IF EXISTS):
-- databases created before schema versioning existed run it as their baseline.
--
-- Hierarchy:  account -> location -> check_report -> {metric, finding}
-- All timestamps are UTC ISO-8601 text (SQLite CURRENT_TIMESTAMP).
-- Dates chosen by the user (check_report.checked_on) are plain 'YYYY-MM-DD'.

-- ---------------------------------------------------------------- accounts

CREATE TABLE IF NOT EXISTS account (
    id          INTEGER PRIMARY KEY,
    name        TEXT    NOT NULL,
    notes       TEXT    NOT NULL DEFAULT '',
    archived    INTEGER NOT NULL DEFAULT 0 CHECK (archived IN (0, 1)),
    created_at  TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at  TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (trim(name) <> '')
);

-- Case-insensitive uniqueness of account names.
CREATE UNIQUE INDEX IF NOT EXISTS ux_account_name
    ON account (lower(name));

-- --------------------------------------------------------------- locations

CREATE TABLE IF NOT EXISTS location (
    id          INTEGER PRIMARY KEY,
    account_id  INTEGER NOT NULL REFERENCES account (id) ON DELETE CASCADE,
    name        TEXT    NOT NULL,
    code        TEXT    NOT NULL DEFAULT '',   -- store number / external ref
    notes       TEXT    NOT NULL DEFAULT '',
    archived    INTEGER NOT NULL DEFAULT 0 CHECK (archived IN (0, 1)),
    created_at  TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at  TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (trim(name) <> '')
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_location_account_name
    ON location (account_id, lower(name));
CREATE INDEX IF NOT EXISTS ix_location_account
    ON location (account_id);

-- ----------------------------------------------------------- check reports

CREATE TABLE IF NOT EXISTS check_report (
    id           INTEGER PRIMARY KEY,
    location_id  INTEGER NOT NULL REFERENCES location (id) ON DELETE CASCADE,
    checked_on   TEXT    NOT NULL,               -- 'YYYY-MM-DD', the check date
    checked_by   TEXT    NOT NULL DEFAULT '',
    -- optional data window the check covers (may differ from checked_on)
    period_start TEXT,
    period_end   TEXT,
    summary      TEXT    NOT NULL DEFAULT '',
    status       TEXT    NOT NULL DEFAULT 'draft'
                 CHECK (status IN ('draft', 'complete')),
    created_at   TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at   TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (checked_on GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    CHECK (period_start IS NULL OR period_start GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    CHECK (period_end   IS NULL OR period_end   GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    CHECK (period_start IS NULL OR period_end IS NULL OR period_start <= period_end)
);

-- Drives "previous check for this location" and the compare view.
-- Ties on checked_on are broken by id (later id = later check).
CREATE INDEX IF NOT EXISTS ix_check_report_location_date
    ON check_report (location_id, checked_on, id);

-- ------------------------------------------------- metrics (flexible k/v)

CREATE TABLE IF NOT EXISTS metric (
    id         INTEGER PRIMARY KEY,
    report_id  INTEGER NOT NULL REFERENCES check_report (id) ON DELETE CASCADE,
    key        TEXT    NOT NULL,
    -- value_text always holds what the user typed; value_num is filled in by
    -- the app when the value parses as a number, so the compare view can show
    -- deltas and the search can filter on ranges.
    value_text TEXT    NOT NULL DEFAULT '',
    value_num  REAL,
    unit       TEXT    NOT NULL DEFAULT '',
    note       TEXT    NOT NULL DEFAULT '',
    sort_order INTEGER NOT NULL DEFAULT 0,
    CHECK (trim(key) <> '')
);

-- One value per key per report; keys match across reports by lower(key),
-- which is how the compare view lines metrics up.
CREATE UNIQUE INDEX IF NOT EXISTS ux_metric_report_key
    ON metric (report_id, lower(key));
CREATE INDEX IF NOT EXISTS ix_metric_key
    ON metric (lower(key));

-- ---------------------------------------------------------------- findings

CREATE TABLE IF NOT EXISTS finding (
    id              INTEGER PRIMARY KEY,
    report_id       INTEGER NOT NULL REFERENCES check_report (id) ON DELETE CASCADE,
    category        TEXT    NOT NULL
                    CHECK (category IN ('llm_scoring_error', 'config_error', 'imbalance',
                                        'conversion_anomaly', 'other')),
    description     TEXT    NOT NULL DEFAULT '',
    suspected_cause TEXT    NOT NULL DEFAULT '',
    change_made     TEXT    NOT NULL DEFAULT '',
    status          TEXT    NOT NULL DEFAULT 'open'
                    CHECK (status IN ('open', 'fixed', 'verified', 'not_resolved')),
    -- Carry-over link: this finding continues an earlier one (recurrence or
    -- follow-up). ON DELETE SET NULL keeps the newer finding if the older
    -- report is deleted.
    parent_finding_id INTEGER REFERENCES finding (id) ON DELETE SET NULL,
    created_at      TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at      TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (parent_finding_id IS NULL OR parent_finding_id <> id)
);

CREATE INDEX IF NOT EXISTS ix_finding_report   ON finding (report_id);
CREATE INDEX IF NOT EXISTS ix_finding_status   ON finding (status);
CREATE INDEX IF NOT EXISTS ix_finding_category ON finding (category);
CREATE INDEX IF NOT EXISTS ix_finding_parent   ON finding (parent_finding_id);

-- Audit trail of status changes. A row records *which check* the change was
-- recorded during, so "updated an old finding while logging a new check"
-- stays traceable.
CREATE TABLE IF NOT EXISTS finding_status_event (
    id          INTEGER PRIMARY KEY,
    finding_id  INTEGER NOT NULL REFERENCES finding (id) ON DELETE CASCADE,
    report_id   INTEGER REFERENCES check_report (id) ON DELETE SET NULL,
    from_status TEXT,
    to_status   TEXT    NOT NULL
                CHECK (to_status IN ('open', 'fixed', 'verified', 'not_resolved')),
    note        TEXT    NOT NULL DEFAULT '',
    created_at  TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS ix_fse_finding ON finding_status_event (finding_id, id);
CREATE INDEX IF NOT EXISTS ix_fse_report  ON finding_status_event (report_id);

-- ---------------------------------------------------- updated_at triggers

CREATE TRIGGER IF NOT EXISTS tr_account_touch
AFTER UPDATE ON account FOR EACH ROW WHEN NEW.updated_at = OLD.updated_at
BEGIN
    UPDATE account SET updated_at = CURRENT_TIMESTAMP WHERE id = NEW.id;
END;

CREATE TRIGGER IF NOT EXISTS tr_location_touch
AFTER UPDATE ON location FOR EACH ROW WHEN NEW.updated_at = OLD.updated_at
BEGIN
    UPDATE location SET updated_at = CURRENT_TIMESTAMP WHERE id = NEW.id;
END;

CREATE TRIGGER IF NOT EXISTS tr_check_report_touch
AFTER UPDATE ON check_report FOR EACH ROW WHEN NEW.updated_at = OLD.updated_at
BEGIN
    UPDATE check_report SET updated_at = CURRENT_TIMESTAMP WHERE id = NEW.id;
END;

CREATE TRIGGER IF NOT EXISTS tr_finding_touch
AFTER UPDATE ON finding FOR EACH ROW WHEN NEW.updated_at = OLD.updated_at
BEGIN
    UPDATE finding SET updated_at = CURRENT_TIMESTAMP WHERE id = NEW.id;
END;

-- A carry-over link must stay inside the same location.
CREATE TRIGGER IF NOT EXISTS tr_finding_parent_same_location_ins
AFTER INSERT ON finding FOR EACH ROW WHEN NEW.parent_finding_id IS NOT NULL
BEGIN
    SELECT CASE WHEN (
        SELECT r.location_id FROM check_report r WHERE r.id = NEW.report_id
    ) <> (
        SELECT pr.location_id FROM finding p
        JOIN check_report pr ON pr.id = p.report_id
        WHERE p.id = NEW.parent_finding_id
    ) THEN RAISE(ABORT, 'parent finding belongs to a different location') END;
END;

CREATE TRIGGER IF NOT EXISTS tr_finding_parent_same_location_upd
AFTER UPDATE OF parent_finding_id, report_id ON finding
FOR EACH ROW WHEN NEW.parent_finding_id IS NOT NULL
BEGIN
    SELECT CASE WHEN (
        SELECT r.location_id FROM check_report r WHERE r.id = NEW.report_id
    ) <> (
        SELECT pr.location_id FROM finding p
        JOIN check_report pr ON pr.id = p.report_id
        WHERE p.id = NEW.parent_finding_id
    ) THEN RAISE(ABORT, 'parent finding belongs to a different location') END;
END;

-- ------------------------------------------------------------------- views
--
-- A changed view definition goes in a new migration: DROP VIEW IF EXISTS
-- (dependents first) then CREATE VIEW.

DROP VIEW IF EXISTS v_check_report_full;
DROP VIEW IF EXISTS v_open_finding;

-- THE definition of "finding still needs attention", used for every open
-- count and the carry-over panel: status open/not_resolved (keep in sync with
-- db.OPEN_FINDING_STATUSES) and not superseded by a carry-over child, so a
-- chain counts once.
CREATE VIEW v_open_finding AS
SELECT
    f.id          AS finding_id,
    f.report_id,
    r.location_id,
    r.checked_on,
    f.category,
    f.status
FROM finding f
JOIN check_report r ON r.id = f.report_id
WHERE f.status IN ('open', 'not_resolved')
  AND NOT EXISTS (SELECT 1 FROM finding c WHERE c.parent_finding_id = f.id);

-- Flattened finding rows for the global search / filter screen.
CREATE VIEW IF NOT EXISTS v_finding_full AS
SELECT
    f.id                AS finding_id,
    f.category,
    f.description,
    f.suspected_cause,
    f.change_made,
    f.status,
    f.parent_finding_id,
    f.created_at        AS finding_created_at,
    f.updated_at        AS finding_updated_at,
    r.id                AS report_id,
    r.checked_on,
    r.checked_by,
    r.status            AS report_status,
    l.id                AS location_id,
    l.name              AS location_name,
    l.code              AS location_code,
    l.archived          AS location_archived,
    a.id                AS account_id,
    a.name              AS account_name,
    a.archived          AS account_archived
FROM finding f
JOIN check_report r ON r.id = f.report_id
JOIN location     l ON l.id = r.location_id
JOIN account      a ON a.id = l.account_id;

-- Report rows with their location/account and the id of the preceding check
-- at the same location (NULL for the first check) -- used by the compare view.
CREATE VIEW v_check_report_full AS
SELECT
    r.id          AS report_id,
    r.checked_on,
    r.checked_by,
    r.period_start,
    r.period_end,
    r.summary,
    r.status      AS report_status,
    r.created_at,
    r.updated_at,
    l.id          AS location_id,
    l.name        AS location_name,
    l.code        AS location_code,
    a.id          AS account_id,
    a.name        AS account_name,
    (SELECT p.id FROM check_report p
      WHERE p.location_id = r.location_id
        AND (p.checked_on < r.checked_on
             OR (p.checked_on = r.checked_on AND p.id < r.id))
      ORDER BY p.checked_on DESC, p.id DESC
      LIMIT 1)    AS prev_report_id,
    (SELECT COUNT(*) FROM metric m  WHERE m.report_id = r.id)  AS metric_count,
    (SELECT COUNT(*) FROM finding f WHERE f.report_id = r.id)  AS finding_count,
    (SELECT COUNT(*) FROM v_open_finding o
      WHERE o.report_id = r.id)                                AS open_finding_count
FROM check_report r
JOIN location l ON l.id = r.location_id
JOIN account  a ON a.id = l.account_id;
