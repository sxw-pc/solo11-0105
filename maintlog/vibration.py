"""Freeze a multi-speed vibration trial and the completed work order it came from."""
import hashlib
import json
from pathlib import Path
from .store import connect


def register(db_path, case_id, fixture):
    raw = Path(fixture).read_bytes()
    doc = json.loads(raw)
    if not case_id or not doc.get('work_order') or len(doc.get('traces', [])) < 3 or not doc.get('mount_calibrations'):
        raise ValueError('bad_vibration_case')
    db = connect(db_path)
    try:
        with db:
            decision = db.execute('SELECT status FROM decision WHERE work_order=?', (doc['work_order'],)).fetchone()
            if not decision or decision['status'] != '完成':
                raise ValueError('vibration_order_not_complete')
            events = [dict(e) for e in db.execute('''SELECT e.id,e.sha256,s.sha256 AS source_sha256
                FROM event e JOIN source s ON s.id=e.source_id
                WHERE e.work_order=? AND e.kind='reading' ORDER BY e.at,e.id''', (doc['work_order'],))]
            if not events:
                raise ValueError('vibration_reading_missing')
            digest = hashlib.sha256(raw).hexdigest()
            old = db.execute('SELECT digest FROM vibration_case WHERE id=?', (case_id,)).fetchone()
            if old and old['digest'] != digest:
                raise ValueError('vibration_case_conflict')
            db.execute('INSERT OR IGNORE INTO vibration_case VALUES (?,?,?)',
                       (case_id, json.dumps({'doc': doc, 'reading_evidence': events}, sort_keys=True), digest))
    finally:
        db.close()
    return {'case_id': case_id, 'digest': digest, 'traces': len(doc['traces'])}
