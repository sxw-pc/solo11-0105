from pathlib import Path
import pytest
from maintlog.__main__ import initialize
from maintlog.ingest import ingest
from maintlog.rules import evaluate
from maintlog.warranty_billing import register
from maintlog.store import connect
FIX=Path(__file__).resolve().parents[1]/'fixtures'
def test_warranty_freezes_completed_order_and_invoice(tmp_path):
    db=tmp_path/'warranty.db';initialize(db,FIX/'config.json')
    assert ingest(FIX/'events.jsonl',db)['imported']==6
    assert evaluate(db)=={'WO-1':'完成'}
    got=register(db,'WARRANTY-1',FIX/'warranty-case.json')
    assert got['invoice']=='INV-001'
    assert register(db,'WARRANTY-1',FIX/'warranty-case.json')==got
    c=connect(db)
    try:assert c.execute('SELECT customer_due_cents FROM issued_invoice WHERE id=?',('INV-001',)).fetchone()[0]==8000
    finally:c.close()
def test_warranty_requires_complete_order(tmp_path):
    db=tmp_path/'warranty.db';initialize(db,FIX/'config.json')
    with pytest.raises(ValueError,match='billing_order_not_complete'):
        register(db,'WARRANTY-1',FIX/'warranty-case.json')
