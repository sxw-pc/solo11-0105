from pathlib import Path
import pytest
from maintlog.__main__ import initialize
from maintlog.ingest import ingest
from maintlog.rules import evaluate
from maintlog.vibration import register
from maintlog.store import connect

FIX = Path(__file__).resolve().parents[1] / 'fixtures'

def test_freezes_completed_order_and_vibration_trial(tmp_path):
    db = tmp_path / 'vibration.db'
    initialize(db, FIX / 'config.json')
    assert ingest(FIX / 'events.jsonl', db)['imported'] == 6
    assert evaluate(db)['WO-1'] == '完成'
    got = register(db, 'VIB-1', FIX / 'vibration-case.json')
    assert got['traces'] == 3
    assert register(db, 'VIB-1', FIX / 'vibration-case.json') == got
    con = connect(db)
    try:
        assert con.execute('SELECT COUNT(*) FROM vibration_case').fetchone()[0] == 1
    finally:
        con.close()

def test_rejects_without_completed_order(tmp_path):
    with pytest.raises(ValueError, match='vibration_order_not_complete'):
        register(tmp_path / 'none.db', 'VIB-1', FIX / 'vibration-case.json')
