import json
from pathlib import Path
from .store import connect
def report(db_path,out):
    db=connect(db_path);data={t:[dict(x) for x in db.execute(f'SELECT * FROM {t} ORDER BY 1')] for t in ('work_order','step','instrument','stock','event','evidence','issue','decision','run','permit_grant','permit_adjudication','start_attempt')}
    data['source']=[dict(x) for x in db.execute('SELECT id,name,sha256,length(raw) AS byte_length FROM source ORDER BY id')]
    data['permit_case']=[dict(x) for x in db.execute('SELECT id,digest,payload FROM permit_case ORDER BY id')]
    p=Path(out);p.parent.mkdir(parents=True,exist_ok=True);p.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
    lines=['# 设备保养履历','']
    lines+=[f"- {r['work_order']}: {r['status']}" for r in data['decision']]
    lines+=['','## 隔离许可','']
    for g in data['permit_grant']:
        lines.append(f"- {g['id']} {g['work_order']} [{g['contract']}] {g['status']} fence={g['fence']} 窗口 {g['valid_from']} ~ {g['valid_until']}")
    for a in data['start_attempt']:
        mark='准许' if a['status']=='permitted' else '阻断'
        lines.append(f"- {mark}开工 event={a['event_id']} {a['work_order']} 原因={a['reason']}")
    p.with_suffix('.md').write_text('\n'.join(lines)+'\n',encoding='utf-8');db.close();return p
