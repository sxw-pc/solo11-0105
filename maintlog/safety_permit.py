"""Freeze pending work order and isolation evidence before permit adjudication."""
import hashlib,json
from pathlib import Path
from .store import connect

def register(db_path,case_id,fixture):
    path=Path(fixture).resolve();raw=path.read_bytes();doc=json.loads(raw)
    if not case_id or not doc.get('work_order') or not doc.get('energy_edges') or not doc.get('lock_events'):
        raise ValueError('bad_permit_case')
    db=connect(db_path)
    try:
        with db:
            order=db.execute('SELECT id,equipment,status FROM work_order WHERE id=?',(doc['work_order'],)).fetchone()
            if not order or order['equipment']!=doc['equipment'] or order['status']!='待接收':
                raise ValueError('permit_order_missing')
            digest=hashlib.sha256(raw).hexdigest()
            payload=json.dumps({'fixture':str(path),'case':doc,'work_order':dict(order)},sort_keys=True,separators=(',',':'))
            old=db.execute('SELECT digest FROM permit_case WHERE id=?',(case_id,)).fetchone()
            if old and old['digest']!=digest:raise ValueError('permit_case_conflict')
            db.execute('INSERT OR IGNORE INTO permit_case VALUES (?,?,?)',(case_id,payload,digest))
    finally:db.close()
    return {'case_id':case_id,'digest':digest,'locks':len(doc['lock_events'])}
