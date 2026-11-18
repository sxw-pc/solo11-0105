import json,os,tempfile
from pathlib import Path
from .store import connect
from .technician_privacy import canon, digest

def report(db_path,out):
    """Original full work-order report (internal); behavior unchanged."""
    db=connect(db_path);data={t:[dict(x) for x in db.execute(f'SELECT * FROM {t} ORDER BY 1')] for t in ('work_order','step','instrument','stock','event','evidence','issue','decision','run')}
    data['source']=[dict(x) for x in db.execute('SELECT id,name,sha256,length(raw) AS byte_length FROM source ORDER BY id')]
    p=Path(out);p.parent.mkdir(parents=True,exist_ok=True);p.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
    p.with_suffix('.md').write_text('# 设备保养履历\n\n'+'\n'.join(f"- {r['work_order']}: {r['status']}" for r in data['decision']),encoding='utf-8');db.close();return p

def _reading_rows(db):
    return [dict(e) for e in db.execute(
        "SELECT e.id,e.device,e.seq,e.work_order,e.at,e.payload,e.sha256,s.sha256 AS source_sha256 "
        "FROM event e JOIN source s ON s.id=e.source_id WHERE e.kind='reading' ORDER BY e.id")]

def render_release(db,row,events=None,counts=None):
    """Render the actual audience-isolated release artifact for a ledger row.

    per_technician (internal): exact per-technician evidence, each reading
    carrying event/payload/source hashes for re-verification.
    aggregate_inference_checked (external): exact group totals only — no
    event.payload, no operator names beyond the release's own member set and
    never any individual count.

    Output is built with an explicit key order and sorted lists so the digest
    is byte-deterministic across connections and crash recovery.
    """
    if events is None:events=_reading_rows(db)
    members=json.loads(row['members']) if isinstance(row['members'],str) else list(row['members'])
    mode=row.get('contract_mode')
    if mode is None:
        mode=db.execute('SELECT mode FROM release_contract WHERE id=?',(row['contract_id'],)).fetchone()['mode']
    if mode=='per_technician':
        if counts is None:
            counts={}
            for e in events:
                op=json.loads(e['payload']).get('operator');counts[op]=counts.get(op,0)+1
        evidence=[]
        for e in events:
            payload=json.loads(e['payload'])
            # Internal audit contract authorizes per-technician verification
            # against every original reading event, regardless of the
            # proposal's own member set.
            evidence.append({'event_id':e['id'],'work_order':e['work_order'],'device':e['device'],
                             'seq':e['seq'],'at':e['at'],'operator':payload.get('operator'),
                             'event_sha256':e['sha256'],'payload_sha256':e['sha256'],
                             'source_sha256':e['source_sha256']})
        evidence.sort(key=lambda x:x['event_id'])
        binding={'report_id':row['report_id'],'audience':row['audience'],'contract_id':row['contract_id'],
                 'members':sorted(members),'reading_count':row['reading_count'],
                 'event_version':row['event_version'],'source_digest':row['source_digest'],
                 'contract_digest':row['contract_digest']}
        payload={'report_id':binding['report_id'],'audience':binding['audience'],'contract_id':binding['contract_id'],
                 'members':binding['members'],'event_version':binding['event_version'],'source_digest':binding['source_digest'],
                 'view':'per_technician','binding':binding,'report_digest':row.get('report_digest',''),
                 'technician_counts':[{'operator':m,'reading_count':counts.get(m,0)} for m in sorted(counts)],
                 'reading_evidence':evidence}
        lines=['# 内部审计：逐技师采录证据','',f"- 报表: {row['report_id']} / 受众: {row['audience']}",
               f"- 事件版本: {row['event_version']}",f"- 来源摘要: {row['source_digest']}",'', '## 逐技师次数']
        lines += [f"- {m}: {counts.get(m,0)}" for m in sorted(counts)]
        lines += ['', '## 逐事件可复核证据']
        lines += [f"- event {e['event_id']} ({e['work_order']} {e['device']}:{e['seq']}) {e['operator']} event={e['event_sha256'][:12]} source={e['source_sha256'][:12]}" for e in evidence]
    else:
        binding={'report_id':row['report_id'],'audience':row['audience'],'contract_id':row['contract_id'],
                 'members':sorted(members),'reading_count':row['reading_count'],
                 'event_version':row['event_version'],'source_digest':row['source_digest'],
                 'contract_digest':row['contract_digest']}
        payload={'report_id':binding['report_id'],'audience':binding['audience'],'contract_id':binding['contract_id'],
                 'members':binding['members'],'event_version':binding['event_version'],'source_digest':binding['source_digest'],
                 'view':'aggregate_inference_checked','binding':binding,'report_digest':row.get('report_digest',''),
                 'reading_count':row['reading_count'],
                 'privacy_note':'group total only; individual technician counts are not derivable from reports held by this audience'}
        lines=['# 外部合作方：群组采录统计','',f"- 报表: {row['report_id']} / 受众: {row['audience']}",
               f"- 成员集合: {', '.join(sorted(members))}",f"- 合计读数: {row['reading_count']}",
               f"- 事件版本: {row['event_version']}",f"- 来源摘要: {row['source_digest']}",
               '- 不含任何 event.payload 或逐技师次数']
    md='\n'.join(lines)+'\n'
    return payload,md

def verify_release_file(path,expected_digest):
    """A release file is valid only if its binding digest matches the ledger."""
    doc=json.loads(Path(path).read_text(encoding='utf-8'))
    from .technician_privacy import digest as _digest
    return doc.get('report_digest')==expected_digest and _digest(doc['binding'])==expected_digest

def write_release(json_path,md_path,payload,md):
    """Atomic tmp+fsync+rename for both artifacts; parent dirs created first."""
    json_path=Path(json_path);md_path=Path(md_path)
    json_path.parent.mkdir(parents=True,exist_ok=True)
    for target,content in ((json_path,canon(payload)),(md_path,md)):
        fd,tmp=tempfile.mkstemp(prefix='.rel-',suffix=target.suffix,dir=str(target.parent))
        try:
            with os.fdopen(fd,'w',encoding='utf-8') as f:
                f.write(content);f.flush();os.fsync(f.fileno())
            os.replace(tmp,target)
        except Exception:
            try:os.unlink(tmp)
            except OSError:pass
            raise

def audience_reports(db_path,out_dir):
    """Materialize one directory per audience from the durable ledger."""
    db=connect(db_path);out_dir=Path(out_dir).resolve();written=[]
    try:
        events=_reading_rows(db);counts={}
        for e in events:
            op=json.loads(e['payload']).get('operator');counts[op]=counts.get(op,0)+1
        for r in db.execute("SELECT * FROM release_ledger WHERE status='published' AND json_path IS NOT NULL ORDER BY id"):
            row=dict(r);jp=Path(row['json_path']);mp=Path(row['md_path'])
            if jp.parent != out_dir/row['audience']:
                jp=out_dir/row['audience']/(row['report_id']+'.json');mp=jp.with_suffix('.md')
            payload,md=render_release(db,row,events,counts)
            from .technician_privacy import digest as _digest
            if _digest(payload['binding'])!=row['report_digest'] or payload['report_digest']!=row['report_digest']:
                raise RuntimeError('report_digest_mismatch:'+row['report_id'])
            write_release(jp,mp,payload,md)
            db.execute('INSERT INTO release_ledger_event(ledger_id,phase,detail) VALUES (?,?,?)',
                       (row['id'],'materialize','audience_report_written'))
            written.append(str(jp))
        db.commit()
    finally:db.close()
    return written
