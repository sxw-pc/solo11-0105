"""Freeze reference traceability and two-point instrument laboratory readings."""
import hashlib,json
from pathlib import Path
from .store import connect

def register(db_path,case_id,fixture):
    path=Path(fixture).resolve();raw=path.read_bytes();doc=json.loads(raw)
    if not case_id or not doc.get('instrument') or not doc.get('reference') or not doc.get('points'):
        raise ValueError('bad_calibration_case')
    db=connect(db_path)
    try:
        with db:
            instrument=db.execute('SELECT * FROM instrument WHERE id=?',(doc['instrument'],)).fetchone()
            if not instrument or instrument['unit']!=doc['unit']:
                raise ValueError('calibration_instrument_missing')
            digest=hashlib.sha256(raw).hexdigest()
            payload=json.dumps({'fixture':str(path),'case':doc,'instrument':dict(instrument)},sort_keys=True,separators=(',',':'))
            old=db.execute('SELECT digest FROM calibration_case WHERE id=?',(case_id,)).fetchone()
            if old and old['digest']!=digest:raise ValueError('calibration_case_conflict')
            db.execute('INSERT OR IGNORE INTO calibration_case VALUES (?,?,?)',(case_id,payload,digest))
    finally:db.close()
    return {'case_id':case_id,'digest':digest,'points':len(doc['points'])}
