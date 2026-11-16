import json
from maintlog.__main__ import initialize
from maintlog.ingest import ingest
from maintlog.rules import evaluate, VERSION
from maintlog.store import connect
from maintlog.report import report
WO='WO-T'
def ev(kind,seq,at,**kw):return {'kind':kind,'device':'T1','seq':seq,'work_order':WO,'at':at,**kw}
def config(contract,limit1=None,limit2=None):
    s1={'work_order':WO,'position':1,'code':'isolate','required':1}
    s2={'work_order':WO,'position':2,'code':'inspect','required':1}
    if limit1 is not None:s1['time_limit_minutes']=limit1
    if limit2 is not None:s2['time_limit_minutes']=limit2
    return {'work_order':[{'id':WO,'equipment':'EQ-1','procedure_version':'P1','status':'待接收','contract':contract}],
            'step':[s1,s2],
            'instrument':[{'id':'I-1','unit':'C','min':0,'max':100,'calibrated_until':'2027-01-01T00:00:00+00:00'}],
            'stock':[{'part':'NEW-1','issued_to':WO}]}
def main_scenario():
    # 步骤1：08:00 激活，08:04 因等待备件暂停，08:24 恢复（暂停 20 分钟），08:29 完成
    # 实际作业 4+5=9 分钟，墙钟 29 分钟
    return [ev('start',1,'2026-01-01T08:00:00+00:00'),
            ev('pause',2,'2026-01-01T08:04:00+00:00',reason='waiting_parts'),
            ev('resume',3,'2026-01-01T08:24:00+00:00'),
            ev('replace',4,'2026-01-01T08:26:00+00:00',old='OLD-1',new='NEW-1'),
            ev('step',5,'2026-01-01T08:29:00+00:00',position=1,procedure_version='P1',result='pass'),
            ev('reading',6,'2026-01-01T08:30:00+00:00',instrument='I-1',unit='C',value=25),
            ev('step',7,'2026-01-01T08:35:00+00:00',position=2,procedure_version='P1',result='pass'),
            ev('sign',8,'2026-01-01T08:36:00+00:00',signer='tech-1')]
def run_case(tmp_path,cfg,events,name='case'):
    db=tmp_path/f'{name}.db'
    c=tmp_path/f'{name}.json';c.write_text(json.dumps(cfg),encoding='utf-8')
    initialize(db,c)
    f=tmp_path/f'{name}.jsonl';f.write_text('\n'.join(json.dumps(e) for e in events),encoding='utf-8')
    ing=ingest(f,db)
    status=evaluate(db)
    return db,status,ing
def decisions(db):
    c=connect(db);rows=[dict(r) for r in c.execute('SELECT * FROM decision WHERE work_order=? ORDER BY run_id',(WO,))];c.close();return rows
def timing(db):return json.loads(decisions(db)[-1]['detail'])['timing']
def test_two_contracts_same_events(tmp_path):
    # 同一组事件：时限 10 分钟、实际作业 9 分钟、等待备件暂停 20 分钟
    db,status,_=run_case(tmp_path,config('schedule',limit1=10),main_scenario(),'sched')
    assert status[WO]=='完成'  # 技术验收状态保留
    t=timing(db)
    assert t['settlement']=='settled' and t['conclusion']=='逾期完成'  # 排程契约：墙钟 29>10
    s1=t['steps'][0]
    assert s1['position']==1 and s1['duration_minutes']==29.0 and s1['overtime_minutes']==19.0 and s1['status']=='逾期'
    assert s1['window']['start_event_id'] and s1['window']['end_event_id']
    assert all(seg['start_event_id'] and seg['end_event_id'] for seg in s1['segments'])  # 每段引用原始事件 ID
    db2,status2,_=run_case(tmp_path,config('piecework',limit1=10),main_scenario(),'piece')
    assert status2[WO]=='完成'
    t2=timing(db2)
    assert t2['settlement']=='settled' and t2['conclusion']=='按时完成'  # 计件契约：有效作业 9<=10
    s2=t2['steps'][0]
    assert s2['duration_minutes']==9.0 and s2['overtime_minutes']==-1.0 and s2['status']=='按时'
    pauses=[s for s in s2['segments'] if s['kind']=='pause']
    assert len(pauses)==1 and pauses[0]['minutes']==20.0 and pauses[0]['counted'] is False
