import json
from collections import defaultdict
from .store import connect
from .ingest import when
VERSION='2'
SEVERE={'step_failed','step_order','bad_transition','inactive_event','part_mismatch','reading_untrusted'}
def _parts_wait(reason):return 'part' in reason.lower() or '备件' in reason
def evaluate(path):
    db=connect(path);out={}
    with db:
        run_id=db.execute("INSERT INTO run(status,started_at) VALUES ('待接收',CURRENT_TIMESTAMP)").lastrowid
        db.execute("UPDATE run SET status='已校验' WHERE id=?",(run_id,))
        db.execute("DELETE FROM issue WHERE code NOT IN ('parse_error','identity_conflict','unknown_order')")
        events=[dict(e) for e in db.execute('SELECT * FROM event ORDER BY at,id')]
        devices=defaultdict(list)
        for e in events:devices[e['device']].append(e)
        gaps=set();clock_bad=set()
        for d,dev_events in devices.items():
            seq={e['seq'] for e in dev_events}
            if max(seq)-min(seq)+1!=len(seq):
                gaps.update(e['work_order'] for e in dev_events);db.execute('INSERT INTO issue(code,work_order,detail) VALUES (?,?,?)',('sequence_gap',d,'missing terminal sequence'))
            by_seq=sorted(dev_events,key=lambda e:e['seq'])
            if any(when(by_seq[i+1]['at'])<when(by_seq[i]['at']) for i in range(len(by_seq)-1)):
                clock_bad.update(e['work_order'] for e in dev_events);db.execute('INSERT INTO issue(code,work_order,detail) VALUES (?,?,?)',('clock_regression',d,'terminal clock moved backwards'))
        for order in db.execute('SELECT * FROM work_order'):
            oid=order['id'];contract=order['contract'];timed=contract in ('schedule','piecework')
            ee=[e for e in events if e['work_order']==oid];steps={r['position']:dict(r) for r in db.execute('SELECT * FROM step WHERE work_order=?',(oid,))}
            problems=[];tprobs=[];done=set();state='new';readings=[];parts=[];signatures=[]
            segments=[];open_seg=None;start_ref=None;step_bounds=[];replace_times=[];pauses=[]
            if oid in gaps:problems.append(('sequence_gap',None))
            if timed and oid in clock_bad:tprobs.append(('clock_regression',None))
            for e in ee:
                data=json.loads(e['payload']);kind=e['kind']
                if kind=='start':
                    if state!='new':problems.append(('bad_transition',e['id']))
                    else:state='active';open_seg=['active',e['id'],e['at']];start_ref=(e['id'],e['at'])
                elif kind=='pause':
                    if state!='active':
                        problems.append(('bad_transition',e['id']))
                        if timed and state=='paused':tprobs.append(('pause_overlap',e['id']))
                    else:
                        segments.append((open_seg[0],open_seg[1],open_seg[2],e['id'],e['at']))
                        state='paused';open_seg=['pause',e['id'],e['at']]
                        pauses.append({'id':e['id'],'at':e['at'],'reason':str(data.get('reason') or ''),'resume_id':None})
                elif kind=='resume':
                    if state!='paused':problems.append(('bad_transition',e['id']))
                    else:
                        segments.append((open_seg[0],open_seg[1],open_seg[2],e['id'],e['at']))
                        if pauses and pauses[-1]['resume_id'] is None:pauses[-1]['resume_id']=e['id']
                        state='active';open_seg=['active',e['id'],e['at']]
                elif kind=='void':
                    if open_seg:segments.append((open_seg[0],open_seg[1],open_seg[2],e['id'],e['at']));open_seg=None
                    state='void'
                elif state!='active':
                    if timed and state=='paused' and kind in ('step','sign'):tprobs.append((f'{kind}_during_pause',e['id']))
                    else:problems.append(('inactive_event',e['id']))
                elif kind=='step':
                    pos=int(data.get('position',-1));spec=steps.get(pos)
                    if not spec or any(i not in done for i in range(1,pos)) or data.get('procedure_version')!=order['procedure_version']:
                        problems.append(('step_order',e['id']))
                    elif data.get('result')!='pass':problems.append(('step_failed',e['id']))
                    else:done.add(pos);step_bounds.append((pos,e['id'],e['at']))
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
                    else:parts.append(e['id']);replace_times.append((e['id'],e['at']))
                elif kind=='sign':
                    signatures.append(e['id']);state='signed'
                    if open_seg:segments.append((open_seg[0],open_seg[1],open_seg[2],e['id'],e['at']));open_seg=None
            if open_seg is not None:
                segments.append((open_seg[0],open_seg[1],open_seg[2],ee[-1]['id'],ee[-1]['at']))
                if timed and open_seg[0]=='pause':tprobs.append(('pause_unclosed',open_seg[1]))
            if timed and state!='void':
                for p in pauses:
                    if _parts_wait(p['reason']) and replace_times and all(when(at)<when(p['at']) for _,at in replace_times):
                        tprobs.append(('part_before_pause',p['id']))
            if not ee or any(s['required'] and i not in done for i,s in steps.items()) or not signatures:
                problems.append(('missing_evidence',None))
            if not readings:problems.append(('missing_reading',None))
            if not parts:problems.append(('missing_part',None))
            timing=None
            if timed:
                if state=='void':
                    timing={'contract':contract,'settlement':'void','conclusion':None,'steps':[],'problems':[]}
                else:
                    tprob_codes=sorted({c for c,_ in tprobs}|({'sequence_gap'} if oid in gaps else set()))
                    if tprob_codes:
                        timing={'contract':contract,'settlement':'blocked','conclusion':None,'steps':[],'problems':tprob_codes}
                    else:
                        pause_by_id={p['id']:p for p in pauses};bounds={pos:(eid,at) for pos,eid,at in step_bounds}
                        step_results=[];prev=None
                        for pos in sorted(done):
                            beid,bat=bounds[pos];ws_id,ws_at=prev if prev else (start_ref or (None,None))
                            segs_out=[];dur=0.0
                            if ws_at is not None:
                                for skind,sid,sat,seid,seat in segments:
                                    s=max(when(sat),when(ws_at));en=min(when(seat),when(bat))
                                    if en<=s:continue
                                    mins=round((en-s).total_seconds()/60.0,6)
                                    if skind=='active':counted=True
                                    elif contract=='schedule':counted=True
                                    else:counted=not pause_by_id.get(sid,{}).get('reason')
                                    if counted:dur+=mins
                                    segs_out.append({'kind':skind,'start_event_id':sid,'end_event_id':seid,'minutes':mins,'counted':counted})
                            dur=round(dur,6);limit=steps[pos].get('time_limit_minutes')
                            over=round(dur-limit,6) if limit is not None else None
                            step_results.append({'position':pos,'window':{'start_event_id':ws_id,'end_event_id':beid},'duration_minutes':dur,'limit_minutes':limit,'overtime_minutes':over,'status':('逾期' if over>1e-9 else '按时') if over is not None else None,'segments':segs_out})
                            prev=(beid,bat)
                        complete=bool(signatures) and all(not s['required'] or i in done for i,s in steps.items())
                        if not complete:conclusion=None
                        elif any(s['status']=='逾期' for s in step_results):conclusion='逾期完成'
                        elif any(s['limit_minutes'] is not None for s in step_results):conclusion='按时完成'
                        else:conclusion=None
                        timing={'contract':contract,'settlement':'settled','conclusion':conclusion,'steps':step_results,'problems':[]}
            for code,eid in sorted(set(problems+tprobs)):
                db.execute('INSERT INTO issue(code,work_order,event_id,detail) VALUES (?,?,?,?)',(code,oid,eid,code))
            if state=='void':status='作废'
            elif tprobs:status='证据不足'
            elif any(c in SEVERE for c,_ in problems):status='需返工'
            elif problems:status='证据不足'
            else:status='完成'
            detail=json.dumps({'event_ids':[e['id'] for e in ee],'completed_steps':sorted(done),'reading_ids':readings,'replacement_ids':parts,'signature_ids':signatures,'issues':[c for c,_ in problems+tprobs],'timing':timing},ensure_ascii=False)
            db.execute('UPDATE work_order SET status=? WHERE id=?',('作废' if status=='作废' else '已判定',oid))
            db.execute('INSERT INTO decision VALUES (?,?,?,?,?)',(oid,run_id,status,detail,VERSION));out[oid]=status
        db.execute("UPDATE run SET status=CASE WHEN EXISTS(SELECT 1 FROM decision WHERE run_id=? AND status NOT IN ('完成','作废')) THEN '待核' ELSE '已判定' END,ended_at=CURRENT_TIMESTAMP WHERE id=?",(run_id,run_id))
    db.close();return out
