"""Freeze completed service evidence and a published invoice, then settle warranty.

``register`` only freezes a completed work order's technical decision, replacement
events, source digests, the immutable issued invoice and the pre-authorized warranty
contracts. It computes no receivables and no refunds.

``settle`` turns a frozen case into an auditable settlement per contract:

* only technically completed orders whose replacement parts match the stocked
  issuance and the original event identities, and whose prices/contracts/invoice are
  valid, can be finally billed — otherwise the whole settlement is held 待核;
* labor and part lines are summed in whole cents; the warranty deductible is applied
  once per work order to the covered lines, never once per line;
* customer due plus insurer due equals the accepted gross total cent for cent, with
  per-line attribution preserved by an integer-only largest-remainder rule;
* INV-001 is never rewritten; differences are expressed as customer
  supplementary-charge/refund entries and insurer receivables;
* every price/contract/evidence change opens a new revision that first reverses the
  previous effective adjustment, so amounts never accumulate on top of the invoice;
  replaying an identical contract/evidence snapshot is idempotent.
"""
import hashlib,json
from datetime import datetime,timezone
from pathlib import Path
from .store import connect

ENTRY_CUSTOMER='customer_adjustment'
ENTRY_INSURER='insurer_receivable'
ENTRY_REVERSAL='reversal'
STATUS_HELD='待核'
STATUS_BILLED='已出账'

def register(db_path,case_id,fixture):
    path=Path(fixture).resolve();raw=path.read_bytes();doc=json.loads(raw)
    if not case_id or not doc.get('work_order') or not doc.get('invoice') or not doc.get('contracts'):
        raise ValueError('bad_warranty_case')
    db=connect(db_path)
    try:
        with db:
            decision=db.execute('SELECT status,detail,rule_version FROM decision WHERE work_order=?',(doc['work_order'],)).fetchone()
            if not decision or decision['status']!='完成':raise ValueError('billing_order_not_complete')
            evidence=[dict(e) for e in db.execute('SELECT e.id,e.kind,e.at,e.sha256,s.sha256 AS source_sha256 FROM event e JOIN source s ON s.id=e.source_id WHERE e.work_order=? ORDER BY e.id',(doc['work_order'],))]
            if not any(e['kind']=='replace' for e in evidence):raise ValueError('billing_part_missing')
            digest=hashlib.sha256(raw).hexdigest()
            payload=json.dumps({'fixture':str(path),'doc':doc,'decision':dict(decision),'evidence':evidence},sort_keys=True,separators=(',',':'))
            inv=doc['invoice'];old=db.execute('SELECT work_order,customer_due_cents FROM issued_invoice WHERE id=?',(inv['id'],)).fetchone()
            if old:
                if old['work_order']!=doc['work_order'] or old['customer_due_cents']!=inv['customer_due_cents']:raise ValueError('invoice_conflict')
            else:
                db.execute('INSERT INTO issued_invoice VALUES (?,?,?)',(inv['id'],doc['work_order'],inv['customer_due_cents']))
            old=db.execute('SELECT digest FROM warranty_case WHERE id=?',(case_id,)).fetchone()
            if old and old['digest']!=digest:raise ValueError('warranty_case_conflict')
            db.execute('INSERT OR IGNORE INTO warranty_case VALUES (?,?,?)',(case_id,payload,digest))
    finally:db.close()
    return {'case_id':case_id,'digest':digest,'invoice':doc['invoice']['id']}

def _now():
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S+00:00')

