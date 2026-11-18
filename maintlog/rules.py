import json
from collections import defaultdict
from .store import connect
from .ingest import when
VERSION='1'
def evaluate(path):
    db=connect(path);out={}
    with db:
        run_id=db.execute("INSERT INTO run(status,started_at) VALUES ('待接收',CURRENT_TIMESTAMP)").lastrowid
        db.execute("UPDATE run SET status='已校验' WHERE id=?",(run_id,))
        db.execute('DELETE FROM decision');db.execute("DELETE FROM issue WHERE code NOT IN ('parse_error','identity_conflict','unknown_order')")
        events=[dict(e) for e in db.execute('SELECT * FROM event ORDER BY at,id')]
        permit_wos=set()
        for r in db.execute('SELECT payload FROM permit_case'):
            try:permit_wos.add(json.loads(r['payload'])['case']['work_order'])
            except (KeyError,TypeError,json.JSONDecodeError):continue
        attempts={r['event_id']:(r['status'],r['reason']) for r in db.execute('SELECT event_id,status,reason FROM start_attempt')}
        devices=defaultdict(list)
        for e in events:devices[e['device']].append(e)
        gaps=set()
        for d,ee in devices.items():
            seq={e['seq'] for e in ee}
            if max(seq)-min(seq)+1!=len(seq):
                gaps.update(e['work_order'] for e in ee);db.execute('INSERT INTO issue(code,work_order,detail) VALUES (?,?,?)',('sequence_gap',d,'missing terminal sequence'))
        for order in db.execute('SELECT * FROM work_order'):
            oid=order['id'];ee=[e for e in events if e['work_order']==oid];steps={r['position']:dict(r) for r in db.execute('SELECT * FROM step WHERE work_order=?',(oid,))}
            problems=[];extras={};done=set();state='new';readings=[];parts=[];signatures=[]
            if oid in gaps:problems.append(('sequence_gap',None))
            for e in ee:
                data=json.loads(e['payload']);kind=e['kind']
                if kind=='start':
                    permitted=True
                    if oid in permit_wos:
                        st=attempts.get(e['id'])
                        permitted=bool(st and st[0]=='permitted')
                        if not permitted:
                            problems.append(('permit_denied',e['id']));extras[('permit_denied',e['id'])]=st[1] if st else 'permit_missing'
                    if permitted:
                        if state!='new':problems.append(('bad_transition',e['id']))
                        else:state='active'
                elif kind=='pause':
                    if state!='active':problems.append(('bad_transition',e['id']))
                    else:state='paused'
                elif kind=='resume':
                    if state!='paused':problems.append(('bad_transition',e['id']))
                    else:state='active'
                elif kind=='void':state='void'
                elif state!='active':problems.append(('inactive_event',e['id']))
                elif kind=='step':
                    pos=int(data.get('position',-1));spec=steps.get(pos)
                    if not spec or any(i not in done for i in range(1,pos)) or data.get('procedure_version')!=order['procedure_version']:
                        problems.append(('step_order',e['id']))
                    elif data.get('result')!='pass':problems.append(('step_failed',e['id']))
                    else:done.add(pos)
                elif kind=='reading':
                    instrument=db.execute('SELECT * FROM instrument WHERE id=?',(data.get('instrument'),)).fetchone()
                    units={'C':(lambda v:v),'F':(lambda v:(v-32)*5/9),'bar':(lambda v:v),'kPa':(lambda v:v/100)}
                    try:value=units[data['unit']](float(data['value']))
                    except (KeyError,ValueError,TypeError):problems.append(('unit_invalid',e['id']));continue
                    base='C' if data['unit'] in ('C','F') else 'bar'
                    if not instrument or instrument['unit']!=base or when(instrument['calibrated_until'])<when(e['at']) or not instrument['min']<=value<=instrument['max']:
                        problems.append(('reading_untrusted',e['id']))
                    else:readings.append(e['id'])
                elif kind=='replace':
                    old,new=data.get('old'),data.get('new');issued=db.execute('SELECT issued_to FROM stock WHERE part=?',(new,)).fetchone()
                    if not old or not new or old==new or not issued or issued['issued_to']!=oid:problems.append(('part_mismatch',e['id']))
                    else:parts.append(e['id'])
                elif kind=='sign':signatures.append(e['id']);state='signed'
            if not ee or any(s['required'] and i not in done for i,s in steps.items()) or not signatures:
                problems.append(('missing_evidence',None))
            if not readings:problems.append(('missing_reading',None))
            if not parts:problems.append(('missing_part',None))
            for code,eid in sorted(set(problems)):
                db.execute('INSERT INTO issue(code,work_order,event_id,detail) VALUES (?,?,?,?)',(code,oid,eid,extras.get((code,eid),code)))
            severe={'step_failed','step_order','bad_transition','inactive_event','part_mismatch','reading_untrusted','permit_denied'}
            status='作废' if state=='void' else ('需返工' if any(c in severe for c,_ in problems) else ('证据不足' if problems else '完成'))
            start_attempts=[{'event_id':e['id'],'status':attempts[e['id']][0],'reason':attempts[e['id']][1]} for e in ee if e['kind']=='start' and e['id'] in attempts]
            detail=json.dumps({'event_ids':[e['id'] for e in ee],'completed_steps':sorted(done),'reading_ids':readings,'replacement_ids':parts,'signature_ids':signatures,'start_attempts':start_attempts,'issues':[c for c,_ in problems]},ensure_ascii=False)
            db.execute('UPDATE work_order SET status=? WHERE id=?',('作废' if status=='作废' else '已判定',oid))
            db.execute('INSERT INTO decision VALUES (?,?,?,?)',(oid,status,detail,VERSION));out[oid]=status
        db.execute("UPDATE run SET status=CASE WHEN EXISTS(SELECT 1 FROM decision WHERE status NOT IN ('完成','作废')) THEN '待核' ELSE '已判定' END,ended_at=CURRENT_TIMESTAMP WHERE id=?",(run_id,))
    db.close();return out
