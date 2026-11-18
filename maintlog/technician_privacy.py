"""Audience-bound technician privacy: review, reject and publish group stats.

Two pre-authorized audience contracts are supported:

* ``per_technician`` (internal audit) — exact per-technician evidence with the
  original reading events and source hashes may be released;
* ``aggregate_inference_checked`` (external partner) — only exact group totals
  may be released, and a candidate is rejected whenever the *joint* system of
  every report already held (or reserved) by that audience plus the candidate
  uniquely determines any individual technician's count.

Every decision runs one recoverable two-phase protocol: a durable ledger
placeholder is committed before any file is written, files are written
tmp+fsync+rename, and only then the ledger row is flipped to ``published``.
Crash recovery never leaves a readable file unknown to the ledger, nor a
``published`` row without a readable file.  Rejection rows never store the
protected reading count, only a reason code and a digest of the inference
evidence.
"""
import hashlib,json,time
from collections import Counter
from pathlib import Path
from .store import connect
from .inference import inference_audit

INTERNAL_AUDIENCE='INTERNAL-AUDIT'
EXTERNAL_AUDIENCE='COMMUNITY-PARTNER'
CONTRACT_MODES={'internal_auditor':'per_technician',
                'external_partner':'aggregate_inference_checked'}

def canon(obj):
    return json.dumps(obj,sort_keys=True,separators=(',',':'),ensure_ascii=False)
def digest(obj):
    return hashlib.sha256((canon(obj) if not isinstance(obj,(bytes,bytearray)) else bytes(obj)).encode('utf-8')).hexdigest()

def _load_doc(fixture):
    if isinstance(fixture,(str,Path)):
        raw=Path(fixture).resolve().read_bytes()
        return json.loads(raw),hashlib.sha256(raw).hexdigest(),str(Path(fixture).resolve())
    return fixture,digest(fixture),'<case-object>'

def reading_evidence(db):
    """Real operator evidence driven by event.payload (never by the proposal)."""
    events=[dict(e) for e in db.execute(
        "SELECT e.id,e.device,e.seq,e.work_order,e.kind,e.at,e.payload,e.sha256,s.sha256 AS source_sha256 "
        "FROM event e JOIN source s ON s.id=e.source_id WHERE e.kind='reading' ORDER BY e.id")]
    counts=Counter()
    for e in events:
        op=json.loads(e['payload']).get('operator')
        if not op:raise ValueError('operator_evidence_missing')
        counts[op]+=1
    if not events:raise ValueError('operator_evidence_missing')
    return events,counts

def event_fingerprint(events):
    version=digest([{'event_id':e['id'],'event_sha256':e['sha256'],'source_sha256':e['source_sha256']} for e in events])
    source_digest=digest(sorted({e['source_sha256'] for e in events}))
    return version,source_digest

def _upsert_contracts(db,doc,audience_override=None):
    out={}
    audience=audience_override or doc.get('audience')
    for c in doc.get('contracts',[]):
        cid=c['id'];mode=CONTRACT_MODES.get(cid)
        if not mode:raise ValueError('unknown_contract:'+cid)
        contract_audience=INTERNAL_AUDIENCE if mode=='per_technician' else audience
        body={'id':cid,'audience':contract_audience,'mode':mode,'authorized_by':c.get('authorized_by')}
        h=digest(body)
        db.execute('INSERT OR IGNORE INTO release_contract(id,audience,mode,authorized_by,digest) VALUES (?,?,?,?,?)',
                   (cid,contract_audience,mode,c.get('authorized_by'),h))
        out[cid]={'audience':contract_audience,'mode':mode,'digest':h}
    return out

