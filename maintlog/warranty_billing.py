"""Freeze completed service evidence and a published invoice for later settlement."""
import hashlib,json
from pathlib import Path
from .store import connect

def register(db_path,case_id,fixture):
    path=Path(fixture).resolve();raw=path.read_bytes();doc=json.loads(raw)
    if not case_id or not doc.get('work_order') or not doc.get('invoice') or not doc.get('contracts'):
        raise ValueError('bad_warranty_case')
    db=connect(db_path)
    try:
        with db:
            decision=db.execute('SELECT status,detail,rule_version FROM decision WHERE work_order=?',(doc['work_order'],)).fetchone()
            if not decision or decision['status']!='完成':raise ValueError('billing_order_not_complete')
            evidence=[dict(e) for e in db.execute('SELECT e.id,e.kind,e.at,e.sha256,s.sha256 AS source_sha256 FROM event e JOIN source s ON s.id=e.source_id WHERE e.work_order=? ORDER BY e.id',(doc['work_order'],))]
            if not any(e['kind']=='replace' for e in evidence):raise ValueError('billing_part_missing')
            digest=hashlib.sha256(raw).hexdigest()
            payload=json.dumps({'fixture':str(path),'doc':doc,'decision':dict(decision),'evidence':evidence},sort_keys=True,separators=(',',':'))
            inv=doc['invoice'];old=db.execute('SELECT work_order,customer_due_cents FROM issued_invoice WHERE id=?',(inv['id'],)).fetchone()
            if old and (old['work_order']!=doc['work_order'] or old['customer_due_cents']!=inv['customer_due_cents']):raise ValueError('invoice_conflict')
            db.execute('INSERT OR IGNORE INTO issued_invoice VALUES (?,?,?)',(inv['id'],doc['work_order'],inv['customer_due_cents']))
            old=db.execute('SELECT digest FROM warranty_case WHERE id=?',(case_id,)).fetchone()
            if old and old['digest']!=digest:raise ValueError('warranty_case_conflict')
            db.execute('INSERT OR IGNORE INTO warranty_case VALUES (?,?,?)',(case_id,payload,digest))
    finally:db.close()
    return {'case_id':case_id,'digest':digest,'invoice':doc['invoice']['id']}
