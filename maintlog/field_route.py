"""Freeze work-order visits and road data for an offline service shift."""
import hashlib,json
from pathlib import Path
from .store import connect

def register(db_path,route_id,fixture):
    path=Path(fixture).resolve(); raw=path.read_bytes(); doc=json.loads(raw)
    if not route_id or not doc.get('visits') or not doc.get('arcs') or not doc.get('technicians'):
        raise ValueError('bad_route_case')
    db=connect(db_path)
    try:
        with db:
            ids=[]
            for visit in doc['visits']:
                row=db.execute('SELECT id,equipment FROM work_order WHERE id=?',(visit['work_order'],)).fetchone()
                if not row or row['equipment']!=visit['equipment']:
                    raise ValueError('route_order_missing')
                ids.append(row['id'])
            if len(set(ids))!=len(ids):raise ValueError('duplicate_route_order')
            digest=hashlib.sha256(raw).hexdigest()
            payload=json.dumps({'fixture':str(path),'route':doc,'orders':ids},sort_keys=True,separators=(',',':'))
            old=db.execute('SELECT digest FROM route_case WHERE id=?',(route_id,)).fetchone()
            if old and old['digest']!=digest:raise ValueError('route_case_conflict')
            db.execute('INSERT OR IGNORE INTO route_case VALUES (?,?,?)',(route_id,payload,digest))
    finally:db.close()
    return {'route_id':route_id,'digest':digest,'visits':len(ids)}
