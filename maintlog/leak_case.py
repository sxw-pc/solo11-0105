"""Freeze real pressure and temperature readings for a synthetic seal trial."""
import hashlib,json
from pathlib import Path
from .store import connect

def register(db_path,case_id,fixture):
    if not case_id:raise ValueError('missing_leak_case_id')
    source=Path(fixture).resolve();raw=source.read_bytes();doc=json.loads(raw)
    if not doc.get('work_order') or len(doc.get('reading_seq',[]))!=4 or not doc.get('models'):
        raise ValueError('bad_leak_contract')
    db=connect(db_path)
    try:
        with db:
            decision=db.execute('SELECT status FROM decision WHERE work_order=?',(doc['work_order'],)).fetchone()
            if not decision or decision['status']!='完成':raise ValueError('leak_order_not_complete')
            rows=[]
            for seq in doc['reading_seq']:
                e=db.execute("SELECT id,at,payload,sha256 FROM event WHERE device=? AND seq=? AND work_order=? AND kind='reading'",(doc['device'],seq,doc['work_order'])).fetchone()
                if not e:raise ValueError('leak_reading_missing')
                rows.append(dict(e))
            payload=json.dumps({'fixture':str(source),'digest':hashlib.sha256(raw).hexdigest(),
                                'doc':doc,'readings':rows},sort_keys=True,separators=(',',':'))
            old=db.execute('SELECT payload FROM leak_case WHERE id=?',(case_id,)).fetchone()
            if old and old['payload']!=payload:raise ValueError('leak_case_conflict')
            db.execute('INSERT OR IGNORE INTO leak_case VALUES (?,?)',(case_id,payload))
    finally:db.close()
    return {'case_id':case_id,'digest':hashlib.sha256(raw).hexdigest(),'readings':len(rows)}
