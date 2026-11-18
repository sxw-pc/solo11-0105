import json,sqlite3
from pathlib import Path
import pytest
from maintlog.__main__ import initialize,main
from maintlog.ingest import ingest
from maintlog.rules import evaluate
from maintlog.report import report
from maintlog.safety_permit import register,grant,revoke,permits
from maintlog.store import connect

FIX=Path(__file__).resolve().parents[1]/'fixtures'
CASE=json.loads((FIX/'permit-case.json').read_text(encoding='utf-8'))
AT='2026-01-01T08:30:00+00:00'

def clone(case):
    return json.loads(json.dumps(case))

def setup(tmp_path,case=None,name='permit'):
    db=tmp_path/f'{name}.db';initialize(db,FIX/'permit-config.json')
    c=tmp_path/f'{name}.json';c.write_text(json.dumps(case if case is not None else CASE),encoding='utf-8')
    register(db,'PERMIT-1',c)
    return db

def start_rows(at=AT,seq=1,device='TERM-1'):
    return [{'kind':'start','device':device,'seq':seq,'work_order':'WO-P','at':at}]

def add(tmp_path,db,rows,name='e.jsonl'):
    p=tmp_path/name;p.write_text('\n'.join(json.dumps(r) for r in rows),encoding='utf-8');return ingest(p,db)

def attempts(db):
    c=connect(db);rows=[dict(r) for r in c.execute('SELECT * FROM start_attempt ORDER BY id')];c.close();return rows

def issues(db,code):
    c=connect(db);rows=[dict(r) for r in c.execute('SELECT * FROM issue WHERE code=?',(code,))];c.close();return rows

def grant_row(db,grant_id):
    c=connect(db);row=c.execute('SELECT * FROM permit_grant WHERE id=?',(grant_id,)).fetchone();c.close();return row

def codes(evidence):
    return {p['code'] for p in evidence['problems']}

def test_cutset_grant_permits_start(tmp_path):
    db=setup(tmp_path)
    g=grant(db,'PERMIT-1','equivalent_cutset',AT)
    assert g['granted'] and g['fence']
    paths=g['evidence']['paths']
    assert len(paths)==2 and all(p['cut_by'] and p['cut_by']['point']=='UP' for p in paths)
    res=add(tmp_path,db,start_rows())
    assert res['imported']==1 and res['starts'][0]['status']=='permitted'
    att=attempts(db)
    assert att[0]['status']=='permitted' and att[0]['grant_id']==g['grant_id']
    evaluate(db)
    assert not issues(db,'permit_denied')
    row=grant_row(db,g['grant_id'])
    assert row['status']=='consumed' and row['consumed_event']==att[0]['event_id']

def test_all_local_points_denied_and_start_blocked(tmp_path):
    db=setup(tmp_path)
    g=grant(db,'PERMIT-1','all_local_points',AT)
    assert not g['granted'] and g['fence'] is None
    unlocked={p['point'] for p in g['evidence']['problems'] if p['code']=='unlocked_local_point'}
    assert unlocked=={'BR-A','BR-B'}
    add(tmp_path,db,start_rows())
    att=attempts(db)
    assert att[0]['status']=='blocked' and att[0]['reason']=='no_active_permit'
    out=evaluate(db)
    assert out['WO-P']=='需返工'
    denied=issues(db,'permit_denied')
    assert denied and denied[0]['detail']=='no_active_permit'
    c=connect(db)
    assert c.execute("SELECT COUNT(*) FROM event WHERE kind='start'").fetchone()[0]==1
    assert c.execute('SELECT COUNT(*) FROM source').fetchone()[0]==1
    c.close()

def test_parallel_feed_path_uncut(tmp_path):
    case=clone(CASE);case['energy_edges'].append({'from':'GRID','to':'EQ-P'})
    db=setup(tmp_path,case)
    g=grant(db,'PERMIT-1','equivalent_cutset',AT)
    assert not g['granted']
    uncut=[p for p in g['evidence']['problems'] if p['code']=='uncut_path']
    assert uncut and uncut[0]['path']==['GRID','EQ-P']
    direct=[p for p in g['evidence']['paths'] if p['cut_by'] is None]
    assert [p['nodes'] for p in direct]==[['GRID','EQ-P']]

