"""Freeze equipment identity, component temperature sensors and fan contract."""
import csv,hashlib,json
from pathlib import Path
from .store import connect

def register(db_path,case_id,fixture):
    path=Path(fixture).resolve();raw=path.read_bytes();doc=json.loads(raw)
    if not case_id or not doc.get('equipment') or not doc.get('components') or not doc.get('sensors_file'):
        raise ValueError('bad_thermal_case')
    sensor_path=(path.parent/doc['sensors_file']).resolve();sensor_raw=sensor_path.read_bytes()
    sensors=list(csv.DictReader(sensor_raw.decode('utf-8-sig').splitlines()))
    if not sensors:raise ValueError('thermal_sensors_missing')
    db=connect(db_path)
    try:
        with db:
            order=db.execute('SELECT id,equipment FROM work_order WHERE equipment=? ORDER BY id LIMIT 1',(doc['equipment'],)).fetchone()
            if not order:raise ValueError('thermal_equipment_missing')
            digest=hashlib.sha256(raw+sensor_raw).hexdigest()
            payload=json.dumps({'fixture':str(path),'case':doc,'work_order':dict(order),'sensors':sensors,'sensor_sha256':hashlib.sha256(sensor_raw).hexdigest()},sort_keys=True,separators=(',',':'))
            old=db.execute('SELECT digest FROM thermal_case WHERE id=?',(case_id,)).fetchone()
            if old and old['digest']!=digest:raise ValueError('thermal_case_conflict')
            db.execute('INSERT OR IGNORE INTO thermal_case VALUES (?,?,?)',(case_id,payload,digest))
    finally:db.close()
    return {'case_id':case_id,'digest':digest,'sensors':len(sensors)}
