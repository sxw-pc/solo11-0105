"""Helpers for warranty settlement tests."""
import copy,json
from pathlib import Path
from maintlog.__main__ import initialize
from maintlog.ingest import ingest
from maintlog.rules import evaluate
from maintlog.warranty_billing import register
from maintlog.store import connect

FIX=Path(__file__).resolve().parents[1]/'fixtures'

def base_case():
    return json.loads((FIX/'warranty-case.json').read_text(encoding='utf-8'))

def seed(tmp,events_name='events.jsonl'):
    db=tmp/'maintlog.db'
    initialize(db,FIX/'config.json')
    ingest(FIX/events_name,db)
    assert evaluate(db)=={'WO-1':'完成'}
    return db

def register_case(tmp,db,case_id,doc,name='case.json'):
    p=tmp/name
    p.write_text(json.dumps(doc,ensure_ascii=False),encoding='utf-8')
    return register(db,case_id,p)

def registered(tmp,case_id='WARRANTY-1',doc=None):
    db=seed(tmp)
    register_case(tmp,db,case_id,doc or base_case())
    return db

def write_events(tmp,db,rows,name='extra.jsonl'):
    p=tmp/name
    p.write_text('\n'.join(json.dumps(r) for r in rows),encoding='utf-8')
    return ingest(p,db)

def event_rows():
    return [json.loads(x) for x in (FIX/'events.jsonl').read_text(encoding='utf-8').splitlines()]

def entry_sums(db,oid,contract_id):
    c=connect(db)
    try:
        out={}
        for party in ('customer','insurer'):
            out[party]=c.execute('SELECT COALESCE(SUM(amount_cents),0) FROM settlement_entry '
                                 'WHERE work_order=? AND contract_id=? AND party=?',(oid,contract_id,party)).fetchone()[0]
        return out
    finally:c.close()

def invoice_amount(db,invoice_id='INV-001'):
    c=connect(db)
    try:return c.execute('SELECT customer_due_cents FROM issued_invoice WHERE id=?',(invoice_id,)).fetchone()[0]
    finally:c.close()