def test_branch_locks_satisfy_all_local_points(tmp_path):
    case=clone(CASE)
    case['lock_events']+=[
        {'id':'LOCK-2','point':'BR-A','action':'lock','at':'2026-01-01T08:02:00+00:00','staff':'OPS-1','verified_by':'SAFE-1','seal':'L-002'},
        {'id':'LOCK-3','point':'BR-B','action':'lock','at':'2026-01-01T08:03:00+00:00','staff':'OPS-1','verified_by':'SAFE-1','seal':'L-003'}]
    db=setup(tmp_path,case)
    g=grant(db,'PERMIT-1','all_local_points',AT)
    assert g['granted']
    assert all(p['locked'] for p in g['evidence']['local_points'])
    add(tmp_path,db,start_rows())
    assert attempts(db)[0]['status']=='permitted'

def test_missing_signature_blocks(tmp_path):
    case=clone(CASE);case['signatures']=[s for s in case['signatures'] if s['role']!='safety']
    db=setup(tmp_path,case)
    g=grant(db,'PERMIT-1','equivalent_cutset',AT)
    assert not g['granted']
    assert {'code':'missing_signature','role':'safety'} in g['evidence']['problems']

def test_unlock_then_relock_without_review_blocks(tmp_path):
    case=clone(CASE)
    case['lock_events']+=[
        {'id':'UNLOCK-1','point':'UP','action':'unlock','at':'2026-01-01T08:10:00+00:00','staff':'OPS-1','seal':'L-001'},
        {'id':'LOCK-1B','point':'UP','action':'lock','at':'2026-01-01T08:20:00+00:00','staff':'OPS-1','seal':'L-001B'}]
    db=setup(tmp_path,case)
    g=grant(db,'PERMIT-1','equivalent_cutset',AT)
    assert not g['granted']
    assert 'unverified_relock' in codes(g['evidence'])

def test_unlock_between_grant_and_start_blocks(tmp_path):
    case=clone(CASE)
    case['lock_events'].append({'id':'UNLOCK-1','point':'UP','action':'unlock','at':'2026-01-01T08:45:00+00:00','staff':'OPS-1','seal':'L-001'})
    db=setup(tmp_path,case)
    g=grant(db,'PERMIT-1','equivalent_cutset',AT)
    assert g['granted']
    add(tmp_path,db,start_rows(at='2026-01-01T08:50:00+00:00'))
    att=attempts(db)
    assert att[0]['status']=='blocked' and att[0]['reason']=='lock_state_changed'
    evaluate(db)
    assert issues(db,'permit_denied')
    assert grant_row(db,g['grant_id'])['status']=='active'

def test_late_start_blocked(tmp_path):
    db=setup(tmp_path)
    g=grant(db,'PERMIT-1','equivalent_cutset',AT)
    assert g['granted']
    add(tmp_path,db,start_rows(at='2026-01-01T09:30:00+00:00'))
    att=attempts(db)
    assert att[0]['status']=='blocked' and att[0]['reason']=='permit_expired'
    late=grant(db,'PERMIT-1','equivalent_cutset','2026-01-01T09:30:00+00:00')
    assert not late['granted'] and 'start_deadline_exceeded' in codes(late['evidence'])

