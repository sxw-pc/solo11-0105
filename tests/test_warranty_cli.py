"""Offline billing commands: register, settle, settlement-report."""
import json,subprocess,sys
from pathlib import Path
from maintlog.__main__ import initialize
from maintlog.ingest import ingest
from maintlog.rules import evaluate
from maintlog.store import connect
from warranty_helpers import FIX

ROOT=Path(__file__).resolve().parents[1]
_LAUNCHER=("import sys;sys.path.insert(0,%r);"
           "from maintlog.__main__ import main;main()") % str(ROOT)

def _run(*args):
    # -I keeps the isolated mode the data pipeline requires; the launcher puts the
    # repo root on sys.path explicitly, since -I omits both cwd and PYTHONPATH
    return subprocess.run([sys.executable,'-I','-c',_LAUNCHER,*args],cwd=ROOT,
                          capture_output=True,text=True,check=True)

def test_cli_register_settle_and_report(tmp_path):
    db=tmp_path/'cli.db'
    initialize(db,FIX/'config.json')
    ingest(FIX/'events.jsonl',db);evaluate(db)
    out=_run('register','--db',str(db),'--case','WARRANTY-1','--fixture',str(FIX/'warranty-case.json'))
    assert 'INV-001' in out.stdout
    parts=json.loads(_run('settle','--db',str(db),'--case','WARRANTY-1','--contract','parts_covered').stdout)
    assert parts['status']=='已出账' and parts['insurer_due_cents']==3000 and parts['customer_due_cents']==11000
    labor=json.loads(_run('settle','--db',str(db),'--case','WARRANTY-1','--contract','labor_covered').stdout)
    assert labor['insurer_due_cents']==9000 and labor['customer_due_cents']==5000
    report=tmp_path/'settlement.json'
    _run('settlement-report','--db',str(db),'--out',str(report))
    data=json.loads(report.read_text(encoding='utf-8'))
    assert {s['contract_id'] for s in data['streams']}=={'parts_covered','labor_covered'}
    c=connect(db)
    try:assert c.execute('PRAGMA foreign_key_check').fetchall()==[]
    finally:c.close()
