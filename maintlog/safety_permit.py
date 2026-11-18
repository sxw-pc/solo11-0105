"""Freeze pending work orders and adjudicate isolation permits before terminal starts."""
import hashlib,json
from pathlib import Path
from .store import connect,when

CONTRACTS=('equivalent_cutset','all_local_points')

def register(db_path,case_id,fixture):
    path=Path(fixture).resolve();raw=path.read_bytes();doc=json.loads(raw)
    if not case_id or not doc.get('work_order') or not doc.get('energy_edges') or not doc.get('lock_events'):
        raise ValueError('bad_permit_case')
    db=connect(db_path)
    try:
        with db:
            order=db.execute('SELECT id,equipment,status FROM work_order WHERE id=?',(doc['work_order'],)).fetchone()
            if not order or order['equipment']!=doc['equipment'] or order['status']!='待接收':
                raise ValueError('permit_order_missing')
            digest=hashlib.sha256(raw).hexdigest()
            payload=json.dumps({'fixture':str(path),'case':doc,'work_order':dict(order)},sort_keys=True,separators=(',',':'))
            old=db.execute('SELECT digest FROM permit_case WHERE id=?',(case_id,)).fetchone()
            if old and old['digest']!=digest:raise ValueError('permit_case_conflict')
            db.execute('INSERT OR IGNORE INTO permit_case VALUES (?,?,?)',(case_id,payload,digest))
    finally:db.close()
    return {'case_id':case_id,'digest':digest,'locks':len(doc['lock_events'])}

def _digest(obj):
    return hashlib.sha256(json.dumps(obj,sort_keys=True,separators=(',',':')).encode()).hexdigest()

def topology_digest(case):
    return _digest({'equipment':case.get('equipment'),
        'energy_sources':sorted(case.get('energy_sources') or []),
        'energy_edges':sorted((e.get('from'),e.get('to')) for e in case.get('energy_edges') or []),
        'local_points':sorted(case.get('local_points') or [])})

def lock_digest(effective):
    return _digest([{'point':pt,'id':lk.get('id'),'seal':lk.get('seal'),'staff':lk.get('staff'),
        'verified_by':lk.get('verified_by'),'at':lk.get('at')} for pt,lk in sorted(effective.items())])

def _find_cycle(adj,nodes):
    color={n:0 for n in nodes};stack=[]
    def dfs(n):
        color[n]=1;stack.append(n)
        for m in adj.get(n,()):
            if color.get(m,0)==1:return stack[stack.index(m):]+[m]
            if color.get(m,0)==0:
                r=dfs(m)
                if r:return r
        stack.pop();color[n]=2;return None
    for n in sorted(nodes):
        if color[n]==0:
            r=dfs(n)
            if r:return r
    return None

def _enum_paths(adj,sources,equipment):
    out=[]
    for s in sorted(sources):
        stack=[(s,[s])]
        while stack:
            n,p=stack.pop()
            if n==equipment:out.append(p);continue
            for m in adj.get(n,()):
                if m not in p:stack.append((m,p+[m]))
    return out

def _topology(case):
    problems=[]
    sources=list(case.get('energy_sources') or [])
    edges=case.get('energy_edges') or []
    equip=case.get('equipment')
    local=list(case.get('local_points') or [])
    adj={};nodes=set(sources)|{equip}|set(local)
    for e in edges:
        f,t=e.get('from'),e.get('to')
        if not f or not t:problems.append({'code':'missing_edge','edge':{k:e.get(k) for k in ('from','to')}});continue
        adj.setdefault(f,[]).append(t);nodes.add(f);nodes.add(t)
    if not edges:problems.append({'code':'missing_edge','detail':'no energy edges declared'})
    indeg={n:0 for n in nodes}
    for f,ts in adj.items():
        for t in ts:indeg[t]=indeg.get(t,0)+1
    for n in sorted(nodes):
        if n not in sources and n!=equip and indeg.get(n,0)==0:
            problems.append({'code':'unknown_feed_point','node':n})
    edge_nodes={x for e in edges for x in (e.get('from'),e.get('to'))}
    for lp in local:
        if lp not in edge_nodes and lp not in sources and lp!=equip:
            problems.append({'code':'unknown_local_point','point':lp})
    cyc=_find_cycle(adj,nodes)
    if cyc:problems.append({'code':'cycle','nodes':cyc})
    paths=[] if cyc else _enum_paths(adj,sources,equip)
    if not cyc and not paths:
        problems.append({'code':'no_source_path','detail':'no energy path from declared sources to equipment; cannot prove isolation'})
    return problems,paths

