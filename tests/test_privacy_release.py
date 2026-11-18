"""Audience-bound privacy release: inference blocking, two-phase recovery,
isolation and full backward compatibility of the work-order report."""
import json,threading
from pathlib import Path

import pytest

from maintlog.__main__ import initialize,demo
from maintlog.ingest import ingest
from maintlog.rules import evaluate
from maintlog import technician_privacy as tp
from maintlog.technician_privacy import (register,review_publish,recover,
    reading_evidence,coarsen_proposal,INTERNAL_AUDIENCE,EXTERNAL_AUDIENCE)
from maintlog.inference import inference_audit
from maintlog.report import audience_reports,render_release,verify_release_file,report
from maintlog.store import connect

FIX=Path(__file__).resolve().parents[1]/'fixtures'

def _seed(tmp_path,fixture=FIX/'privacy-events.jsonl',case=FIX/'technician-privacy-case.json',case_id='PRIV-1'):
    db=tmp_path/'priv.db';out=tmp_path/'releases'
    initialize(db,FIX/'config.json')
    assert ingest(fixture,db)['imported']==11
    assert evaluate(db)=={'WO-1':'完成'}
    register(db,case_id,case)
    return db,out

def _case(tmp_path,rid,members,count=None,audience=EXTERNAL_AUDIENCE,prior=None):
    doc={'audience':audience,
         'prior':prior or {'id':'SEED','members':['P1','P2','P3'],'reading_count':6},
         'proposed':{'id':rid,'members':members,'reading_count':count},
         'contracts':[{'id':'internal_auditor','authorized_by':'MAINTENANCE-QA'},
                      {'id':'external_partner','authorized_by':'DATA-GOVERNANCE'}]}
    p=tmp_path/(rid+'.json');p.write_text(json.dumps(doc,ensure_ascii=False),encoding='utf-8')
    return p

def _rows(db):
    c=connect(db)
    out=[dict(r) for r in c.execute('SELECT * FROM release_ledger ORDER BY id')]
    c.close();return out

# ---------------------------------------------------------------- R1/R2 core
def test_external_r2_rejected_by_joint_subtraction(tmp_path):
    db,out=_seed(tmp_path)
    d=review_publish(db,FIX/'technician-privacy-case.json',EXTERNAL_AUDIENCE,out)
    assert d['decision']=='rejected' and d['reason']=='inference_leak'
    # exact algebraic proof: P3 = R1 - R2
    audit=inference_audit(['P1','P2','P3'],[
        {'report_id':'R1','members':['P1','P2','P3'],'count':6},
        {'report_id':'R2','members':['P1','P2'],'count':3}])
    assert set(audit['pinned'])=={'P3'}
    coef={x['report_id']:x['coefficient'] for x in audit['pinned']['P3']['derived_from']}
    assert coef=={'R1':1,'R2':-1}

def test_r1_alone_does_not_leak_and_r2_is_republishable_internally(tmp_path):
    db,out=_seed(tmp_path)
    assert inference_audit(['P1','P2','P3'],[
        {'report_id':'R1','members':['P1','P2','P3'],'count':6}])['pinned']=={}
    d=review_publish(db,FIX/'technician-privacy-case.json',INTERNAL_AUDIENCE,out)
    assert d['decision']=='published'
    doc=json.loads(Path(d['json_path']).read_text(encoding='utf-8'))
    assert doc['view']=='per_technician'
    assert doc['binding']['reading_count']==3  # binding covers the release group
    assert {x['operator']:x['reading_count'] for x in doc['technician_counts']}=={'P1':1,'P2':2,'P3':3}
    # the internal view still carries evidence for all three technicians
    assert {e['operator'] for e in doc['reading_evidence']}=={'P1','P2','P3'}
    # internal evidence stays verifiable along event/source hashes
    c=connect(db);src={r['sha256'] for r in c.execute('SELECT sha256 FROM source')};c.close()
    for e in doc['reading_evidence']:
        assert e['source_sha256'] in src and len(e['event_sha256'])==64

