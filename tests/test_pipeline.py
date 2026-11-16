import json
from pathlib import Path
from maintlog.__main__ import demo,initialize
from maintlog.ingest import ingest
from maintlog.rules import evaluate
from maintlog.store import connect
FIX=Path(__file__).resolve().parent.parent/'fixtures'
def setup(tmp_path):
    db=tmp_path/'x.db';initialize(db,FIX/'config.json');return db
def events():return [json.loads(x) for x in (FIX/'events.jsonl').read_text().splitlines()]
def add(tmp_path,db,rows):
    p=tmp_path/'e.jsonl';p.write_text('\n'.join(json.dumps(r) for r in rows),encoding='utf-8');return ingest(p,db)
def test_demo(tmp_path):
    data=json.loads(demo(tmp_path).read_text(encoding='utf-8'));assert data['decision'][0]['status']=='完成';assert data['source'][0]['byte_length']>0
def test_duplicate(tmp_path):
    db=setup(tmp_path);assert ingest(FIX/'events.jsonl',db)['imported']==6;assert ingest(FIX/'events.jsonl',db)['duplicate_file']
def test_missing_evidence(tmp_path):
    db=setup(tmp_path);assert evaluate(db)['WO-1']=='证据不足'
def test_step_order(tmp_path):
    db=setup(tmp_path);rows=events();rows[1]['position']=2;add(tmp_path,db,rows);assert evaluate(db)['WO-1']=='需返工'
def test_reading_calibration(tmp_path):
    db=setup(tmp_path);rows=events();rows[2]['at']='2028-01-01T08:10:00+00:00';add(tmp_path,db,rows);assert evaluate(db)['WO-1']=='需返工'
def test_part_issue(tmp_path):
    db=setup(tmp_path);rows=events();rows[3]['new']='UNISSUED';add(tmp_path,db,rows);assert evaluate(db)['WO-1']=='需返工'
def test_pause_without_resume(tmp_path):
    db=setup(tmp_path);rows=events();rows[2]['kind']='pause';add(tmp_path,db,rows);assert evaluate(db)['WO-1']=='需返工'
def test_identity_conflict(tmp_path):
    db=setup(tmp_path);ingest(FIX/'events.jsonl',db);rows=events();rows[1]['result']='fail';add(tmp_path,db,rows)
    c=connect(db);assert c.execute("SELECT count(*) FROM issue WHERE code='identity_conflict'").fetchone()[0]==1
def test_void(tmp_path):
    db=setup(tmp_path);rows=events()[:1];rows.append({'kind':'void','device':'T1','seq':2,'work_order':'WO-1','at':'2026-01-01T08:01:00+00:00'});add(tmp_path,db,rows);assert evaluate(db)['WO-1']=='作废'