def _signatures(case,valid_from,valid_until):
    problems=[];roles={}
    for s in case.get('signatures') or []:
        r=s.get('role')
        if r in roles:problems.append({'code':'duplicate_signature','role':r})
        roles[r]=s
    op,sf=roles.get('operator'),roles.get('safety')
    if not op:problems.append({'code':'missing_signature','role':'operator'})
    if not sf:problems.append({'code':'missing_signature','role':'safety'})
    if op and sf and op.get('person')==sf.get('person'):
        problems.append({'code':'signature_not_independent','person':op.get('person')})
    for s in (op,sf):
        if not s:continue
        try:at=when(s['at'])
        except (KeyError,TypeError,ValueError):
            problems.append({'code':'signature_clock_invalid','role':s.get('role')});continue
        if valid_from and at<valid_from or valid_until and at>valid_until:
            problems.append({'code':'signature_expired','role':s.get('role'),'at':s.get('at')})
    return op,sf,problems

def _replay_locks(events,t,op,sf):
    """Replay the ordered lock/unlock record up to time t. Any inconsistency blocks."""
    problems=[];state={};ever_unlocked=set();prev=None
    for i,ev in enumerate(events):
        try:
            action=ev['action'];point=str(ev['point']);stamp=when(ev['at'])
        except (KeyError,TypeError,ValueError) as e:
            problems.append({'code':'lock_record_invalid','index':i,'detail':str(e)});continue
        if prev is not None and stamp<prev:
            problems.append({'code':'clock_conflict','detail':f"lock event {ev.get('id')} out of order"})
        prev=stamp
        if stamp>t:continue
        if action=='lock':
            if state.get(point):
                problems.append({'code':'clock_conflict','detail':f'{point} locked again before unlock'})
            vb=ev.get('verified_by');relock=point in ever_unlocked
            if not vb:
                problems.append({'code':'unverified_relock' if relock else 'lock_unverified','point':point,'lock':ev.get('id')})
            elif vb==ev.get('staff'):
                problems.append({'code':'lock_verification_not_independent','point':point,'lock':ev.get('id')})
            if not ev.get('seal'):
                problems.append({'code':'missing_seal','point':point,'lock':ev.get('id')})
            if op and ev.get('staff')!=op.get('person'):
                problems.append({'code':'lock_staff_mismatch','point':point,'staff':ev.get('staff')})
            if sf and vb and vb!=sf.get('person'):
                problems.append({'code':'lock_verifier_mismatch','point':point,'verified_by':vb})
            state[point]=ev
        elif action=='unlock':
            cur=state.get(point)
            if not cur:
                problems.append({'code':'clock_conflict','detail':f'unlock without lock at {point}'})
            else:
                if ev.get('seal')!=cur.get('seal'):
                    problems.append({'code':'seal_mismatch','point':point,'expected':cur.get('seal'),'got':ev.get('seal')})
                state[point]=None;ever_unlocked.add(point)
        else:
            problems.append({'code':'unknown_lock_action','point':point,'action':action})
    return {p:e for p,e in state.items() if e},problems

