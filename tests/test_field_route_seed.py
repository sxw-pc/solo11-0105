from pathlib import Path
import pytest
from maintlog.__main__ import initialize
from maintlog.field_route import register
from maintlog.store import connect
FIX=Path(__file__).resolve().parents[1]/'fixtures'
def test_route_registers_real_orders(tmp_path):
    db=tmp_path/'route.db';initialize(db,FIX/'route-config.json')
    got=register(db,'R-1',FIX/'route-case.json')
    assert got['visits']==2
    assert register(db,'R-1',FIX/'route-case.json')==got
    c=connect(db)
    try:assert c.execute('SELECT COUNT(*) FROM route_case').fetchone()[0]==1
    finally:c.close()
def test_route_rejects_missing_orders(tmp_path):
    with pytest.raises(ValueError,match='route_order_missing'):
        register(tmp_path/'missing.db','R-1',FIX/'route-case.json')
