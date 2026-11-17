from pathlib import Path
import pytest
from maintlog.__main__ import initialize
from maintlog.calibration import register
from maintlog.store import connect
FIX=Path(__file__).resolve().parents[1]/'fixtures'
def test_calibration_freezes_real_instrument(tmp_path):
    db=tmp_path/'calibration.db';initialize(db,FIX/'config.json')
    got=register(db,'CAL-1',FIX/'calibration-case.json')
    assert got['points']==2
    assert register(db,'CAL-1',FIX/'calibration-case.json')==got
    c=connect(db)
    try:assert c.execute('SELECT COUNT(*) FROM calibration_case').fetchone()[0]==1
    finally:c.close()
def test_calibration_requires_instrument(tmp_path):
    with pytest.raises(ValueError,match='calibration_instrument_missing'):
        register(tmp_path/'missing.db','CAL-1',FIX/'calibration-case.json')