def _digest(obj):
    return hashlib.sha256(json.dumps(obj,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode('utf-8')).hexdigest()

def _load_case(db,case_id):
    row=db.execute('SELECT payload,digest FROM warranty_case WHERE id=?',(case_id,)).fetchone()
    if not row:raise ValueError('unknown_case')
    frozen=json.loads(row['payload'])
    # canonical digest of the frozen content: independent of fixture byte formatting,
    # so any price/contract/evidence change necessarily changes the snapshot digest
    case_digest=_digest(frozen)
    return frozen,case_digest,frozen['doc']

def _whole_cents(value):
    # bool is an int subclass but never a valid price
    if isinstance(value,bool) or not isinstance(value,int) or value<0:return None
    return value

def _eligibility(db,doc,case_digest,frozen):
    """Re-verify the frozen case against live technical state. Returns reason codes."""
    reasons=[];oid=doc['work_order']
    decision=db.execute('SELECT status,detail,rule_version FROM decision WHERE work_order=?',(oid,)).fetchone()
    if not decision or decision['status']!='完成':
        reasons.append('order_not_complete')
    # source divergence: the same terminal/sequence observed with different payloads
    if db.execute("SELECT 1 FROM issue WHERE work_order=? AND code='identity_conflict' LIMIT 1",(oid,)).fetchone():
        reasons.append('source_conflict')
    live_evidence=[dict(e) for e in db.execute(
        'SELECT e.id,e.kind,e.at,e.sha256,s.sha256 AS source_sha256 FROM event e '
        'JOIN source s ON s.id=e.source_id WHERE e.work_order=? ORDER BY e.id',(oid,))]
    if _digest(live_evidence)!=_digest(frozen.get('evidence',[])):
        reasons.append('evidence_drift')
    lines=doc.get('lines')
    if not isinstance(lines,list) or not lines:
        reasons.append('lines_invalid')
    else:
        seen=set()
        for i,line in enumerate(lines):
            kind=line.get('kind');item=line.get('item');gross=_whole_cents(line.get('gross_cents'))
            if kind not in ('labor','part') or not item or gross is None:
                reasons.append('lines_invalid');break
            key=(kind,item)
            if key in seen:reasons.append('lines_invalid');break
            seen.add(key)
    deductible=_whole_cents(doc.get('warranty_deductible_cents'))
    if deductible is None:reasons.append('deductible_invalid')
    inv=doc.get('invoice')
    frozen_inv=frozen.get('doc',{}).get('invoice',{})
    if not isinstance(inv,dict) or not inv.get('id') or _whole_cents(inv.get('customer_due_cents')) is None:
        reasons.append('invoice_invalid')
    else:
        issued=db.execute('SELECT work_order,customer_due_cents FROM issued_invoice WHERE id=?',(inv['id'],)).fetchone()
        # the issued invoice is immutable: the frozen case and the live row must agree with it
        if (not issued or issued['work_order']!=oid or issued['customer_due_cents']!=inv['customer_due_cents']
                or frozen_inv.get('customer_due_cents')!=inv['customer_due_cents']
                or frozen_inv.get('id')!=inv['id']):
            reasons.append('invoice_conflict')
    contracts=doc.get('contracts')
    if not isinstance(contracts,list) or not contracts:
        reasons.append('contracts_invalid')
    else:
        ids=set()
        for c in contracts:
            covered=c.get('covered_kinds') if isinstance(c,dict) else None
            if not c.get('id') or not isinstance(covered,list) or not covered or any(k not in ('labor','part') for k in covered):
                reasons.append('contracts_invalid');break
            if c['id'] in ids:reasons.append('contracts_invalid');break
            ids.add(c['id'])
    # part identity: every billed part line must be one accepted replacement whose
    # new part was issued to this exact work order, matching the original event.
    if isinstance(lines,list) and not any(r=='lines_invalid' for r in reasons):
        replacements={}
        for e in live_evidence:
            if e['kind']!='replace':continue
            payload=db.execute('SELECT payload FROM event WHERE id=?',(e['id'],)).fetchone()
            data=json.loads(payload['payload']) if payload else {}
            stock=db.execute('SELECT issued_to FROM stock WHERE part=?',(data.get('new'),)).fetchone()
            if stock and stock['issued_to']==oid and data.get('old') and data.get('new') and data['old']!=data['new']:
                replacements[data['new']]=e['id']
        for line in lines:
            if line['kind']=='part' and line['item'] not in replacements:
                reasons.append('part_identity_conflict');break
    snapshot_digest=_digest({
        'case_digest':case_digest,'decision':dict(decision) if decision else None,
        'evidence':live_evidence,'lines':lines,'invoice':inv,
        'deductible':deductible,'contracts':contracts,
    })
    return sorted(set(reasons)),snapshot_digest

def largest_remainder(weights,total):
    """Split ``total`` whole cents by integer weights using the stable rule.

    Largest remainders win; ties resolve by earlier index so attribution is
    deterministic. Pure integer arithmetic — no floating point anywhere.
    """
    if not isinstance(total,int) or isinstance(total,bool) or total<0:raise ValueError('bad_total')
    if any(not isinstance(w,int) or isinstance(w,bool) or w<0 for w in weights):raise ValueError('bad_weight')
    n=len(weights);weight_sum=sum(weights)
    if n==0:return []
    if total==0:return [0]*n
    if weight_sum==0:
        out=[0]*n
        for i in range(total):out[i%n]+=1
        return out
    floors=[];remainders=[];allocated=0
    for w in weights:
        q,r=divmod(total*w,weight_sum)
        floors.append(q);remainders.append(r);allocated+=q
    order=sorted(range(n),key=lambda i:(-remainders[i],i))
    k=0
    while allocated<total:
        floors[order[k%n]]+=1;allocated+=1;k+=1
    return floors

def _apportion(lines,covered_kinds,deductible):
    """Customer/insurer split per line; deductible charged once on covered lines."""
    gross_total=sum(l['gross_cents'] for l in lines)
    covered_idx=[i for i,l in enumerate(lines) if l['kind'] in covered_kinds]
    covered_gross=sum(lines[i]['gross_cents'] for i in covered_idx)
    insurer_cap=max(0,covered_gross-deductible)
    # insurer share of each covered gross line, largest remainder, index-stable
    insurer_total_line=[0]*len(lines)
    if covered_idx:
        shares=largest_remainder([lines[i]['gross_cents'] for i in covered_idx],insurer_cap)
        for i,share in zip(covered_idx,shares):insurer_total_line[i]=share
    split=[]
    for i,l in enumerate(lines):
        insurer=insurer_total_line[i]
        customer=l['gross_cents']-insurer
        split.append({'line_no':i,'kind':l['kind'],'item':l['item'],'gross_cents':l['gross_cents'],
                      'covered':1 if i in covered_idx else 0,
                      'customer_cents':customer,'insurer_cents':insurer})
    insurer_due=sum(r['insurer_cents'] for r in split)
    customer_due=sum(r['customer_cents'] for r in split)
    assert customer_due+insurer_due==gross_total
    return split,gross_total,customer_due,insurer_due

def _stream_row(db,oid,contract_id):
    return db.execute('SELECT * FROM settlement_stream WHERE work_order=? AND contract_id=?',(oid,contract_id)).fetchone()

def _previous_entries(db,oid,contract_id,revision_id):
    # only the prior revision's own effective entries are reversed; its reversal
    # entries already cancelled the history before it and must never be reversed again
    return [dict(r) for r in db.execute(
        'SELECT entry_type,party,amount_cents,line_no,detail FROM settlement_entry '
        'WHERE work_order=? AND contract_id=? AND revision_id=? AND entry_type!=? ORDER BY id',
        (oid,contract_id,revision_id,ENTRY_REVERSAL))]

def settle(db_path,case_id,contract_id):
    """Settle one frozen warranty case under one pre-authorized contract.

    Returns the revision record. An identical snapshot replayed on the same stream
    is returned unchanged; a changed snapshot opens a new revision whose first
    entries reverse the previous effective adjustment.
    """
    db=connect(db_path)
    try:
        frozen,case_digest,doc=_load_case(db,case_id)
        oid=doc['work_order']
        if not any(isinstance(c,dict) and c.get('id')==contract_id for c in doc.get('contracts',[])):
            raise ValueError('unknown_contract')
        with db:
            reasons,snapshot_digest=_eligibility(db,doc,case_digest,frozen)
            stream=_stream_row(db,oid,contract_id)
            now=_now()
            if stream and stream['snapshot_digest']==snapshot_digest:
                # idempotent replay: same evidence/contract/price snapshot
                rev=db.execute('SELECT * FROM settlement_revision WHERE id=?',(stream['latest_revision'],)).fetchone()
                return _revision_payload(db,rev)
            prev_revision_id=stream['latest_revision'] if stream else None
            revision_no=db.execute('SELECT COALESCE(MAX(revision_no),0)+1 FROM settlement_revision WHERE work_order=? AND contract_id=?',(oid,contract_id)).fetchone()[0]
            inv=doc.get('invoice') if isinstance(doc.get('invoice'),dict) else {}
            cur=db.execute(
                'INSERT INTO settlement_revision(work_order,contract_id,revision_no,case_id,status,invoice_id,'
                'invoice_customer_due_cents,deductible_cents,gross_total_cents,customer_due_cents,insurer_due_cents,'
                'snapshot_digest,reasons_json,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (oid,contract_id,revision_no,case_id,STATUS_HELD if reasons else STATUS_BILLED,
                 inv.get('id'),inv.get('customer_due_cents'),
                 doc.get('warranty_deductible_cents'),None,None,None,snapshot_digest,
                 json.dumps(reasons,ensure_ascii=False),now))
            rev_id=cur.lastrowid
            if reasons:
                if stream:
                    db.execute('UPDATE settlement_stream SET status=?,latest_revision=?,snapshot_digest=?,case_id=? '
                               'WHERE work_order=? AND contract_id=?',
                               (STATUS_HELD,rev_id,snapshot_digest,case_id,oid,contract_id))
                else:
                    db.execute('INSERT INTO settlement_stream VALUES (?,?,?,?,?,?)',
                               (oid,contract_id,case_id,STATUS_HELD,rev_id,snapshot_digest))
                return _revision_payload(db,db.execute('SELECT * FROM settlement_revision WHERE id=?',(rev_id,)).fetchone())
            contract=next(c for c in doc['contracts'] if c['id']==contract_id)
            split,gross_total,customer_due,insurer_due=_apportion(doc['lines'],set(contract['covered_kinds']),
                                                                  doc['warranty_deductible_cents'])
            # 1) reverse every entry of the previous effective revision, cent for cent
            if prev_revision_id is not None:
                for e in _previous_entries(db,oid,contract_id,prev_revision_id):
                    db.execute('INSERT INTO settlement_entry(work_order,contract_id,revision_id,entry_type,party,'
                               'amount_cents,line_no,detail,created_at) VALUES (?,?,?,?,?,?,?,?,?)',
                               (oid,contract_id,rev_id,ENTRY_REVERSAL,e['party'],-e['amount_cents'],e['line_no'],
                                f"冲回 revision {prev_revision_id}: {e['detail']}",now))
            # 2) new effective adjustment against the immutable invoice.
            #    Zero-amount rows are kept: they are the auditable target that the
            #    next revision's reversals can negate, so balances never drift.
            for r in split:
                db.execute('INSERT INTO settlement_entry(work_order,contract_id,revision_id,entry_type,party,'
                           'amount_cents,line_no,detail,created_at) VALUES (?,?,?,?,?,?,?,?,?)',
                           (oid,contract_id,rev_id,ENTRY_INSURER,'insurer',r['insurer_cents'],r['line_no'],
                            f"承保方应收 {contract_id} 行{r['line_no']+1} {r['kind']}/{r['item']}",now))
            customer_adjustment=customer_due-inv['customer_due_cents']
            db.execute('INSERT INTO settlement_entry(work_order,contract_id,revision_id,entry_type,party,'
                       'amount_cents,line_no,detail,created_at) VALUES (?,?,?,?,?,?,?,?,?)',
                       (oid,contract_id,rev_id,ENTRY_CUSTOMER,'customer',customer_adjustment,None,
                        ('客户补收' if customer_adjustment>=0 else '客户退款')
                        +f" 对发票 {inv['id']} 的净补差",now))
            db.execute('UPDATE settlement_revision SET gross_total_cents=?,customer_due_cents=?,insurer_due_cents=? '
                       'WHERE id=?',(gross_total,customer_due,insurer_due,rev_id))
            for r in split:
                db.execute('INSERT INTO settlement_line VALUES (?,?,?,?,?,?,?,?)',
                           (rev_id,r['line_no'],r['kind'],r['item'],r['gross_cents'],r['covered'],
                            r['customer_cents'],r['insurer_cents']))
            if stream:
                db.execute('UPDATE settlement_stream SET status=?,latest_revision=?,snapshot_digest=?,case_id=? '
                           'WHERE work_order=? AND contract_id=?',
                           (STATUS_BILLED,rev_id,snapshot_digest,case_id,oid,contract_id))
            else:
                db.execute('INSERT INTO settlement_stream VALUES (?,?,?,?,?,?)',
                           (oid,contract_id,case_id,STATUS_BILLED,rev_id,snapshot_digest))
            return _revision_payload(db,db.execute('SELECT * FROM settlement_revision WHERE id=?',(rev_id,)).fetchone())
    finally:db.close()