def register(db_path,case_id,fixture):
    """Freeze real operator counts and the prior group statistic R1.

    The prior is an externally already-obtained report: it is mirrored into
    the durable release ledger so renaming reports, re-importing files or
    correcting readings can never make the protocol forget it.
    """
    path=Path(fixture).resolve();raw=path.read_bytes();doc=json.loads(raw)
    if not case_id or not doc.get('audience') or not doc.get('prior') or not doc.get('proposed'):
        raise ValueError('bad_technician_privacy_case')
    db=connect(db_path)
    try:
        with db:
            events,counts=reading_evidence(db)
            prior=doc['prior'];group=list(prior['members'])
            total=sum(counts[x] for x in group)
            if total!=prior['reading_count']:raise ValueError('prior_count_mismatch')
            source_sha=events[0]['source_sha256']
            db.execute('INSERT OR IGNORE INTO published_tech_stat VALUES (?,?,?,?,?)',
                       (prior['id'],doc['audience'],json.dumps(sorted(group)),total,source_sha))
            contracts=_upsert_contracts(db,doc)
            ext=contracts.get('external_partner')
            event_version,source_digest=event_fingerprint(events)
            report_summary={'report_id':prior['id'],'audience':doc['audience'],'members':sorted(group),
                            'reading_count':total,'event_version':event_version,'source_digest':source_digest}
            if ext:
                db.execute("""INSERT OR IGNORE INTO release_ledger
                    (report_id,audience,contract_id,members,reading_count,event_version,source_digest,
                     contract_digest,report_digest,status,reason,json_path,md_path,published_at)
                    VALUES (?,?,?,?,?,?,?,?,?,'published','prior_external_obtained',NULL,NULL,CURRENT_TIMESTAMP)""",
                    (prior['id'],doc['audience'],'external_partner',canon(sorted(group)),total,
                     event_version,source_digest,ext['digest'],digest(report_summary)))
                db.execute('INSERT INTO release_ledger_event(ledger_id,phase,detail) SELECT id,\'register\',\'prior_external_obtained\' FROM release_ledger WHERE report_id=? AND audience=?',
                           (prior['id'],doc['audience']))
            payload=json.dumps({'fixture':str(path),'case':doc,'counts':dict(counts),'events':events},sort_keys=True,separators=(',',':'))
            h=hashlib.sha256(raw).hexdigest()
            old=db.execute('SELECT digest FROM technician_privacy_case WHERE id=?',(case_id,)).fetchone()
            if old and old['digest']!=h:raise ValueError('privacy_case_conflict')
            db.execute('INSERT OR IGNORE INTO technician_privacy_case VALUES (?,?,?)',(case_id,payload,h))
    finally:db.close()
    return {'case_id':case_id,'digest':hashlib.sha256(raw).hexdigest(),'operators':len(counts)}

def _resolve_contract(db,audience,contract_id,doc):
    if contract_id is None:
        matches=[r for r in db.execute('SELECT * FROM release_contract WHERE audience=?',(audience,))]
        if not matches:
            # Bind the requested audience to the contract mode declared in the
            # case file (external audiences map to the external partner contract).
            _upsert_contracts(db,doc,audience_override=audience)
            matches=[r for r in db.execute('SELECT * FROM release_contract WHERE audience=?',(audience,))]
        if not matches:raise ValueError('no_contract_for_audience:'+audience)
        if len(matches)>1:raise ValueError('ambiguous_contract_for_audience:'+audience)
        row=matches[0]
    else:
        row=db.execute('SELECT * FROM release_contract WHERE id=?',(contract_id,)).fetchone()
        if not row:raise ValueError('unknown_contract:'+contract_id)
        if row['audience']!=audience:raise ValueError('contract_audience_mismatch')
    return dict(row)

def _held_constraints(db,audience):
    rows=db.execute("SELECT report_id,members,reading_count FROM release_ledger WHERE audience=? AND status IN ('placeholder','published') ORDER BY id",
                    (audience,)).fetchall()
    return [{'report_id':r['report_id'],'members':json.loads(r['members']),'count':r['reading_count']} for r in rows]

def _retry(fn,*args):
    last=None
    for _ in range(5):
        try:return fn(*args)
        except Exception as e:  # sqlite3.OperationalError database is locked
            if type(e).__name__=='OperationalError' and 'locked' in str(e):last=e;time.sleep(0.05);continue
            raise
    raise last

