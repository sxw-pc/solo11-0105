"""Freeze calibrated instrument, work-order reservation and competing claims."""
import hashlib,json
from pathlib import Path
from .store import connect

def register(db_path,case_id,fixture):
    path=Path(fixture).resolve();raw=path.read_bytes();doc=json.loads(raw)
    if not case_id or not doc.get('instrument') or not doc.get('custody') or not doc.get('claims'):
        raise ValueError('bad_tool_case')
    db=connect(db_path)
    try:
        with db:
            instrument=db.execute('SELECT * FROM instrument WHERE id=?',(doc['instrument'],)).fetchone()
            order=db.execute('SELECT id FROM work_order WHERE id=?',(doc['work_order'],)).fetchone()
            if not instrument or not order:raise ValueError('tool_or_order_missing')
            voucher_digest=hashlib.sha256(doc['voucher_serial'].encode()).hexdigest()
            expected={'work_order':doc['work_order'],'assigned_tech':doc['assigned_tech'],'voucher_digest':voucher_digest,'seal':doc['seal']}
            old=db.execute('SELECT work_order,assigned_tech,voucher_digest,seal FROM tool_reservation WHERE instrument=?',(doc['instrument'],)).fetchone()
            if old and dict(old)!=expected:raise ValueError('tool_reservation_conflict')
            db.execute('INSERT OR IGNORE INTO tool_reservation VALUES (?,?,?,?,?)',(doc['instrument'],doc['work_order'],doc['assigned_tech'],voucher_digest,doc['seal']))
            digest=hashlib.sha256(raw).hexdigest()
            payload=json.dumps({'fixture':str(path),'case':doc,'instrument':dict(instrument),'voucher_digest':voucher_digest},sort_keys=True,separators=(',',':'))
            old=db.execute('SELECT digest FROM tool_checkout_case WHERE id=?',(case_id,)).fetchone()
            if old and old['digest']!=digest:raise ValueError('tool_case_conflict')
            db.execute('INSERT OR IGNORE INTO tool_checkout_case VALUES (?,?,?)',(case_id,payload,digest))
    finally:db.close()
    return {'case_id':case_id,'digest':digest,'claims':len(doc['claims'])}
