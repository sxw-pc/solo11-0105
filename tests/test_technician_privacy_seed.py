from pathlib import Path
import pytest
from maintlog.__main__ import initialize
from maintlog.ingest import ingest
from maintlog.rules import evaluate
from maintlog.technician_privacy import register
from maintlog.store import connect
FIX=Path(__file__).resolve().parents[1]/'fixtures'
def test_privacy_freezes_real_operator_counts_and_prior_release(tmp_path):
    db=tmp_path/'privacy.db';initialize(db,FIX/'config.json')
    assert ingest(FIX/'privacy-events.jsonl',db)['imported']==11
    assert evaluate(db)=={'WO-1':'完成'}
    got=register(db,'PRIV-1',FIX/'technician-privacy-case.json')
    assert got['operators']==3
    assert register(db,'PRIV-1',FIX/'technician-privacy-case.json')==got
    c=connect(db)
    try:assert c.execute('SELECT reading_count FROM published_tech_stat WHERE id=?',('R1',)).fetchone()[0]==6
    finally:c.close()
def test_privacy_requires_reading_events(tmp_path):
    db=tmp_path/'privacy.db';initialize(db,FIX/'config.json')
    with pytest.raises(ValueError,match='operator_evidence_missing'):
        register(db,'PRIV-1',FIX/'technician-privacy-case.json')
