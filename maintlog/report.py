import json
from pathlib import Path
from .store import connect
CONTRACT_LABEL={'schedule':'班组排程(墙钟)','piecework':'合同计件(有效作业)'}
def _line(r):
    line=f"- {r['work_order']}: {r['status']}"
    t=json.loads(r['detail']).get('timing')
    if t:
        label=CONTRACT_LABEL.get(t['contract'],t['contract'])
        if t['settlement']=='blocked':line+=f"；时限未结算（证据不足：{','.join(t['problems'])}）"
        elif t['settlement']=='void':line+='；工单作废，时限不结算'
        elif t.get('conclusion'):
            steps='，'.join(f"步骤{s['position']}{s['status']}，差值{s['overtime_minutes']:+.1f}分钟" for s in t['steps'] if s['limit_minutes'] is not None)
            line+=f"；{label}：{t['conclusion']}"+(f"（{steps}）" if steps else '')
    return line
def report(db_path,out):
    db=connect(db_path);data={t:[dict(x) for x in db.execute(f'SELECT * FROM {t} ORDER BY 1')] for t in ('work_order','step','instrument','stock','event','evidence','issue','decision','run')}
    data['source']=[dict(x) for x in db.execute('SELECT id,name,sha256,length(raw) AS byte_length FROM source ORDER BY id')]
    p=Path(out);p.parent.mkdir(parents=True,exist_ok=True);p.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
    latest={}
    for r in data['decision']:
        if r['work_order'] not in latest or r['run_id']>latest[r['work_order']]['run_id']:latest[r['work_order']]=r
    p.with_suffix('.md').write_text('# 设备保养履历\n\n'+'\n'.join(_line(latest[k]) for k in sorted(latest)),encoding='utf-8');db.close();return p
