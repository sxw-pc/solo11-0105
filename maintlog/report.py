import json
from pathlib import Path
from .store import connect
def report(db_path,out):
    db=connect(db_path);data={t:[dict(x) for x in db.execute(f'SELECT * FROM {t} ORDER BY 1')] for t in ('work_order','step','instrument','stock','event','evidence','issue','decision','run')}
    data['source']=[dict(x) for x in db.execute('SELECT id,name,sha256,length(raw) AS byte_length FROM source ORDER BY id')]
    p=Path(out);p.parent.mkdir(parents=True,exist_ok=True);p.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
    p.with_suffix('.md').write_text('# 设备保养履历\n\n'+'\n'.join(f"- {r['work_order']}: {r['status']}" for r in data['decision']),encoding='utf-8');db.close();return p