def test_rejection_row_never_stores_protected_count(tmp_path):
    db,out=_seed(tmp_path)
    review_publish(db,FIX/'technician-privacy-case.json',EXTERNAL_AUDIENCE,out)
    row=[r for r in _rows(db) if r['status']=='rejected'][0]
    assert row['reading_count'] is None and row['reason']=='inference_leak'
    assert row['evidence_digest'] and len(row['evidence_digest'])==64
    # no external file may exist for a rejected report
    assert not (out/EXTERNAL_AUDIENCE/'R2.json').exists()

def test_external_file_contains_no_payload_and_no_individual_counts(tmp_path):
    db,out=_seed(tmp_path)
    # a distinct, genuinely safe external aggregate (new report id, same total
    # over the same full member set is treated as duplicate; use a fresh id
    # whose group cannot be combined with R1 to pin anyone)
    p=_case(tmp_path,'R3',['P1','P2','P3'],6,
            prior={'id':'SEED','members':['P9'],'reading_count':0})
    # register without the fixture's R1 prior on a fresh partner audience
    fresh='PARTNER-Y'
    d=review_publish(db,p,fresh,out)
    assert d['decision']=='published'
    raw=Path(d['json_path']).read_text(encoding='utf-8')
    doc=json.loads(raw)
    assert doc['view']=='aggregate_inference_checked'
    assert 'payload' not in raw and 'reading_evidence' not in raw
    assert 'technician_counts' not in raw
    assert all(x not in raw for x in ('"P1":','"P2":','"P3":'))
    assert verify_release_file(d['json_path'],doc['report_digest'])

# ------------------------------------------------------------- multi-group
def test_multi_group_simultaneous_system_blocks_each_member(tmp_path):
    # any two pair-reports are safe; all three jointly pin every technician
    pairs=[('G1',['a','b']),('G2',['b','c']),('G3',['a','c'])]
    two=inference_audit(['a','b','c'],[{'report_id':i,'members':m,'count':1} for i,m in pairs[:2]])
    assert two['pinned']=={}
    full=inference_audit(['a','b','c'],[{'report_id':i,'members':m,'count':1} for i,m in pairs])
    assert set(full['pinned'])=={'a','b','c'}

def test_multigroup_fixture_subtraction_is_safe_but_three_way_joint_leaks(tmp_path):
    doc=json.loads((FIX/'technician-privacy-multigroup.json').read_text(encoding='utf-8'))
    prior=doc['prior'];proposal=doc['proposed']
    # R1-style four-person total minus a two-person total only exposes the
    # complementary pair sum — no individual is uniquely determined yet.
    two=inference_audit(sorted(set(prior['members'])|set(proposal['members'])),[
        {'report_id':prior['id'],'members':prior['members'],'count':prior['reading_count']},
        {'report_id':proposal['id'],'members':proposal['members'],'count':proposal['reading_count']}])
    assert two['pinned']=={}
    # two more overlapping pairs give full rank over T1,T2,T3; M1 then pins T4.
    extras=[{'report_id':'M3','members':['T1','T3'],'count':4},
            {'report_id':'M4','members':['T2','T3'],'count':5}]
    cols=sorted(set(prior['members'])|set(proposal['members'])|{m for e in extras for m in e['members']})
    three=inference_audit(cols,[
        {'report_id':prior['id'],'members':prior['members'],'count':prior['reading_count']},
        {'report_id':proposal['id'],'members':proposal['members'],'count':proposal['reading_count']},
        *extras])
    assert set(three['pinned'])=={'T1','T2','T3','T4'}

def test_duplicate_same_group_republish_is_idempotent_and_cannot_leak(tmp_path):
    db,out=_seed(tmp_path)
    audience='PARTNER-D'
    p1=_case(tmp_path,'D1',['P1','P2','P3'],6,audience=audience)
    assert review_publish(db,p1,audience,out)['decision']=='published'
    p2=_case(tmp_path,'D2',['P3','P2','P1'],6,audience=audience)  # same members, new name, same count
    d2=review_publish(db,p2,audience,out)
    assert d2['decision']=='rejected' and d2['reason']=='duplicate_group_release'
    # conflicting count for the same real member set is a conflict, not a release
    p3=_case(tmp_path,'D3',['P1','P2','P3'],99,audience=audience)
    d3=review_publish(db,p3,audience,out)
    assert d3['reason'] in ('count_mismatch','group_count_conflict')
    # still only one external file
    assert sorted(p.name for p in (out/audience).glob('*.json'))==['D1.json']

