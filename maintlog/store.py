import sqlite3
from pathlib import Path
SCHEMA="""
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
CREATE TABLE IF NOT EXISTS waste_container(id TEXT PRIMARY KEY,work_order TEXT NOT NULL REFERENCES work_order(id),gross_kg TEXT NOT NULL,contaminant_kg TEXT NOT NULL,seal TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS waste_case(id TEXT PRIMARY KEY,payload TEXT NOT NULL,digest TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS run(id INTEGER PRIMARY KEY, status TEXT NOT NULL CHECK(status IN ('待接收','已校验','待核','已判定','作废')), started_at TEXT NOT NULL, ended_at);
"""
# Versioned migrations. user_version 0 == frozen seed schema above.
MIGRATIONS={
1:"""
ALTER TABLE waste_container ADD COLUMN inspection_batch TEXT;
CREATE TABLE IF NOT EXISTS waste_contract(
  case_id TEXT NOT NULL,
  contract_id TEXT NOT NULL,
  route TEXT NOT NULL CHECK(route IN ('closed_loop_sep','sealed_ship')),
  authorized_by TEXT NOT NULL,
  cert_id TEXT,
  cert_valid_until TEXT,
  carrier_license TEXT,
  license_valid_until TEXT,
  manifest_id TEXT,
  payload TEXT NOT NULL,
  PRIMARY KEY(case_id,contract_id)
);
CREATE TABLE IF NOT EXISTS material_batch(
  id TEXT PRIMARY KEY,
  case_id TEXT NOT NULL,
  work_order TEXT NOT NULL,
  kind TEXT NOT NULL CHECK(kind IN ('sealed_parent','reuse_fluid','hazard_residue')),
  parent_id TEXT,
  container TEXT NOT NULL,
  seal TEXT,
  inspection_batch TEXT NOT NULL,
  gross_kg TEXT NOT NULL,
  contaminant_kg TEXT NOT NULL,
  remaining_kg TEXT NOT NULL,
  remaining_contaminant_kg TEXT NOT NULL,
  custody TEXT NOT NULL CHECK(custody IN ('现场封存','待运','已交接承运方','已消耗分离','待核')),
  hazard_class TEXT NOT NULL CHECK(hazard_class IN ('危废','非危废')),
  source_sha256 TEXT NOT NULL,
  created_event_id INTEGER,
  last_event_id INTEGER,
  UNIQUE(case_id,container,kind)
);
CREATE TABLE IF NOT EXISTS material_event(
  id INTEGER PRIMARY KEY,
  case_id TEXT NOT NULL,
  at TEXT NOT NULL,
  event_type TEXT NOT NULL CHECK(event_type IN ('case_freeze','hold_pending_review','route_lock','separation','shipment_signoff','reinspection','carrier_return','correction')),
  contract_id TEXT,
  batch_id TEXT,
  container TEXT,
  gross_delta TEXT NOT NULL DEFAULT '0.000',
  contaminant_delta TEXT NOT NULL DEFAULT '0.000',
  detail TEXT NOT NULL,
  digest TEXT NOT NULL,
  UNIQUE(case_id,digest)
);
CREATE TABLE IF NOT EXISTS waste_manifest(
  id INTEGER PRIMARY KEY,
  case_id TEXT NOT NULL,
  manifest_id TEXT NOT NULL,
  carrier_license TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('待签收','部分签收','完全签收','回退待核')),
  detail TEXT NOT NULL,
  UNIQUE(case_id,manifest_id)
);
CREATE TABLE IF NOT EXISTS manifest_line(
  id INTEGER PRIMARY KEY,
  manifest_id INTEGER NOT NULL REFERENCES waste_manifest(id),
  container TEXT NOT NULL,
  batch_id TEXT NOT NULL,
  seal TEXT NOT NULL,
  sealed_gross_kg TEXT NOT NULL,
  signed_kg TEXT NOT NULL DEFAULT '0.000',
  unsigned_kg TEXT NOT NULL,
  signed_contaminant_kg TEXT NOT NULL DEFAULT '0.000',
  status TEXT NOT NULL CHECK(status IN ('待签收','已签收','拒收','部分签收','回退')),
  receipt_id TEXT,
  signed_by TEXT,
  signed_at TEXT,
  UNIQUE(manifest_id,container)
);
CREATE TABLE IF NOT EXISTS waste_verification(
  id INTEGER PRIMARY KEY,
  case_id TEXT NOT NULL,
  batch_id TEXT NOT NULL,
  kind TEXT NOT NULL CHECK(kind IN ('现场复验','分离复验')),
  result TEXT NOT NULL CHECK(result IN ('pass','fail')),
  inspector TEXT NOT NULL,
  at TEXT NOT NULL,
  voucher_id TEXT NOT NULL,
  detail TEXT NOT NULL,
  UNIQUE(case_id,voucher_id)
);
""",
}
def connect(path):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    db=sqlite3.connect(path);db.row_factory=sqlite3.Row
    db.execute('PRAGMA foreign_keys=ON')
    db.executescript(SCHEMA)
    version=db.execute('PRAGMA user_version').fetchone()[0]
    for ver in sorted(MIGRATIONS):
        if version<ver:
            db.executescript(MIGRATIONS[ver]);db.execute(f'PRAGMA user_version={ver}')
            version=ver
    return db
