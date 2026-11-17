from pathlib import Path
import pytest
from maintlog.__main__ import initialize
from maintlog.thermal_control import register
from maintlog.store import connect
FIX=Path(__file__).resolve().parents[1]/'fixtures'
def test_thermal_freezes_real_equipment_and_components(tmp_path):
    db=tmp_path/'thermal.db';initialize(db,FIX/'config.json')
    got=register(db,'THERM-1',FIX/'thermal-case.json')
    assert got['sensors']==2
    assert register(db,'THERM-1',FIX/'thermal-case.json')==got
    c=connect(db)
    try:assert c.execute('SELECT COUNT(*) FROM thermal_case').fetchone()[0]==1
    finally:c.close()
def test_thermal_requires_real_equipment(tmp_path):
    with pytest.raises(ValueError,match='thermal_equipment_missing'):
        register(tmp_path/'missing.db','THERM-1',FIX/'thermal-case.json')