def test_operator_duplicate_readings_drive_real_counts(tmp_path):
    db,out=_seed(tmp_path)
    _,counts=reading_evidence(connect(db))
    assert dict(counts)=={'P1':1,'P2':2,'P3':3}  # operator repeats, not event rows
    # a singleton group with the true count still uniquely pins that technician
    p=_case(tmp_path,'BAD',['P1'],1)
    d=review_publish(db,p,'PARTNER-S',out)
    assert d['decision']=='rejected' and d['reason']=='inference_leak'
    # a proposal claiming a wrong exact count is rejected on evidence first
    p2=_case(tmp_path,'BAD2',['P1','P2','P3'],99)
    assert review_publish(db,p2,EXTERNAL_AUDIENCE,out)['reason']=='count_mismatch'

# ------------------------------------------------------------- source revision
def test_source_revision_changes_event_version_but_prior_is_remembered(tmp_path):
    db,out=_seed(tmp_path)
    c=connect(db)
    v1=c.execute('SELECT event_version FROM release_ledger').fetchone()[0]
    c.close()
    # re-import a revised source with one additional P3 reading (new device/seq)
    r=ingest(FIX/'privacy-events-revised.jsonl',db)
    assert r['imported']==1
    events,counts=reading_evidence(connect(db))
    assert counts['P3']==4
    from maintlog.technician_privacy import event_fingerprint
    v2,_=event_fingerprint(events);assert v2!=v1
    # the old R1 constraint still blocks R2 even after the correction
    d=review_publish(db,FIX/'technician-privacy-case.json',EXTERNAL_AUDIENCE,out)
    assert d['decision']=='rejected'
    # re-registering R1 against changed counts must fail loudly
    with pytest.raises(ValueError,match='prior_count_mismatch'):
        register(db,'PRIV-2',FIX/'technician-privacy-case.json')

# ------------------------------------------------------------- concurrency
def test_concurrent_candidates_each_safe_jointly_unsafe(tmp_path):
    db,out=_seed(tmp_path)
    # fresh audience so no prior; three pair groups over the three real
    # technicians (true counts P1=1,P2=2,P3=3): any pair is safe on its own,
    # but all three pair equations jointly pin every technician.
    audience='PARTNER-X'
    docs=[]
    for rid,members in (('X1',['P1','P2']),('X2',['P2','P3']),('X3',['P1','P3'])):
        d={'audience':audience,'prior':{'id':'SEED','members':['P1','P2','P3'],'reading_count':6},
           'proposed':{'id':rid,'members':members},
           'contracts':[{'id':'external_partner','authorized_by':'DG'}]}
        c=connect(db);tp._upsert_contracts(c,d);c.commit();c.close()
        p=tmp_path/(rid+'.json');p.write_text(json.dumps(d));docs.append(p)
    results=[];lock=threading.Lock()
    def worker(p):
        r=review_publish(db,p,audience,out)
        with lock:results.append(r)
    threads=[threading.Thread(target=worker,args=(p,)) for p in docs]
    for t in threads:t.start()
    for t in threads:t.join()
    decisions=sorted(r['decision'] for r in results)
    assert decisions.count('published')==2 and decisions.count('rejected')==1
    rejected=[r for r in results if r['decision']=='rejected'][0]
    assert rejected['reason']=='inference_leak'
    # ledger is consistent: placeholders never stuck, files match rows
    summary=recover(db,out)
    assert summary['aborted']==0
    live=[r for r in _rows(db) if r['audience']==audience]
    assert sorted(r['status'] for r in live)==['published','published','rejected']
    for r in live:
        if r['status']=='published':
            assert Path(r['json_path']).exists()
            assert verify_release_file(r['json_path'],r['report_digest'])