def review_publish(db_path,fixture,audience,out_dir,contract_id=None,report_id=None):
    """Review the fixture's proposed report for an audience and publish safely.

    Returns a decision dict: {'decision':'published'|'rejected', 'reason', ...}.
    The decision is durable: repeat calls return the recorded decision.
    """
    doc,_,_=_load_doc(fixture)
    if not doc.get('proposed'):raise ValueError('bad_technician_privacy_case')
    proposal=doc['proposed'];rid=report_id or proposal['id']
    members=sorted(proposal['members']);out_dir=Path(out_dir).resolve()
    db=connect(db_path)
    try:
        events,counts=reading_evidence(db)
        event_version,source_digest=event_fingerprint(events)
        true_count=sum(counts[m] for m in members if m in counts)
        return _retry(_review_txn,db,doc,audience,contract_id,rid,members,proposal.get('reading_count'),
                      true_count,event_version,source_digest,out_dir,events,counts)
    finally:db.close()

def _review_txn(db,doc,audience,contract_id,rid,members,claimed_count,true_count,event_version,source_digest,out_dir,events,counts):
    from .report import render_release
    db.execute('BEGIN IMMEDIATE')
    try:
        contract=_resolve_contract(db,audience,contract_id,doc)
        prior=db.execute('SELECT * FROM release_ledger WHERE audience=? AND report_id=?',(audience,rid)).fetchone()
        if prior:
            db.execute('INSERT INTO release_ledger_event(ledger_id,phase,detail) VALUES (?,?,?)',
                       (prior['id'],'review','idempotent_review'))
            db.commit()
            if prior['status']=='placeholder':
                # Another request is mid-protocol; wait for its terminal state
                # rather than handing back a transient placeholder.
                return _await_terminal(db,prior['id'])
            return _decision_from_row(dict(prior))
        reason=None;evidence_h=None
        # Evidence correctness is checked against the real payload first.
        if claimed_count is not None and claimed_count!=true_count:
            reason='count_mismatch'
        else:
            for r in db.execute("SELECT id,report_id,members,reading_count FROM release_ledger WHERE audience=? AND status IN ('placeholder','published')",(audience,)):
                if json.loads(r['members'])==members:
                    reason='group_count_conflict' if r['reading_count']!=true_count else 'duplicate_group_release'
                    break
        if reason is None and contract['mode']=='aggregate_inference_checked':
            constraints=_held_constraints(db,audience)
            columns=sorted(set(members)|{m for c in constraints for m in c['members']})
            constraints.append({'report_id':rid,'members':members,'count':true_count})
            audit=inference_audit(columns,constraints)
            if audit['pinned']:
                reason='inference_leak'
                evidence_h=digest({'pinned':audit['pinned'],'constraints':[
                    {'report_id':c['report_id'],'members':c['members']} for c in audit['constraints']]})
        json_path=str(out_dir/audience/(rid+'.json'));md_path=str(out_dir/audience/(rid+'.md'))
        ledger_seed={'report_id':rid,'audience':audience,'contract_id':contract['id'],
                     'members':sorted(members),'reading_count':true_count,'event_version':event_version,
                     'source_digest':source_digest,'contract_digest':contract['digest']}
        if reason:
            # Render nothing; the stored digest binds identity only and the
            # protected count is deliberately omitted from the rejection row.
            report_h=digest(ledger_seed)
            cur=db.execute("""INSERT INTO release_ledger
                (report_id,audience,contract_id,members,reading_count,event_version,source_digest,
                 contract_digest,report_digest,status,reason,evidence_digest,json_path,md_path)
                VALUES (?,?,?,?,NULL,?,?,?,?, 'rejected',?,?,?,?)""",
                (rid,audience,contract['id'],canon(members),event_version,source_digest,
                 contract['digest'],report_h,reason,evidence_h,json_path,md_path))
            lid=cur.lastrowid
            db.execute('INSERT INTO release_ledger_event(ledger_id,phase,detail) VALUES (?,?,?)',
                       (lid,'reject',reason))
            db.commit()
            return {'decision':'rejected','report_id':rid,'audience':audience,'reason':reason,
                    'evidence_digest':evidence_h,'ledger_id':lid}
        # Render the real artifact before reserving the row, so the ledger
        # stores the digest of exactly the bytes that will be published.
        seed_row=dict(ledger_seed);seed_row['members']=canon(members);seed_row['json_path']=json_path;seed_row['md_path']=md_path
        payload,md_text=render_release(db,seed_row,events,counts)
        report_h=digest(payload['binding'])
        if report_h!=digest(ledger_seed):
            raise RuntimeError('binding_digest_mismatch')
        payload['report_digest']=report_h
        md_text=render_release(db,{**seed_row,'report_digest':report_h},events,counts)[1]
        cur=db.execute("""INSERT INTO release_ledger
            (report_id,audience,contract_id,members,reading_count,event_version,source_digest,
             contract_digest,report_digest,status,reason,json_path,md_path)
            VALUES (?,?,?,?,?,?,?,?,?,'placeholder',NULL,?,?)""",
            (rid,audience,contract['id'],canon(members),true_count,event_version,source_digest,
             contract['digest'],report_h,json_path,md_path))
        lid=cur.lastrowid
        db.execute('INSERT INTO release_ledger_event(ledger_id,phase,detail) VALUES (?,?,?)',
                   (lid,'phase1','ledger_placeholder_committed'))
        db.commit()
    except Exception:
        db.rollback();raise
    # phase 2: files land only after the durable placeholder exists
    from .report import write_release
    try:
        row=dict(db.execute('SELECT * FROM release_ledger WHERE id=?',(lid,)).fetchone())
        payload,md=render_release(db,row,events,counts)
        if digest(payload['binding'])!=report_h or payload['report_digest']!=report_h:
            raise RuntimeError('report_digest_mismatch')
        write_release(Path(json_path),Path(md_path),payload,md)
    except Exception as exc:
        # If an artifact actually reached disk, keep the placeholder so
        # recover() can verify/complete it; only abort when nothing is readable.
        db.execute('BEGIN IMMEDIATE')
        if Path(json_path).exists():
            db.execute('INSERT INTO release_ledger_event(ledger_id,phase,detail) VALUES (?,?,?)',
                       (lid,'phase2_interrupted',type(exc).__name__))
            db.commit();raise
        db.execute("UPDATE release_ledger SET status='aborted',reason=? WHERE id=? AND status='placeholder'",
                   ('file_write_failed:'+type(exc).__name__,lid))
        db.execute('INSERT INTO release_ledger_event(ledger_id,phase,detail) VALUES (?,?,?)',
                   (lid,'abort','file_write_failed'))
        db.commit()
        raise
    db.execute('BEGIN IMMEDIATE')
    fresh=db.execute('SELECT status FROM release_ledger WHERE id=?',(lid,)).fetchone()
    if fresh['status']!='placeholder':
        db.commit();raise RuntimeError('ledger_row_not_placeholder:'+fresh['status'])
    db.execute("UPDATE release_ledger SET status='published',published_at=CURRENT_TIMESTAMP WHERE id=?",(lid,))
    db.execute('INSERT INTO release_ledger_event(ledger_id,phase,detail) VALUES (?,?,?)',
               (lid,'phase2','files_written_and_verified'))
    if contract['mode']=='aggregate_inference_checked':
        db.execute('INSERT OR IGNORE INTO published_tech_stat VALUES (?,?,?,?,?)',
                   (rid,audience,canon(members),true_count,
                    db.execute('SELECT sha256 FROM source ORDER BY id LIMIT 1').fetchone()[0]))
    db.commit()
    return {'decision':'published','report_id':rid,'audience':audience,'reason':None,
            'json_path':json_path,'md_path':md_path,'ledger_id':lid,'report_digest':report_h}