def test_one_time_fence_and_sequence_conflict(tmp_path):
    db=setup(tmp_path)
    g=grant(db,'PERMIT-1','equivalent_cutset',AT)
    assert g['granted']
    add(tmp_path,db,start_rows(),'a.jsonl')
    assert ingest(tmp_path/'a.jsonl',db)['duplicate_file']
    conflict=[{'kind':'start','device':'TERM-1','seq':1,'work_order':'WO-P','at':'2026-01-01T08:31:00+00:00'}]
    assert add(tmp_path,db,conflict,'b.jsonl')['imported']==0
    assert issues(db,'identity_conflict')
    add(tmp_path,db,start_rows(at='2026-01-01T08:40:00+00:00',seq=2),'c.jsonl')
    att=attempts(db)
    assert [a['status'] for a in att]==['permitted','blocked']
    assert att[1]['reason']=='no_active_permit'
    c=connect(db)
    assert c.execute("SELECT COUNT(*) FROM permit_grant WHERE status='consumed'").fetchone()[0]==1
    assert c.execute("SELECT COUNT(*) FROM event WHERE kind='start'").fetchone()[0]==2
    c.close()

def test_steps_execute_under_valid_permit(tmp_path):
    db=setup(tmp_path)
    g=grant(db,'PERMIT-1','equivalent_cutset',AT)
    assert g['granted']
    rows=start_rows()+[
        {'kind':'step','device':'TERM-1','seq':2,'work_order':'WO-P','at':'2026-01-01T08:35:00+00:00','position':1,'procedure_version':'P1','result':'pass'},
        {'kind':'sign','device':'TERM-1','seq':3,'work_order':'WO-P','at':'2026-01-01T08:40:00+00:00','signer':'tech-9'}]
    add(tmp_path,db,rows)
    evaluate(db)
    assert not issues(db,'permit_denied') and not issues(db,'inactive_event')
    c=connect(db)
    detail=json.loads(c.execute("SELECT detail FROM decision WHERE work_order='WO-P'").fetchone()['detail'])
    c.close()
    assert detail['completed_steps']==[1] and detail['signature_ids']
    assert detail['start_attempts'][0]['status']=='permitted'

def test_revoke_invalidates_token(tmp_path):
    db=setup(tmp_path)
    g=grant(db,'PERMIT-1','equivalent_cutset',AT)
    assert g['granted']
    assert revoke(db,g['grant_id'])=={'revoked':g['grant_id']}
    add(tmp_path,db,start_rows())
    att=attempts(db)
    assert att[0]['status']=='blocked' and att[0]['reason']=='no_active_permit'
    with pytest.raises(ValueError,match='permit_not_active'):
        revoke(db,g['grant_id'])

def test_graph_change_invalidates_grant(tmp_path):
    db=setup(tmp_path)
    g=grant(db,'PERMIT-1','equivalent_cutset',AT)
    assert g['granted']
    case2=clone(CASE);case2['energy_edges'].append({'from':'GRID','to':'EQ-P'})
    c2=tmp_path/'case2.json';c2.write_text(json.dumps(case2),encoding='utf-8')
    register(db,'PERMIT-2',c2)
    add(tmp_path,db,start_rows())
    att=attempts(db)
    assert att[0]['status']=='blocked' and att[0]['reason']=='stale_case'

def test_topology_defects_block(tmp_path):
    case=clone(CASE);case['energy_edges'].append({'from':'EQ-P','to':'UP'})
    db=setup(tmp_path,case,'cycle')
    g=grant(db,'PERMIT-1','equivalent_cutset',AT)
    assert not g['granted'] and 'cycle' in codes(g['evidence'])
    case=clone(CASE);case['energy_edges'].append({'from':'GEN-X','to':'EQ-P'})
    db=setup(tmp_path,case,'feed')
    g=grant(db,'PERMIT-1','equivalent_cutset',AT)
    assert not g['granted'] and 'unknown_feed_point' in codes(g['evidence'])
    case=clone(CASE);case['energy_edges']=[]
    db=tmp_path/'empty.db';initialize(db,FIX/'permit-config.json')
    c=tmp_path/'case3.json';c.write_text(json.dumps(case),encoding='utf-8')
    with pytest.raises(ValueError,match='bad_permit_case'):
        register(db,'PERMIT-1',c)
    case=clone(CASE)
    case['energy_edges']=[e for e in case['energy_edges'] if e['to']!='EQ-P']
    db=setup(tmp_path,case,'unreach')
    g=grant(db,'PERMIT-1','equivalent_cutset',AT)
    assert not g['granted'] and 'no_source_path' in codes(g['evidence'])

