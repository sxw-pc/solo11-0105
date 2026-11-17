from pathlib import Path
import pytest
from maintlog.__main__ import initialize
from maintlog.ingest import ingest
from maintlog.rules import evaluate
from maintlog.procurement import register
from maintlog.store import connect
FIX=Path(__file__).resolve().parents[1]/'fixtures'
def test_procurement_freezes_consumption_and_quotes(tmp_path):
    db=tmp_path/'procurement.db';initialize(db,FIX/'config.json')
    assert ingest(FIX/'events.jsonl',db)['imported']==6
    assert evaluate(db)=={'WO-1':'完成'}
    got=register(db,'BUY-1',FIX/'procurement-case.json')
    assert got['quotes']==4
    assert register(db,'BUY-1',FIX/'procurement-case.json')==got
    c=connect(db)
    try:assert c.execute('SELECT COUNT(*) FROM procurement_case').fetchone()[0]==1
    finally:c.close()
def test_procurement_requires_completed_consumption(tmp_path):
    db=tmp_path/'procurement.db';initialize(db,FIX/'config.json')
    with pytest.raises(ValueError,match='consumption_order_not_complete'):
        register(db,'BUY-1',FIX/'procurement-case.json')
