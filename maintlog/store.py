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
CREATE TABLE IF NOT EXISTS issued_invoice(id TEXT PRIMARY KEY,work_order TEXT NOT NULL REFERENCES work_order(id),customer_due_cents INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS warranty_case(id TEXT PRIMARY KEY,payload TEXT NOT NULL,digest TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS run(id INTEGER PRIMARY KEY, status TEXT NOT NULL CHECK(status IN ('待接收','已校验','待核','已判定','作废')), started_at TEXT NOT NULL, ended_at TEXT);
CREATE TABLE IF NOT EXISTS settlement_stream(work_order TEXT NOT NULL,contract_id TEXT NOT NULL,case_id TEXT NOT NULL,status TEXT NOT NULL CHECK(status IN ('待核','已出账')),latest_revision INTEGER,snapshot_digest TEXT,PRIMARY KEY(work_order,contract_id));
CREATE TABLE IF NOT EXISTS settlement_revision(id INTEGER PRIMARY KEY,work_order TEXT NOT NULL,contract_id TEXT NOT NULL,revision_no INTEGER NOT NULL,case_id TEXT NOT NULL,status TEXT NOT NULL CHECK(status IN ('待核','已出账')),invoice_id TEXT,invoice_customer_due_cents INTEGER,deductible_cents INTEGER,gross_total_cents INTEGER,customer_due_cents INTEGER,insurer_due_cents INTEGER,snapshot_digest TEXT NOT NULL,reasons_json TEXT NOT NULL,created_at TEXT NOT NULL,UNIQUE(work_order,contract_id,revision_no));
CREATE TABLE IF NOT EXISTS settlement_line(revision_id INTEGER NOT NULL REFERENCES settlement_revision(id),line_no INTEGER NOT NULL,kind TEXT NOT NULL,item TEXT NOT NULL,gross_cents INTEGER NOT NULL,covered INTEGER NOT NULL,customer_cents INTEGER NOT NULL,insurer_cents INTEGER NOT NULL,PRIMARY KEY(revision_id,line_no));
CREATE TABLE IF NOT EXISTS settlement_entry(id INTEGER PRIMARY KEY,work_order TEXT NOT NULL,contract_id TEXT NOT NULL,revision_id INTEGER NOT NULL REFERENCES settlement_revision(id),entry_type TEXT NOT NULL CHECK(entry_type IN ('customer_adjustment','insurer_receivable','reversal')),party TEXT NOT NULL CHECK(party IN ('customer','insurer')),amount_cents INTEGER NOT NULL,line_no INTEGER,detail TEXT NOT NULL,created_at TEXT NOT NULL);
"""
def connect(path):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True);db=sqlite3.connect(path);db.row_factory=sqlite3.Row;db.executescript(SCHEMA);return db