def _revision_payload(db,rev):
    out=dict(rev)
    out['lines']=[dict(r) for r in db.execute('SELECT * FROM settlement_line WHERE revision_id=? ORDER BY line_no',(rev['id'],))]
    out['entries']=[dict(r) for r in db.execute('SELECT * FROM settlement_entry WHERE revision_id=? ORDER BY id',(rev['id'],))]
    out['reasons']=json.loads(rev['reasons_json'])
    return out

def _net_balances(db,oid,contract_id):
    rows=db.execute('SELECT party,SUM(amount_cents) AS total FROM settlement_entry '
                    'WHERE work_order=? AND contract_id=? GROUP BY party',(oid,contract_id)).fetchall()
    nets={r['party']:r['total'] for r in rows}
    return nets.get('customer',0),nets.get('insurer',0)

def settlement_report(db_path,out,case_id=None,work_order=None,contract_id=None):
    """Write a JSON report of every settlement stream, revision, line and entry."""
    db=connect(db_path)
    try:
        q='SELECT * FROM settlement_stream WHERE 1=1';params=[]
        if work_order:q+=' AND work_order=?';params.append(work_order)
        if contract_id:q+=' AND contract_id=?';params.append(contract_id)
        if case_id:q+=' AND case_id=?';params.append(case_id)
        streams=[]
        for s in db.execute(q+' ORDER BY work_order,contract_id',params):
            revs=[_revision_payload(db,r) for r in db.execute(
                'SELECT * FROM settlement_revision WHERE work_order=? AND contract_id=? ORDER BY revision_no',
                (s['work_order'],s['contract_id']))]
            customer_net,insurer_net=_net_balances(db,s['work_order'],s['contract_id'])
            invoice=db.execute('SELECT id,work_order,customer_due_cents FROM issued_invoice WHERE work_order=?',
                               (s['work_order'],)).fetchone()
            latest=revs[-1] if revs else None
            streams.append({
                'work_order':s['work_order'],'contract_id':s['contract_id'],'case_id':s['case_id'],
                'status':s['status'],'latest_revision':s['latest_revision'],'snapshot_digest':s['snapshot_digest'],
                'invoice':dict(invoice) if invoice else None,
                'customer_net_cents':customer_net,'insurer_net_cents':insurer_net,
                'current_customer_due_cents':latest['customer_due_cents'] if latest else None,
                'current_insurer_due_cents':latest['insurer_due_cents'] if latest else None,
                'revisions':revs,
            })
        data={'generated_at':_now(),'streams':streams}
        p=Path(out);p.parent.mkdir(parents=True,exist_ok=True)
        p.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
    finally:db.close()
    return p
