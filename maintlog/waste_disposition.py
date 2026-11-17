"""Freeze completed service, sealed waste containers and certified routes."""
import csv,hashlib,json
from decimal import Decimal
from pathlib import Path
from .store import connect

def register(db_path,case_id,fixture):
    path=Path(fixture).resolve();raw=path.read_bytes();doc=json.loads(raw)
    if not case_id or not doc.get('work_order') or not doc.get('containers_file') or not doc.get('contracts'):
        raise ValueError('bad_waste_case')
    src=(path.parent/doc['containers_file']).resolve();source_raw=src.read_bytes()
    rows=list(csv.DictReader(source_raw.decode('utf-8-sig').splitlines()))
    if not rows:raise ValueError('waste_measurements_missing')
    db=connect(db_path)
    try:
        with db:
            decision=db.execute('SELECT status,detail FROM decision WHERE work_order=?',(doc['work_order'],)).fetchone()
            if not decision or decision['status']!='完成':raise ValueError('waste_order_not_complete')
            for row in rows:
                gross=Decimal(row['gross_kg']);contaminant=Decimal(row['contaminant_kg'])
                if not 0<=contaminant<=gross:raise ValueError('invalid_waste_mass')
                old=db.execute('SELECT work_order,gross_kg,contaminant_kg,seal FROM waste_container WHERE id=?',(row['container'],)).fetchone()
                expected={'work_order':doc['work_order'],'gross_kg':row['gross_kg'],'contaminant_kg':row['contaminant_kg'],'seal':row['seal']}
                if old and dict(old)!=expected:raise ValueError('waste_container_conflict')
                db.execute('INSERT OR IGNORE INTO waste_container VALUES (?,?,?,?,?)',(row['container'],doc['work_order'],row['gross_kg'],row['contaminant_kg'],row['seal']))
            digest=hashlib.sha256(raw+source_raw).hexdigest()
            payload=json.dumps({'fixture':str(path),'case':doc,'containers':rows,'source_sha256':hashlib.sha256(source_raw).hexdigest(),'decision':dict(decision)},sort_keys=True,separators=(',',':'))
            old=db.execute('SELECT digest FROM waste_case WHERE id=?',(case_id,)).fetchone()
            if old and old['digest']!=digest:raise ValueError('waste_case_conflict')
            db.execute('INSERT OR IGNORE INTO waste_case VALUES (?,?,?)',(case_id,payload,digest))
    finally:db.close()
    return {'case_id':case_id,'digest':digest,'containers':len(rows)}