def test_report_explains_overdue_step(tmp_path):
    db,_,_=run_case(tmp_path,config('schedule',limit1=10),main_scenario())
    p=report(db,tmp_path/'r.json')
    md=p.with_suffix('.md').read_text(encoding='utf-8')
    assert '逾期完成' in md and '步骤1' in md and '+19.0' in md
def test_consecutive_pauses(tmp_path):
    # 连续两段暂停：08:04-08:24 等备件 20 分钟、08:30-08:40 交接 10 分钟，作业 4+6+1=11 分钟
    events=[ev('start',1,'2026-01-01T08:00:00+00:00'),
            ev('pause',2,'2026-01-01T08:04:00+00:00',reason='waiting_parts'),
            ev('resume',3,'2026-01-01T08:24:00+00:00'),
            ev('replace',4,'2026-01-01T08:26:00+00:00',old='OLD-1',new='NEW-1'),
            ev('pause',5,'2026-01-01T08:30:00+00:00',reason='shift_break'),
            ev('resume',6,'2026-01-01T08:40:00+00:00'),
            ev('step',7,'2026-01-01T08:41:00+00:00',position=1,procedure_version='P1',result='pass'),
            ev('reading',8,'2026-01-01T08:42:00+00:00',instrument='I-1',unit='C',value=25),
            ev('step',9,'2026-01-01T08:43:00+00:00',position=2,procedure_version='P1',result='pass'),
            ev('sign',10,'2026-01-01T08:44:00+00:00',signer='tech-1')]
    db,status,_=run_case(tmp_path,config('piecework',limit1=15),events)
    assert status[WO]=='完成'
    s1=timing(db)['steps'][0]
    assert s1['duration_minutes']==11.0 and s1['status']=='按时'
    pauses=[s for s in s1['segments'] if s['kind']=='pause']
    assert [p['minutes'] for p in pauses]==[20.0,10.0] and all(p['counted'] is False for p in pauses)
    db2,_,_=run_case(tmp_path,config('schedule',limit1=15),events,'sched')
    assert timing(db2)['steps'][0]['duration_minutes']==41.0
def test_unevidenced_pause_counts_under_piecework(tmp_path):
    # 计件契约只剔除有证据的暂停；无理由的暂停仍计入
    events=main_scenario();del events[1]['reason']
    db,_,_=run_case(tmp_path,config('piecework',limit1=10),events)
    t=timing(db)
    assert t['steps'][0]['duration_minutes']==29.0 and t['conclusion']=='逾期完成'
def test_missing_resume_blocks_settlement(tmp_path):
    events=[ev('start',1,'2026-01-01T08:00:00+00:00'),
            ev('step',2,'2026-01-01T08:05:00+00:00',position=1,procedure_version='P1',result='pass'),
            ev('reading',3,'2026-01-01T08:06:00+00:00',instrument='I-1',unit='C',value=25),
            ev('replace',4,'2026-01-01T08:07:00+00:00',old='OLD-1',new='NEW-1'),
            ev('step',5,'2026-01-01T08:08:00+00:00',position=2,procedure_version='P1',result='pass'),
            ev('pause',6,'2026-01-01T08:09:00+00:00',reason='waiting_parts')]
    db,status,_=run_case(tmp_path,config('schedule',limit1=10),events)
    assert status[WO]=='证据不足'
    t=timing(db)
    assert t['settlement']=='blocked' and 'pause_unclosed' in t['problems']
    assert t['steps']==[] and t['conclusion'] is None  # 不结算，也不按零时长或更有利时钟处理
