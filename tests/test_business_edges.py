import json
from pathlib import Path
from maintlog.__main__ import initialize,demo
from maintlog.ingest import ingest
from maintlog.rules import evaluate
from maintlog.store import connect
FIX=Path(__file__).resolve().parent.parent/'fixtures'
def setup(tmp):
    db=tmp/'x.db';initialize(db,FIX/'config.json');return db
def rows():return [json.loads(x) for x in (FIX/'events.jsonl').read_text().splitlines()]
def add(tmp,db,rr):
    p=tmp/'e.jsonl';p.write_text('\n'.join(json.dumps(r) for r in rr),encoding='utf-8');ingest(p,db)
def test_wrong_procedure_version(tmp_path):
    db=setup(tmp_path);rr=rows();rr[1]['procedure_version']='old';add(tmp_path,db,rr)
    assert evaluate(db)['WO-1']=='需返工'
def test_fahrenheit_conversion(tmp_path):
    db=setup(tmp_path);rr=rows();rr[2]['unit']='F';rr[2]['value']=77;add(tmp_path,db,rr)
    assert evaluate(db)['WO-1']=='完成'
def test_invalid_unit(tmp_path):
    db=setup(tmp_path);rr=rows();rr[2]['unit']='unknown';add(tmp_path,db,rr)
    assert evaluate(db)['WO-1']=='证据不足'
def test_sequence_gap(tmp_path):
    db=setup(tmp_path);rr=rows();rr[-1]['seq']=7;add(tmp_path,db,rr)
    assert evaluate(db)['WO-1']=='证据不足'
def test_history_includes_signature_and_part(tmp_path):
    data=json.loads(demo(tmp_path).read_text(encoding='utf-8'));detail=json.loads(data['decision'][0]['detail'])
    assert detail['signature_ids'] and detail['replacement_ids'] and detail['reading_ids']
