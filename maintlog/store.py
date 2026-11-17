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
CREATE TABLE IF NOT EXISTS technician_privacy_case(id TEXT PRIMARY KEY,payload TEXT NOT NULL,digest TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS published_tech_stat(id TEXT PRIMARY KEY,audience TEXT NOT NULL,members TEXT NOT NULL,reading_count INTEGER NOT NULL,source_sha256 TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS run(id INTEGER PRIMARY KEY, status TEXT NOT NULL CHECK(status IN ('待接收','已校验','待核','已判定','作废')), started_at TEXT NOT NULL, ended_at TEXT);
"""
def connect(path):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True);db=sqlite3.connect(path);db.row_factory=sqlite3.Row;db.executescript(SCHEMA);return db
