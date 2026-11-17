from pathlib import Path
import pytest
from maintlog.__main__ import initialize
from maintlog.firmware_lab import register,state,invoke
FIX=Path(__file__).resolve().parents[1]/'fixtures'
def test_firmware_lab_freezes_actual_equipment_and_failures(tmp_path):
    db=tmp_path/'firmware.db';initialize(db,FIX/'config.json')
    assert register(db,'FIRM-1',FIX/'firmware-case.json')['devices']==3
    assert state(db,'DEV-A')['version']=='v1'
    assert invoke(db,'FIRM-1','DEV-B','install','v2','OP-B',0)=={'status':'ack_lost'}
    assert state(db,'DEV-B')['version']=='v2'
    assert invoke(db,'FIRM-1','DEV-C','install','v2','OP-C',0)['status']=='rejected'
    assert state(db,'DEV-C')['version']=='v1'
def test_firmware_lab_requires_equipment(tmp_path):
    with pytest.raises(ValueError,match='firmware_equipment_missing'):
        register(tmp_path/'missing.db','FIRM-1',FIX/'firmware-case.json')
