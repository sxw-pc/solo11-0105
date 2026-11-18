import hashlib
import sqlite3
from pathlib import Path

SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS work_order(id TEXT PRIMARY KEY,equipment TEXT NOT NULL,procedure_version TEXT NOT NULL,status TEXT NOT NULL CHECK(status IN ('待接收','已校验','待核','已判定','作废')));
CREATE TABLE IF NOT EXISTS step(work_order TEXT NOT NULL REFERENCES work_order(id),position INTEGER NOT NULL,code TEXT NOT NULL,required INTEGER NOT NULL,PRIMARY KEY(work_order,position));
CREATE TABLE IF NOT EXISTS instrument(id TEXT PRIMARY KEY,unit TEXT NOT NULL,min REAL NOT NULL,max REAL NOT NULL,calibrated_until TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS stock(part TEXT PRIMARY KEY,issued_to TEXT NOT NULL REFERENCES work_order(id));
CREATE TABLE IF NOT EXISTS source(id INTEGER PRIMARY KEY,name TEXT NOT NULL,sha256 TEXT NOT NULL UNIQUE,raw BLOB NOT NULL);
CREATE TABLE IF NOT EXISTS event(id INTEGER PRIMARY KEY,source_id INTEGER NOT NULL REFERENCES source(id),line_no INTEGER NOT NULL,device TEXT NOT NULL,seq INTEGER NOT NULL,work_order TEXT NOT NULL,kind TEXT NOT NULL,at TEXT NOT NULL,payload TEXT NOT NULL,sha256 TEXT NOT NULL,UNIQUE(device,seq));
CREATE TABLE IF NOT EXISTS issue(id INTEGER PRIMARY KEY,code TEXT NOT NULL,work_order TEXT NOT NULL,event_id INTEGER REFERENCES event(id),detail TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS decision(work_order TEXT PRIMARY KEY REFERENCES work_order(id),status TEXT NOT NULL,detail TEXT NOT NULL,rule_version TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS evidence(id INTEGER PRIMARY KEY, event_id INTEGER NOT NULL REFERENCES event(id), source_sha256 TEXT NOT NULL, payload_sha256 TEXT NOT NULL, role TEXT NOT NULL, UNIQUE(event_id,role));
CREATE TABLE IF NOT EXISTS procurement_case(id TEXT PRIMARY KEY,payload TEXT NOT NULL,digest TEXT NOT NULL,payload_sha256 TEXT);
CREATE TABLE IF NOT EXISTS run(id INTEGER PRIMARY KEY, status TEXT NOT NULL CHECK(status IN ('待接收','已校验','待核','已判定','作废')), started_at TEXT NOT NULL, ended_at TEXT);
CREATE TABLE IF NOT EXISTS schema_migration(id TEXT PRIMARY KEY, applied_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS procurement_case_revision(
  id INTEGER PRIMARY KEY,
  case_id TEXT NOT NULL REFERENCES procurement_case(id),
  previous_digest TEXT NOT NULL,
  previous_payload_sha256 TEXT,
  new_digest TEXT NOT NULL,
  new_payload_sha256 TEXT NOT NULL,
  recorded_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS procurement_plan(
  id INTEGER PRIMARY KEY,
  case_id TEXT NOT NULL REFERENCES procurement_case(id),
  contract TEXT NOT NULL CHECK(contract IN ('min_total_cost','min_max_lateness')),
  input_digest TEXT NOT NULL,
  solution_sha256 TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('issued','superseded')) DEFAULT 'issued',
  total_cost_cents INTEGER NOT NULL,
  max_lateness_days INTEGER NOT NULL,
  objective TEXT NOT NULL,
  evidence TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_plan_issued
  ON procurement_plan(case_id,contract) WHERE status='issued';
CREATE TABLE IF NOT EXISTS procurement_order(
  id INTEGER PRIMARY KEY,
  plan_id INTEGER NOT NULL REFERENCES procurement_plan(id),
  vendor TEXT NOT NULL,
  work_order TEXT NOT NULL,
  contract TEXT NOT NULL CHECK(contract IN ('min_total_cost','min_max_lateness')),
  total_cents INTEGER NOT NULL,
  min_order_cents INTEGER NOT NULL,
  latest_arrival_day INTEGER NOT NULL,
  quote_sha256 TEXT NOT NULL,
  case_payload_sha256 TEXT NOT NULL,
  consumption_event_id INTEGER NOT NULL REFERENCES event(id),
  consumption_source_sha256 TEXT NOT NULL,
  consumption_payload_sha256 TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(plan_id,vendor)
);
CREATE TABLE IF NOT EXISTS procurement_order_line(
  id INTEGER PRIMARY KEY,
  order_id INTEGER NOT NULL REFERENCES procurement_order(id),
  sku TEXT NOT NULL,
  qty INTEGER NOT NULL CHECK(qty>0),
  unit_cents INTEGER NOT NULL CHECK(unit_cents>0),
  line_cents INTEGER NOT NULL,
  arrival_day INTEGER NOT NULL,
  deadline_day INTEGER NOT NULL,
  lateness_days INTEGER NOT NULL,
  available_qty INTEGER NOT NULL,
  quote_row_sha256 TEXT NOT NULL,
  quote_row TEXT NOT NULL,
  UNIQUE(order_id,sku)
);
CREATE TABLE IF NOT EXISTS procurement_order_retirement(
  order_id INTEGER PRIMARY KEY REFERENCES procurement_order(id),
  plan_id INTEGER NOT NULL REFERENCES procurement_plan(id),
  reason TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_plan_case ON procurement_plan(case_id);
CREATE INDEX IF NOT EXISTS idx_order_plan ON procurement_order(plan_id);
CREATE INDEX IF NOT EXISTS idx_line_order ON procurement_order_line(order_id);
CREATE TRIGGER IF NOT EXISTS procurement_order_no_update
BEFORE UPDATE ON procurement_order
BEGIN SELECT RAISE(ABORT,'issued_order_immutable'); END;
CREATE TRIGGER IF NOT EXISTS procurement_order_no_delete
BEFORE DELETE ON procurement_order
BEGIN SELECT RAISE(ABORT,'issued_order_immutable'); END;
CREATE TRIGGER IF NOT EXISTS procurement_order_line_no_update
BEFORE UPDATE ON procurement_order_line
BEGIN SELECT RAISE(ABORT,'issued_order_line_immutable'); END;
CREATE TRIGGER IF NOT EXISTS procurement_order_line_no_delete
BEFORE DELETE ON procurement_order_line
BEGIN SELECT RAISE(ABORT,'issued_order_line_immutable'); END;
"""


def _migrate(db):
    """Apply additive migrations to databases created by the seed version."""
    columns = {row["name"] for row in db.execute("PRAGMA table_info(procurement_case)")}
    if "payload_sha256" not in columns:
        db.execute("ALTER TABLE procurement_case ADD COLUMN payload_sha256 TEXT")
        db.execute(
            "INSERT OR IGNORE INTO schema_migration(id,applied_at) VALUES (?,CURRENT_TIMESTAMP)",
            ("procurement_case.payload_sha256",),
        )
    plan_sql = db.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='procurement_plan'"
    ).fetchone()
    if plan_sql and "UNIQUE(case_id,contract)" in (plan_sql["sql"] or ""):
        # The seed created procurement_plan with a table-level UNIQUE that would
        # prevent keeping superseded plans.  Rebuild it with a partial unique
        # index over issued plans only.
        db.executescript(
            """
            PRAGMA foreign_keys=OFF;
            BEGIN;
            CREATE TABLE procurement_plan_new(
              id INTEGER PRIMARY KEY,
              case_id TEXT NOT NULL REFERENCES procurement_case(id),
              contract TEXT NOT NULL CHECK(contract IN ('min_total_cost','min_max_lateness')),
              input_digest TEXT NOT NULL,
              solution_sha256 TEXT NOT NULL,
              status TEXT NOT NULL CHECK(status IN ('issued','superseded')) DEFAULT 'issued',
              total_cost_cents INTEGER NOT NULL,
              max_lateness_days INTEGER NOT NULL,
              objective TEXT NOT NULL,
              evidence TEXT NOT NULL,
              created_at TEXT NOT NULL
            );
            INSERT INTO procurement_plan_new SELECT * FROM procurement_plan;
            DROP TABLE procurement_plan;
            ALTER TABLE procurement_plan_new RENAME TO procurement_plan;
            CREATE UNIQUE INDEX idx_plan_issued
              ON procurement_plan(case_id,contract) WHERE status='issued';
            CREATE INDEX idx_plan_case ON procurement_plan(case_id);
            COMMIT;
            PRAGMA foreign_keys=ON;
            """
        )
        db.execute(
            "INSERT OR IGNORE INTO schema_migration(id,applied_at) VALUES (?,CURRENT_TIMESTAMP)",
            ("procurement_plan.partial_issued_unique",),
        )
    # A seed-version row was frozen before the payload hash column existed.
    # Hashing the stored payload is safe: the payload itself is not modified.
    rows = db.execute(
        "SELECT id,payload FROM procurement_case WHERE payload_sha256 IS NULL"
    ).fetchall()
    for row in rows:
        digest = hashlib.sha256(row["payload"].encode("utf-8")).hexdigest()
        db.execute(
            "UPDATE procurement_case SET payload_sha256=? WHERE id=?",
            (digest, row["id"]),
        )
    db.commit()


def connect(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    _migrate(db)
    return db
