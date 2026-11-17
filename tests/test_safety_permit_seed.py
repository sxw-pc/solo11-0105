from pathlib import Path
import pytest
from maintlog.__main__ import initialize
from maintlog.safety_permit import register
from maintlog.store import connect
FIX=Path(__file__).resolve().parents[1]/'fixtures'
def test_permit_freezes_pending_order_and_lock(tmp_path):
    db=tmp_path/'permit.db';initialize(db,FIX/'permit-config.json')
    got=register(db,'PERMIT-1',FIX/'permit-case.json')
    assert got['locks']==1
    assert register(db,'PERMIT-1',FIX/'permit-case.json')==got
    c=connect(db)
    try:assert c.execute('SELECT COUNT(*) FROM permit_case').fetchone()[0]==1
    finally:c.close()
def test_permit_requires_real_pending_order(tmp_path):
    with pytest.raises(ValueError,match='permit_order_missing'):
        register(tmp_path/'missing.db','PERMIT-1',FIX/'permit-case.json')