# ------------------------------------------------------------- crash recovery
class _Boom(Exception):pass

def test_crash_between_placeholder_and_file_is_recoverable(tmp_path,monkeypatch):
    db,out=_seed(tmp_path)
    audience='PARTNER-C'
    p=_case(tmp_path,'C1',['P1','P2','P3'],6,audience=audience)
    from maintlog import report as rpt
    def boom(*a,**k):raise _Boom('disk gone')
    # the protocol imports write_release lazily from maintlog.report
    monkeypatch.setattr(rpt,'write_release',boom)
    with pytest.raises(_Boom):
        review_publish(db,p,audience,out)
    row=[r for r in _rows(db) if r['report_id']=='C1'][0]
    assert row['status']=='aborted' and not (out/audience/'C1.json').exists()
    monkeypatch.undo()
    # aborted request can be retried as a new report id and completes
    p2=_case(tmp_path,'C1b',['P1','P2','P3'],6,audience=audience)
    d=review_publish(db,p2,audience,out)
    assert d['decision']=='published' and Path(d['json_path']).exists()

def test_crash_after_file_before_ledger_flip(tmp_path,monkeypatch):
    db,out=_seed(tmp_path)
    audience='PARTNER-K'
    p=_case(tmp_path,'K1',['P1','P2','P3'],6,audience=audience)
    from maintlog import report as rpt
    orig_write=rpt.write_release
    def spy(jp,mp,payload,md):
        orig_write(jp,mp,payload,md)
        raise _Boom('power loss after rename')
    # write_release is imported lazily inside the protocol; patch the source
    # module attribute the lazy import resolves to.
    monkeypatch.setattr(rpt,'write_release',spy)
    with pytest.raises(_Boom):
        review_publish(db,p,audience,out)
    monkeypatch.undo()
    # placeholder committed and file already on disk -> recovery completes it
    rows=[r for r in _rows(db) if r['report_id']=='K1']
    assert rows and rows[0]['status']=='placeholder'
    assert (out/audience/'K1.json').exists()
    recover(db,out)
    row=[r for r in _rows(db) if r['report_id']=='K1'][0]
    assert row['status']=='published'
    assert verify_release_file(row['json_path'],row['report_digest'])

def test_orphan_file_without_ledger_is_quarantined_not_trusted(tmp_path):
    db,out=_seed(tmp_path)
    d=out/EXTERNAL_AUDIENCE;d.mkdir(parents=True)
    rogue=d/'ROGUE.json';rogue.write_text(json.dumps({'reading_count':1,'operator':'P1'}))
    summary=recover(db,out)
    assert not rogue.exists() and summary['quarantined']
    assert all(Path(q).exists() for q in summary['quarantined'])

# ------------------------------------------------------------- audience isolation
def test_audiences_are_isolated_and_internal_report_keeps_full_payload(tmp_path):
    db,out=_seed(tmp_path)
    # external rejects R2; internal publishes the same id
    review_publish(db,FIX/'technician-privacy-case.json',EXTERNAL_AUDIENCE,out)
    di=review_publish(db,FIX/'technician-privacy-case.json',INTERNAL_AUDIENCE,out)
    ext_dir=out/EXTERNAL_AUDIENCE;int_dir=out/INTERNAL_AUDIENCE
    assert not (ext_dir/'R2.json').exists()
    assert (int_dir/'R2.json').exists() and (int_dir/'R2.md').exists()
    internal=json.loads(Path(di['json_path']).read_text())
    assert internal['reading_evidence'][0]['operator']=='P1'  # real operator preserved inside
    # external artifacts never carry the payload column content
    for f in ext_dir.rglob('*.json'):
        assert 'payload' not in f.read_text()
    # materialization is deterministic and digest-checked
    written=audience_reports(db,out)
    assert all(Path(w).exists() for w in written)