def test_seal_mismatch_blocks(tmp_path):
    case=clone(CASE)
    case['lock_events'].append({'id':'UNLOCK-1','point':'UP','action':'unlock','at':'2026-01-01T08:10:00+00:00','staff':'OPS-1','seal':'L-999'})
    db=setup(tmp_path,case)
    g=grant(db,'PERMIT-1','equivalent_cutset',AT)
    assert not g['granted'] and 'seal_mismatch' in codes(g['evidence'])

def test_clock_conflict_blocks(tmp_path):
    case=clone(CASE)
    case['lock_events']=[{'id':'U-1','point':'UP','action':'unlock','at':'2026-01-01T08:00:00+00:00','staff':'OPS-1','seal':'L-001'}]
    db=setup(tmp_path,case)
    g=grant(db,'PERMIT-1','equivalent_cutset',AT)
    assert not g['granted'] and 'clock_conflict' in codes(g['evidence'])

def test_permit_consumption_rolls_back_with_ingest(tmp_path):
    db=setup(tmp_path)
    g=grant(db,'PERMIT-1','equivalent_cutset',AT)
    assert g['granted']
    c=connect(db)
    c.execute("CREATE TRIGGER reject_attempt BEFORE INSERT ON start_attempt BEGIN SELECT RAISE(ABORT,'injected'); END")
    c.commit();c.close()
    with pytest.raises(sqlite3.IntegrityError):
        add(tmp_path,db,start_rows())
    c=connect(db)
    assert c.execute("SELECT status FROM permit_grant WHERE id=?",(g['grant_id'],)).fetchone()['status']=='active'
    assert c.execute('SELECT COUNT(*) FROM event').fetchone()[0]==0
    assert c.execute('SELECT COUNT(*) FROM source').fetchone()[0]==0
    c.close()

def test_isolation_evidence_report(tmp_path):
    db=setup(tmp_path)
    g=grant(db,'PERMIT-1','equivalent_cutset',AT)
    add(tmp_path,db,start_rows())
    evaluate(db)
    p=report(db,tmp_path/'report.json')
    data=json.loads(p.read_text(encoding='utf-8'))
    assert data['permit_grant'] and data['permit_grant'][0]['fence']==g['fence']
    assert data['permit_grant'][0]['status']=='consumed'
    assert data['start_attempt'][0]['status']=='permitted'
    assert data['permit_adjudication'][0]['outcome']=='granted'
    md=p.with_suffix('.md').read_text(encoding='utf-8')
    assert '隔离许可' in md and g['fence'] in md

def test_permit_cli(tmp_path,monkeypatch,capsys):
    db=tmp_path/'cli.db'
    monkeypatch.setattr('sys.argv',['maintlog','init','--db',str(db),'--config',str(FIX/'permit-config.json')]);main()
    monkeypatch.setattr('sys.argv',['maintlog','register','--db',str(db),'--case','PERMIT-1','--fixture',str(FIX/'permit-case.json')]);main()
    monkeypatch.setattr('sys.argv',['maintlog','permit','--db',str(db),'--case','PERMIT-1','--contract','equivalent_cutset','--at',AT]);main()
    out=json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert out['granted'] and out['fence']
    monkeypatch.setattr('sys.argv',['maintlog','ingest',str(FIX/'permit-start.jsonl'),'--db',str(db)]);main()
    res=json.loads(json.dumps(capsys.readouterr().out.strip()))
    assert "'status': 'permitted'" in res or '"status": "permitted"' in res
    monkeypatch.setattr('sys.argv',['maintlog','permits','--db',str(db)]);main()
    listing=json.loads(capsys.readouterr().out)
    assert listing['grants'][0]['status']=='consumed' and listing['attempts'][0]['status']=='permitted'
