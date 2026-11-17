"""Freeze removed assembly evidence and physical subcomponent inspections."""
import hashlib,json
from pathlib import Path
from .store import connect

def register(db_path,case_id,fixture):
    path=Path(fixture).resolve();raw=path.read_bytes();doc=json.loads(raw)
    if not case_id or not doc.get('cores') or not doc.get('work_order') or not doc.get('removed_assembly'):
        raise ValueError('bad_recovery_case')
    db=connect(db_path)
    try:
        with db:
            decision=db.execute('SELECT status,detail FROM decision WHERE work_order=?',(doc['work_order'],)).fetchone()
            if not decision or decision['status']!='完成':raise ValueError('recovery_order_not_complete')
            matches=[]
            for e in db.execute("SELECT e.id,e.payload,e.sha256,s.sha256 AS source_sha256 FROM event e JOIN source s ON s.id=e.source_id WHERE e.work_order=? AND e.kind='replace'",(doc['work_order'],)):
                payload=json.loads(e['payload'])
                if payload.get('old')==doc['removed_assembly']:matches.append({'event_id':e['id'],'event_sha256':e['sha256'],'source_sha256':e['source_sha256']})
            if len(matches)!=1:raise ValueError('removed_assembly_missing')
            digest=hashlib.sha256(raw).hexdigest()
            payload=json.dumps({'fixture':str(path),'doc':doc,'decision':dict(decision),'removal':matches[0]},sort_keys=True,separators=(',',':'))
            old=db.execute('SELECT digest FROM recovery_case WHERE id=?',(case_id,)).fetchone()
            if old and old['digest']!=digest:raise ValueError('recovery_case_conflict')
            db.execute('INSERT OR IGNORE INTO recovery_case VALUES (?,?,?)',(case_id,payload,digest))
    finally:db.close()
    return {'case_id':case_id,'digest':digest,'cores':len(doc['cores'])}