def _await_terminal(db,ledger_id,timeout=5.0):
    """Wait while a concurrent request is between placeholder and publish."""
    deadline=time.monotonic()+timeout
    while True:
        row=db.execute('SELECT * FROM release_ledger WHERE id=?',(ledger_id,)).fetchone()
        if row['status']!='placeholder':return _decision_from_row(dict(row))
        if time.monotonic()>=deadline:return _decision_from_row(dict(row))  # owner crashed; recover() resolves it
        time.sleep(0.02)

def _decision_from_row(row):
    if row['status']=='rejected':
        return {'decision':'rejected','report_id':row['report_id'],'audience':row['audience'],
                'reason':row['reason'],'evidence_digest':row['evidence_digest'],'ledger_id':row['id']}
    if row['status']=='aborted':
        return {'decision':'aborted','report_id':row['report_id'],'audience':row['audience'],
                'reason':row['reason'],'ledger_id':row['id']}
    return {'decision':row['status'],'report_id':row['report_id'],'audience':row['audience'],
            'reason':row['reason'],'json_path':row['json_path'],'md_path':row['md_path'],'ledger_id':row['id']}

def coarsen_proposal(proposal,members):
    """Return a coarser candidate over a strict superset of members.

    The true count must be supplied by the caller through review_publish,
    which recomputes it from real event.payload data; this helper only edits
    the proposal shape (it never invents a count).
    """
    members=sorted(set(members))
    if not set(proposal['members'])<=set(members) or members==sorted(proposal['members']):
        raise ValueError('coarsening_requires_strict_superset')
    out=dict(proposal);out['members']=members;out.pop('reading_count',None)
    return out