def adjudicate(case,contract,at):
    """Replay locks at candidate start time and decide the contract invariant.
    Returns (ok, evidence) with concrete paths / unlocked points, never a bare boolean."""
    problems=[]
    contracts={c.get('id'):c for c in case.get('contracts') or [] if isinstance(c,dict)}
    c=contracts.get(contract)
    if c is None:raise ValueError('permit_contract_unknown')
    if c.get('authorized_by')!='SITE-SAFETY':raise ValueError('permit_contract_unauthorized')
    try:
        valid_from=when(c['valid_from']);valid_until=when(c['valid_until'])
        if valid_from>=valid_until:problems.append({'code':'clock_conflict','detail':'contract validity window empty'})
    except (KeyError,TypeError,ValueError) as e:
        problems.append({'code':'clock_conflict','detail':f'contract window invalid: {e}'});valid_from=valid_until=None
    deadline=None
    if case.get('start_deadline'):
        try:deadline=when(case['start_deadline'])
        except (TypeError,ValueError) as e:problems.append({'code':'clock_conflict','detail':f'start_deadline invalid: {e}'})
    t=when(at)
    op,sf,sig_problems=_signatures(case,valid_from,valid_until)
    problems+=sig_problems
    effective,lock_problems=_replay_locks(case.get('lock_events') or [],t,op,sf)
    problems+=lock_problems
    if op and sf:
        for pt,lk in sorted(effective.items()):
            for s in (op,sf):
                try:
                    if when(s['at'])<when(lk['at']):
                        problems.append({'code':'signature_predates_lock','role':s.get('role'),'point':pt,'lock':lk.get('id')})
                except (KeyError,TypeError,ValueError):pass
    topo_problems,paths=_topology(case)
    problems+=topo_problems
    locked=set(effective)
    path_ev=[]
    for p in paths:
        cut=next((n for n in p if n in locked),None)
        path_ev.append({'source':p[0],'nodes':p,'cut_by':{'point':cut,'lock':effective[cut].get('id')} if cut else None})
        if contract=='equivalent_cutset' and not cut:
            problems.append({'code':'uncut_path','path':p})
    local_ev=[]
    if contract=='all_local_points':
        for lp in case.get('local_points') or []:
            lk=effective.get(lp)
            local_ev.append({'point':lp,'locked':bool(lk),'lock':lk.get('id') if lk else None})
            if not lk:problems.append({'code':'unlocked_local_point','point':lp})
    if valid_from and t<valid_from:
        problems.append({'code':'outside_window','detail':'before contract validity','valid_from':c.get('valid_from')})
    if valid_until and t>valid_until:
        problems.append({'code':'outside_window','detail':'after contract validity','valid_until':c.get('valid_until')})
    if deadline and t>deadline:
        problems.append({'code':'start_deadline_exceeded','deadline':case.get('start_deadline')})
    end=valid_until
    if deadline and (end is None or deadline<end):end=deadline
    evidence={'contract':contract,'at':at,'work_order':case.get('work_order'),'equipment':case.get('equipment'),
        'window':{'valid_from':valid_from.isoformat() if valid_from else None,
                  'valid_until':end.isoformat() if end else None,
                  'start_deadline':deadline.isoformat() if deadline else None},
        'signatures':{'operator':op.get('person') if op else None,'safety':sf.get('person') if sf else None},
        'locks':[{'point':pt,'id':lk.get('id'),'seal':lk.get('seal'),'staff':lk.get('staff'),
                  'verified_by':lk.get('verified_by'),'at':lk.get('at')} for pt,lk in sorted(effective.items())],
        'paths':path_ev,'local_points':local_ev,
        'topology_digest':topology_digest(case),'lock_digest':lock_digest(effective),
        'problems':problems}
    return (not problems),evidence

def grant(db_path,case_id,contract,at):
    """Adjudicate case under contract at candidate start time; on success issue a
    permit bound to work order, case digest, lock state, topology, window and a one-time fence."""
    db=connect(db_path)
    try:
        with db:
            row=db.execute('SELECT id,payload,digest FROM permit_case WHERE id=?',(case_id,)).fetchone()
            if not row:raise ValueError('permit_case_missing')
            case=json.loads(row['payload'])['case']
            order=db.execute('SELECT status FROM work_order WHERE id=?',(case.get('work_order'),)).fetchone()
            if not order or order['status']!='待接收':raise ValueError('permit_order_state')
            t=when(at)
            ok,evidence=adjudicate(case,contract,at)
            grant_id=fence=None
            if ok:
                n=db.execute('SELECT COUNT(*) c FROM permit_grant WHERE case_id=?',(case_id,)).fetchone()['c']
                grant_id=f'PG-{case_id}-{contract}-{n+1}'
                fence=_digest(f"{grant_id}|{row['digest']}|{evidence['lock_digest']}")[:24]
                db.execute('INSERT INTO permit_grant(id,case_id,case_digest,work_order,contract,fence,topology_digest,lock_digest,valid_from,valid_until,evidence,status,granted_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
                    (grant_id,case_id,row['digest'],case['work_order'],contract,fence,evidence['topology_digest'],
                     evidence['lock_digest'],evidence['window']['valid_from'],evidence['window']['valid_until'],
                     json.dumps(evidence,ensure_ascii=False,sort_keys=True),'active',t.isoformat()))
            db.execute('INSERT INTO permit_adjudication(case_id,contract,at,outcome,evidence,grant_id) VALUES (?,?,?,?,?,?)',
                (case_id,contract,at,'granted' if ok else 'denied',json.dumps(evidence,ensure_ascii=False,sort_keys=True),grant_id))
            return {'granted':ok,'grant_id':grant_id,'fence':fence,'evidence':evidence}
    finally:db.close()