def test_original_work_order_report_unchanged(tmp_path):
    p=demo(tmp_path)
    data=json.loads(p.read_text(encoding='utf-8'))
    assert data['decision'][0]['status']=='完成'
    assert data['source'][0]['byte_length']>0 and len(data['evidence'])==6
    assert p.with_suffix('.md').read_text().startswith('# 设备保养履历')
    # full report still contains event.payload (internal work-order tool)
    assert all('payload' in e for e in data['event'])

# ------------------------------------------------------------- coarsening path
def test_coarsening_to_superset_can_release_without_new_inference(tmp_path):
    db,out=_seed(tmp_path)
    doc=json.loads(Path(FIX/'technician-privacy-case.json').read_text())
    with pytest.raises(ValueError):
        coarsen_proposal(doc['proposed'],['P1','P2'])  # not a strict superset
    # unsafe widening 1: same members as held R1 -> duplicate
    same=coarsen_proposal(doc['proposed'],['P1','P2','P3'])
    p=tmp_path/'coarse0.json';d0=dict(doc);d0['proposed']={**same,'id':'C0'};p.write_text(json.dumps(d0))
    r0=review_publish(db,p,EXTERNAL_AUDIENCE,out)
    assert r0['reason']=='duplicate_group_release'
    # unsafe widening 2: exactly one new member -> that member is pinned by subtraction
    extra1=tmp_path/'p4.jsonl'
    extra1.write_text(json.dumps({"kind":"reading","device":"TERM-2","seq":1,"work_order":"WO-1",
        "at":"2026-01-01T09:00:00+00:00","instrument":"I-1","unit":"C","value":30,"operator":"P4"}))
    ingest(extra1,db)
    w1=coarsen_proposal(doc['proposed'],['P1','P2','P3','P4'])
    p1=tmp_path/'coarse1.json';d1=dict(doc);d1['proposed']={**w1,'id':'C1'};p1.write_text(json.dumps(d1))
    assert review_publish(db,p1,EXTERNAL_AUDIENCE,out)['reason']=='inference_leak'
    # safe widening: at least two new unknowns so no individual is determined
    extra2=tmp_path/'p5.jsonl'
    extra2.write_text(json.dumps({"kind":"reading","device":"TERM-3","seq":1,"work_order":"WO-1",
        "at":"2026-01-01T09:05:00+00:00","instrument":"I-1","unit":"C","value":31,"operator":"P5"}))
    ingest(extra2,db)
    w2=coarsen_proposal(doc['proposed'],['P1','P2','P3','P4','P5'])
    p2=tmp_path/'coarse2.json';d2=dict(doc);d2['proposed']={**w2,'id':'C2'};p2.write_text(json.dumps(d2))
    r2=review_publish(db,p2,EXTERNAL_AUDIENCE,out)
    assert r2['decision']=='published'
    body=Path(r2['json_path']).read_text()
    assert all(k not in body for k in ('payload','technician_counts','reading_evidence'))

# ------------------------------------------------------------- identity binding
def test_release_identity_binds_audience_members_contract_and_versions(tmp_path):
    db,out=_seed(tmp_path)
    # fresh audience so this full-population group is the first release there
    p=_case(tmp_path,'B1',['P1','P2','P3'],6,audience='PARTNER-Z')
    d=review_publish(db,p,'PARTNER-Z',out)
    assert d['decision']=='published'
    row=[r for r in _rows(db) if r['report_id']=='B1'][0]
    assert row['audience']=='PARTNER-Z' and row['contract_id']=='external_partner'
    assert json.loads(row['members'])==['P1','P2','P3']
    assert len(row['event_version'])==64 and len(row['source_digest'])==64
    assert len(row['contract_digest'])==64 and row['report_digest']
    doc=json.loads(Path(d['json_path']).read_text())
    assert doc['binding']['event_version']==row['event_version']
    # renaming the same report cannot bypass the durable memory: on the
    # original partner audience R1 still blocks the overlapping R2 group
    p2=_case(tmp_path,'B2',['P1','P2'],3)
    assert review_publish(db,p2,EXTERNAL_AUDIENCE,out)['reason']=='inference_leak'