def test_part_issued_before_pause_blocks(tmp_path):
    # 备件领用（08:02）早于暂停（08:04），与“等待备件”矛盾
    events=[ev('start',1,'2026-01-01T08:00:00+00:00'),
            ev('replace',2,'2026-01-01T08:02:00+00:00',old='OLD-1',new='NEW-1'),
            ev('pause',3,'2026-01-01T08:04:00+00:00',reason='waiting_parts'),
            ev('resume',4,'2026-01-01T08:24:00+00:00'),
            ev('step',5,'2026-01-01T08:29:00+00:00',position=1,procedure_version='P1',result='pass'),
            ev('reading',6,'2026-01-01T08:30:00+00:00',instrument='I-1',unit='C',value=25),
            ev('step',7,'2026-01-01T08:35:00+00:00',position=2,procedure_version='P1',result='pass'),
            ev('sign',8,'2026-01-01T08:36:00+00:00',signer='tech-1')]
    db,status,_=run_case(tmp_path,config('piecework',limit1=10),events)
    assert status[WO]=='证据不足'
    t=timing(db)
    assert t['settlement']=='blocked' and 'part_before_pause' in t['problems'] and t['conclusion'] is None
def test_step_signed_during_pause_blocks(tmp_path):
    events=[ev('start',1,'2026-01-01T08:00:00+00:00'),
            ev('pause',2,'2026-01-01T08:04:00+00:00',reason='waiting_parts'),
            ev('step',3,'2026-01-01T08:10:00+00:00',position=1,procedure_version='P1',result='pass'),
            ev('resume',4,'2026-01-01T08:24:00+00:00'),
            ev('replace',5,'2026-01-01T08:26:00+00:00',old='OLD-1',new='NEW-1'),
            ev('reading',6,'2026-01-01T08:30:00+00:00',instrument='I-1',unit='C',value=25),
            ev('sign',7,'2026-01-01T08:36:00+00:00',signer='tech-1')]
    db,status,_=run_case(tmp_path,config('schedule',limit1=10),events)
    assert status[WO]=='证据不足'
    t=timing(db)
    assert t['settlement']=='blocked' and 'step_during_pause' in t['problems']
def test_sequence_gap_blocks_settlement(tmp_path):
    events=main_scenario();events[-1]['seq']=90  # 终端序号缺口
    db,status,_=run_case(tmp_path,config('schedule',limit1=10),events)
    assert status[WO]=='证据不足'
    t=timing(db)
    assert t['settlement']=='blocked' and 'sequence_gap' in t['problems'] and t['steps']==[]
def test_clock_regression_blocks_settlement(tmp_path):
    events=[ev('start',1,'2026-01-01T08:00:00+00:00'),
            ev('step',2,'2026-01-01T08:05:00+00:00',position=1,procedure_version='P1',result='pass'),
            ev('reading',3,'2026-01-01T08:10:00+00:00',instrument='I-1',unit='C',value=25),
            ev('replace',4,'2026-01-01T08:15:00+00:00',old='OLD-1',new='NEW-1'),
            ev('step',5,'2026-01-01T08:20:00+00:00',position=2,procedure_version='P1',result='pass'),
            ev('sign',6,'2026-01-01T08:06:00+00:00',signer='tech-1')]  # 时钟倒退
    db,status,_=run_case(tmp_path,config('piecework',limit1=10),events)
    assert status[WO]=='证据不足'
    t=timing(db)
    assert t['settlement']=='blocked' and 'clock_regression' in t['problems'] and t['steps']==[]
def test_void_order_not_zero_duration(tmp_path):
    events=[ev('start',1,'2026-01-01T08:00:00+00:00'),ev('void',2,'2026-01-01T08:05:00+00:00')]
    db,status,_=run_case(tmp_path,config('schedule',limit1=10),events)
    assert status[WO]=='作废'
    t=timing(db)
    assert t['settlement']=='void' and t['steps']==[] and t['conclusion'] is None
