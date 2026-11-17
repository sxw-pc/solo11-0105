from pathlib import Path
from maintlog.__main__ import initialize
from maintlog.ingest import ingest
from maintlog.rules import evaluate
from maintlog.leak_case import register
from maintlog.store import connect
import pytest
FIX=Path(__file__).resolve().parents[1]/'fixtures'
def test_leak_trial_registers_four_completed_readings(tmp_path):
    db=tmp_path/'leak.db';initialize(db,FIX/'leak-config.json')
    assert ingest(FIX/'leak-events.jsonl',db)['imported']==8
    assert evaluate(db)=={'WO-L':'完成'}
    got=register(db,'leak-01',FIX/'leak-case.json')
    assert got['readings']==4
    assert register(db,'leak-01',FIX/'leak-case.json')==got
    c=connect(db)
    try:assert c.execute('SELECT COUNT(*) FROM leak_case').fetchone()[0]==1
    finally:c.close()
def test_leak_case_requires_completed_order(tmp_path):
    db=tmp_path/'leak.db';initialize(db,FIX/'leak-config.json')
    with pytest.raises(ValueError,match='leak_order_not_complete'):register(db,'leak-01',FIX/'leak-case.json')
