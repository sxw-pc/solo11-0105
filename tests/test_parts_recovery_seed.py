from pathlib import Path
import pytest
from maintlog.__main__ import initialize
from maintlog.ingest import ingest
from maintlog.rules import evaluate
from maintlog.parts_recovery import register
from maintlog.store import connect
FIX=Path(__file__).resolve().parents[1]/'fixtures'
def test_recovery_freezes_real_removed_assembly(tmp_path):
    db=tmp_path/'recovery.db';initialize(db,FIX/'config.json')
    assert ingest(FIX/'events.jsonl',db)['imported']==6
    assert evaluate(db)=={'WO-1':'完成'}
    got=register(db,'REC-1',FIX/'recovery-case.json')
    assert got['cores']==4
    assert register(db,'REC-1',FIX/'recovery-case.json')==got
    c=connect(db)
    try:assert c.execute('SELECT COUNT(*) FROM recovery_case').fetchone()[0]==1
    finally:c.close()
def test_recovery_requires_completed_order(tmp_path):
    db=tmp_path/'recovery.db';initialize(db,FIX/'config.json')
    with pytest.raises(ValueError,match='recovery_order_not_complete'):
        register(db,'REC-1',FIX/'recovery-case.json')
