from pathlib import Path
import pytest
from maintlog.__main__ import initialize
from maintlog.ingest import ingest
from maintlog.rules import evaluate
from maintlog.waste_disposition import register
from maintlog.store import connect
FIX=Path(__file__).resolve().parents[1]/'fixtures'
def test_waste_freezes_completed_service_and_containers(tmp_path):
    db=tmp_path/'waste.db';initialize(db,FIX/'config.json')
    assert ingest(FIX/'events.jsonl',db)['imported']==6
    assert evaluate(db)=={'WO-1':'完成'}
    got=register(db,'WASTE-1',FIX/'waste-case.json')
    assert got['containers']==2
    assert register(db,'WASTE-1',FIX/'waste-case.json')==got
    c=connect(db)
    try:assert c.execute('SELECT COUNT(*) FROM waste_container').fetchone()[0]==2
    finally:c.close()
def test_waste_requires_completed_service(tmp_path):
    db=tmp_path/'waste.db';initialize(db,FIX/'config.json')
    with pytest.raises(ValueError,match='waste_order_not_complete'):
        register(db,'WASTE-1',FIX/'waste-case.json')