def revoke(db_path,grant_id):
    db=connect(db_path)
    try:
        with db:
            cur=db.execute("UPDATE permit_grant SET status='revoked' WHERE id=? AND status='active'",(grant_id,))
            if cur.rowcount!=1:raise ValueError('permit_not_active')
        return {'revoked':grant_id}
    finally:db.close()

def _cases_for_order(db,work_order):
    out=[]
    for r in db.execute('SELECT id,payload,digest FROM permit_case ORDER BY rowid'):
        p=json.loads(r['payload'])
        if p['case'].get('work_order')==work_order:out.append((r['id'],p['case'],r['digest']))
    return out

def _check_grant(g,case_id,case,case_digest,at):
    ev={'grant_id':g['id'],'contract':g['contract']}
    if g['case_id']!=case_id or g['case_digest']!=case_digest:
        return False,'stale_case',{**ev,'detail':'permit predates current permit case','current_case':case_id}
    if topology_digest(case)!=g['topology_digest']:
        return False,'topology_changed',{**ev,'detail':'energy topology changed since grant'}
    t=when(at)
    if t<when(g['granted_at']):
        return False,'clock_conflict',{**ev,'detail':'start precedes permit grant','granted_at':g['granted_at']}
    if t<when(g['valid_from']) or t>when(g['valid_until']):
        return False,'permit_expired',{**ev,'detail':'start outside permit validity','valid_from':g['valid_from'],'valid_until':g['valid_until']}
    ok,adj=adjudicate(case,g['contract'],at)
    if adj['lock_digest']!=g['lock_digest']:
        return False,'lock_state_changed',{**ev,'detail':'lock state changed since grant',
            'grant_locks':json.loads(g['evidence'])['locks'],'current_locks':adj['locks']}
    if not ok:
        return False,'invariant_failed',{**ev,'problems':adj['problems']}
    return True,'ok',{**ev,'fence':g['fence'],'paths':adj['paths'],'locks':adj['locks'],'local_points':adj['local_points']}

def gate_start(db,event_id,work_order,at):
    """Verify permit and current lock state for a terminal start, inside the ingest
    transaction. A permitted start consumes its one-time fence; anything else is
    recorded as a blocked attempt and must not activate the work order."""
    cases=_cases_for_order(db,work_order)
    if not cases:return None
    case_id,case,case_digest=cases[-1]
    grants=db.execute("SELECT * FROM permit_grant WHERE work_order=? AND status='active' ORDER BY granted_at,id",(work_order,)).fetchall()
    failures=[]
    for g in grants:
        ok,reason,ev=_check_grant(g,case_id,case,case_digest,at)
        if ok:
            cur=db.execute("UPDATE permit_grant SET status='consumed',consumed_event=? WHERE id=? AND status='active'",(event_id,g['id']))
            if cur.rowcount==1:
                db.execute('INSERT INTO start_attempt(event_id,work_order,grant_id,status,reason,evidence) VALUES (?,?,?,?,?,?)',
                    (event_id,work_order,g['id'],'permitted','ok',json.dumps(ev,ensure_ascii=False,sort_keys=True)))
                return 'permitted'
        failures.append({'grant_id':g['id'],'reason':reason,'evidence':ev})
    if not grants:
        reason,evidence='no_active_permit',{'detail':'no active permit grant for work order','work_order':work_order,'case':case_id}
    else:
        reason,evidence=failures[0]['reason'],{'failures':failures}
    db.execute('INSERT INTO start_attempt(event_id,work_order,grant_id,status,reason,evidence) VALUES (?,?,?,?,?,?)',
        (event_id,work_order,None,'blocked',reason,json.dumps(evidence,ensure_ascii=False,sort_keys=True)))
    return 'blocked'

def permits(db_path):
    db=connect(db_path)
    try:
        return {'grants':[dict(r) for r in db.execute('SELECT * FROM permit_grant ORDER BY granted_at,id')],
                'attempts':[dict(r) for r in db.execute('SELECT * FROM start_attempt ORDER BY id')],
                'adjudications':[dict(r) for r in db.execute('SELECT * FROM permit_adjudication ORDER BY id')]}
    finally:db.close()