def recover(db_path,out_dir,audience=None):
    """Reconcile ledger and files after a crash.

    placeholder + no/partial files -> regenerate, verify digest, publish;
    anything that cannot be regenerated/verified -> aborted + quarantine;
    published + missing/unverifiable file -> regenerate or abort;
    readable files with no ledger row -> quarantine (never silently trusted).
    """
    out_dir=Path(out_dir).resolve();db=connect(db_path);summary={'published':0,'aborted':0,'quarantined':[]}
    try:
        db.execute('BEGIN IMMEDIATE')
        rows=db.execute('SELECT * FROM release_ledger WHERE audience=COALESCE(?,audience) ORDER BY id',
                        (audience,)).fetchall()
        known=set()
        for r in rows:
            row=dict(r)
            # Rejected rows deliberately have no artifacts; nothing to recover.
            if row['status']=='rejected':continue
            if not row['json_path']:continue  # externally obtained prior, no local artifact
            jp,mp=Path(row['json_path']),Path(row['md_path']);known.update((jp,mp))
            from .report import render_release,write_release,verify_release_file
            ok=jp.exists() and mp.exists() and verify_release_file(jp,row['report_digest'])
            if not ok:
                try:
                    events,counts=reading_evidence(db)
                    payload,md=render_release(db,row,events,counts)
                    if digest(payload['binding'])!=row['report_digest']:raise RuntimeError('regenerated_digest_mismatch')
                    for p in (jp,mp):
                        if p.exists():_quarantine(out_dir,p)
                    write_release(jp,mp,payload,md)
                    ok=True
                except Exception as exc:
                    for p in (jp,mp):
                        if p.exists():summary['quarantined'].append(_quarantine(out_dir,p))
                    if row['status']!='aborted':
                        db.execute("UPDATE release_ledger SET status='aborted',reason=COALESCE(reason,?) WHERE id=?",
                                   ('recovery_failed:'+type(exc).__name__,row['id']))
                        db.execute('INSERT INTO release_ledger_event(ledger_id,phase,detail) VALUES (?,?,?)',
                                   (row['id'],'recover','aborted'))
                        summary['aborted']+=1
                    continue
            if row['status']=='placeholder' and ok:
                db.execute("UPDATE release_ledger SET status='published',published_at=CURRENT_TIMESTAMP WHERE id=?",(row['id'],))
                db.execute('INSERT INTO release_ledger_event(ledger_id,phase,detail) VALUES (?,?,?)',
                           (row['id'],'recover','placeholder_completed'))
                summary['published']+=1
        db.commit()
    except Exception:
        db.rollback();raise
    finally:db.close()
    for p in out_dir.rglob('*'):
        if p.is_file() and p.suffix in ('.json','.md') and p not in known and '_quarantine' not in p.parts:
            summary['quarantined'].append(_quarantine(out_dir,p))
    return summary

def _quarantine(out_dir,path):
    qdir=out_dir/'_quarantine';qdir.mkdir(parents=True,exist_ok=True)
    stem=path.name+'.'+str(len(list(qdir.glob('*'))))
    target=qdir/stem
    try:Path(path).rename(target)
    except OSError:return None
    return str(target)
