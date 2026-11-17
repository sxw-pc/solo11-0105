"""Freeze actual reading operators and an already issued group statistic."""
import hashlib,json
from collections import Counter
from pathlib import Path
from .store import connect

def register(db_path,case_id,fixture):
    path=Path(fixture).resolve();raw=path.read_bytes();doc=json.loads(raw)
    if not case_id or not doc.get('audience') or not doc.get('prior') or not doc.get('proposed'):
        raise ValueError('bad_technician_privacy_case')
    db=connect(db_path)
    try:
        with db:
            events=[dict(e) for e in db.execute("SELECT e.id,e.payload,e.sha256,s.sha256 AS source_sha256 FROM event e JOIN source s ON s.id=e.source_id WHERE e.kind='reading' ORDER BY e.id")]
            counts=Counter(json.loads(e['payload']).get('operator') for e in events)
            if not events or None in counts:raise ValueError('operator_evidence_missing')
            prior=doc['prior']; group=prior['members']
            total=sum(counts[x] for x in group)
            if total!=prior['reading_count']:raise ValueError('prior_count_mismatch')
            source_sha=events[0]['source_sha256']
            db.execute('INSERT OR IGNORE INTO published_tech_stat VALUES (?,?,?,?,?)',(prior['id'],doc['audience'],json.dumps(group,sort_keys=True),total,source_sha))
            digest=hashlib.sha256(raw).hexdigest()
            payload=json.dumps({'fixture':str(path),'case':doc,'counts':dict(counts),'events':events},sort_keys=True,separators=(',',':'))
            old=db.execute('SELECT digest FROM technician_privacy_case WHERE id=?',(case_id,)).fetchone()
            if old and old['digest']!=digest:raise ValueError('privacy_case_conflict')
            db.execute('INSERT OR IGNORE INTO technician_privacy_case VALUES (?,?,?)',(case_id,payload,digest))
    finally:db.close()
    return {'case_id':case_id,'digest':digest,'operators':len(counts)}
