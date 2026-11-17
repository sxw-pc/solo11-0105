"""Freeze real part consumption and signed local supplier quote rows."""
import csv,hashlib,json
from pathlib import Path
from .store import connect

def register(db_path,case_id,fixture):
    path=Path(fixture).resolve();raw=path.read_bytes();doc=json.loads(raw)
    if not case_id or not doc.get('demand') or not doc.get('quotes_file') or not doc.get('work_order'):
        raise ValueError('bad_procurement_case')
    quote_path=(path.parent/doc['quotes_file']).resolve();quote_raw=quote_path.read_bytes()
    quotes=list(csv.DictReader(quote_raw.decode('utf-8-sig').splitlines()))
    if not quotes:raise ValueError('quotes_empty')
    db=connect(db_path)
    try:
        with db:
            decision=db.execute('SELECT status,detail FROM decision WHERE work_order=?',(doc['work_order'],)).fetchone()
            if not decision or decision['status']!='完成':raise ValueError('consumption_order_not_complete')
            consumed=[]
            for e in db.execute("SELECT e.id,e.payload,e.sha256,s.sha256 AS source_sha256 FROM event e JOIN source s ON s.id=e.source_id WHERE e.work_order=? AND e.kind='replace'",(doc['work_order'],)):
                consumed.append(dict(e))
            if not any(json.loads(e['payload']).get('new')=='NEW-1' for e in consumed):
                raise ValueError('consumed_part_missing')
            digest=hashlib.sha256(raw+quote_raw).hexdigest()
            payload=json.dumps({'fixture':str(path),'case':doc,'quotes':quotes,'quote_sha256':hashlib.sha256(quote_raw).hexdigest(),'decision':dict(decision),'consumed':consumed},sort_keys=True,separators=(',',':'))
            old=db.execute('SELECT digest FROM procurement_case WHERE id=?',(case_id,)).fetchone()
            if old and old['digest']!=digest:raise ValueError('procurement_case_conflict')
            db.execute('INSERT OR IGNORE INTO procurement_case VALUES (?,?,?)',(case_id,payload,digest))
    finally:db.close()
    return {'case_id':case_id,'digest':digest,'quotes':len(quotes)}