def test_pause_between_steps_belongs_to_next_step(tmp_path):
    # 步骤1 完成后、步骤2 完成前的暂停归属步骤2 的窗口
    events=[ev('start',1,'2026-01-01T08:00:00+00:00'),
            ev('step',2,'2026-01-01T08:05:00+00:00',position=1,procedure_version='P1',result='pass'),
            ev('pause',3,'2026-01-01T08:06:00+00:00',reason='shift_break'),
            ev('resume',4,'2026-01-01T08:26:00+00:00'),
            ev('step',5,'2026-01-01T08:30:00+00:00',position=2,procedure_version='P1',result='pass'),
            ev('reading',6,'2026-01-01T08:31:00+00:00',instrument='I-1',unit='C',value=25),
            ev('replace',7,'2026-01-01T08:32:00+00:00',old='OLD-1',new='NEW-1'),
            ev('sign',8,'2026-01-01T08:33:00+00:00',signer='tech-1')]
    db,status,_=run_case(tmp_path,config('piecework',limit1=10,limit2=10),events)
    assert status[WO]=='完成'
    steps={s['position']:s for s in timing(db)['steps']}
    assert steps[1]['duration_minutes']==5.0 and all(s['kind']=='active' for s in steps[1]['segments'])
    assert steps[2]['duration_minutes']==5.0
    pauses=[s for s in steps[2]['segments'] if s['kind']=='pause']
    assert len(pauses)==1 and pauses[0]['minutes']==20.0 and pauses[0]['counted'] is False
    db2,_,_=run_case(tmp_path,config('schedule',limit1=10,limit2=10),events,'sched')
    steps2={s['position']:s for s in timing(db2)['steps']}
    assert steps2[1]['status']=='按时' and steps2[2]['duration_minutes']==25.0 and steps2[2]['status']=='逾期'
def test_config_change_rerun_keeps_old_run(tmp_path):
    db=tmp_path/'r.db'
    c1=tmp_path/'c1.json';c1.write_text(json.dumps(config('schedule',limit1=10)),encoding='utf-8')
    initialize(db,c1)
    f=tmp_path/'e.jsonl';f.write_text('\n'.join(json.dumps(e) for e in main_scenario()),encoding='utf-8')
    ingest(f,db)
    assert evaluate(db)[WO]=='完成'
    c2=tmp_path/'c2.json';c2.write_text(json.dumps(config('schedule',limit1=40)),encoding='utf-8')
    initialize(db,c2)  # 更改时限配置
    assert evaluate(db)[WO]=='完成'  # 新 Run 重算
    rows=decisions(db)
    assert len(rows)==2
    old,new=(json.loads(r['detail'])['timing'] for r in rows)
    assert rows[0]['rule_version']==VERSION and rows[1]['rule_version']==VERSION  # 旧 Run 保留其规则版本
    assert old['steps'][0]['limit_minutes']==10 and old['conclusion']=='逾期完成'  # 旧 Run 保留其输入
    assert new['steps'][0]['limit_minutes']==40 and new['steps'][0]['duration_minutes']==29.0 and new['conclusion']=='按时完成'
    c=connect(db);assert c.execute('SELECT count(*) FROM run').fetchone()[0]==2;c.close()
def test_duplicate_ingest_not_double_counted(tmp_path):
    db=tmp_path/'d.db'
    c=tmp_path/'c.json';c.write_text(json.dumps(config('schedule',limit1=10)),encoding='utf-8')
    initialize(db,c)
    f=tmp_path/'e.jsonl';f.write_text('\n'.join(json.dumps(e) for e in main_scenario()),encoding='utf-8')
    assert ingest(f,db)['imported']==8
    assert ingest(f,db)['duplicate_file']  # 重复导入被拒绝
    evaluate(db)
    assert timing(db)['steps'][0]['duration_minutes']==29.0
    evaluate(db)  # 重算幂等，不重复累计
    assert timing(db)['steps'][0]['duration_minutes']==29.0
