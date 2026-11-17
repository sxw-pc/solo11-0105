from pathlib import Path
import pytest
from maintlog.__main__ import initialize
from maintlog.tool_custody import register
from maintlog.store import connect
FIX=Path(__file__).resolve().parents[1]/'fixtures'
def test_tool_freezes_calibrated_instrument_and_claims(tmp_path):
    db=tmp_path/'tool.db';initialize(db,FIX/'config.json')
    got=register(db,'TOOL-1',FIX/'tool-custody-case.json')
    assert got['claims']==2
    assert register(db,'TOOL-1',FIX/'tool-custody-case.json')==got
    c=connect(db)
    try:assert c.execute('SELECT COUNT(*) FROM tool_reservation').fetchone()[0]==1
    finally:c.close()
def test_tool_requires_real_instrument(tmp_path):
    with pytest.raises(ValueError,match='tool_or_order_missing'):
        register(tmp_path/'missing.db','TOOL-1',FIX/'tool-custody-case.json')
