"""Revisions, reversals, idempotency and cent-level conservation."""
import copy,json
from pathlib import Path
from maintlog.store import connect
from maintlog.warranty_billing import settle,settlement_report,ENTRY_REVERSAL
from warranty_helpers import FIX,base_case,seed,register_case,entry_sums,invoice_amount

def _rev_nos(db,oid,cid):
    c=connect(db)
    try:return [r[0] for r in c.execute('SELECT revision_no FROM settlement_revision '
                                        'WHERE work_order=? AND contract_id=? ORDER BY revision_no',(oid,cid))]
    finally:c.close()

def test_repeated_settlement_is_idempotent(tmp_path):
    db=seed(tmp_path);register_case(tmp_path,db,'C1',base_case())
    first=settle(db,'C1','parts_covered')
    again=settle(db,'C1','parts_covered')
    assert again['id']==first['id'] and again['snapshot_digest']==first['snapshot_digest']
    third=settle(db,'C1','parts_covered')
    assert third['id']==first['id']
    c=connect(db)
    try:
        assert c.execute('SELECT COUNT(*) FROM settlement_revision').fetchone()[0]==1
        # one insurer receivable per line (including zero-amount rows) plus one customer adjustment
        assert c.execute('SELECT COUNT(*) FROM settlement_entry').fetchone()[0]==3
        assert c.execute('SELECT COUNT(*) FROM settlement_line').fetchone()[0]==2
    finally:c.close()
    assert entry_sums(db,'WO-1','parts_covered')=={'customer':3000,'insurer':3000}

def test_price_change_new_revision_reverses_then_reapplies(tmp_path):
    db=seed(tmp_path);register_case(tmp_path,db,'C1',base_case(),'case1.json')
    r1=settle(db,'C1','parts_covered')
    assert r1['revision_no']==1 and r1['insurer_due_cents']==3000
    # part price rises 4000 -> 5000: new frozen case, same immutable invoice
    doc2=copy.deepcopy(base_case());doc2['lines'][1]['gross_cents']=5000
    register_case(tmp_path,db,'C2',doc2,'case2.json')
    r2=settle(db,'C2','parts_covered')
    assert r2['revision_no']==2
    assert r2['gross_total_cents']==15000 and r2['insurer_due_cents']==4000 and r2['customer_due_cents']==11000
    types=[e['entry_type'] for e in r2['entries']]
    assert types.count(ENTRY_REVERSAL)==3                 # reverse every prior entry, incl. zero-amount rows
    # reversals negate the previous adjustment cent for cent
    rev_sum=sum(e['amount_cents'] for e in r2['entries'] if e['entry_type']==ENTRY_REVERSAL)
    assert rev_sum==-(3000+3000)                          # prior insurer 3000 + customer 3000
    # nothing accumulates on top of the invoice: net equals current revision only
    assert entry_sums(db,'WO-1','parts_covered')=={'customer':3000,'insurer':4000}
    assert invoice_amount(db)==8000                        # INV-001 identity/amount intact
    assert _rev_nos(db,'WO-1','parts_covered')==[1,2]
    # old revision and every old entry are preserved, never overwritten
    c=connect(db)
    try:
        old=[dict(e) for e in c.execute('SELECT * FROM settlement_entry WHERE revision_id=? ORDER BY id',(r1['id'],))]
        assert len(old)==3 and all(e['entry_type']!='reversal' for e in old)
    finally:c.close()

def test_two_consecutive_revisions_keep_conservation(tmp_path):
    db=seed(tmp_path);register_case(tmp_path,db,'C1',base_case(),'case1.json')
    settle(db,'C1','parts_covered')
    doc2=copy.deepcopy(base_case());doc2['lines'][0]['gross_cents']=12000
    register_case(tmp_path,db,'C2',doc2,'case2.json')
    r2=settle(db,'C2','parts_covered')
    assert r2['customer_due_cents']==13000 and r2['insurer_due_cents']==3000
    doc3=copy.deepcopy(doc2);doc3['warranty_deductible_cents']=4000
    register_case(tmp_path,db,'C3',doc3,'case3.json')
    r3=settle(db,'C3','parts_covered')
    assert r3['revision_no']==3
    # deductible 4000 wipes the whole 4000 part gross -> insurer 0, customer pays all 16000
    assert r3['insurer_due_cents']==0 and r3['customer_due_cents']==16000
    assert r3['customer_due_cents']+r3['insurer_due_cents']==r3['gross_total_cents']
    nets=entry_sums(db,'WO-1','parts_covered')
    # reversals cancel revision 2; net = current due minus the 8000 already paid on INV-001
    assert nets=={'customer':16000-8000,'insurer':0}
    c=connect(db)
    try:
        # every effective revision balances: invoice + customer adjustment + insurer = gross
        for rev in c.execute('SELECT id,gross_total_cents,invoice_customer_due_cents FROM settlement_revision WHERE gross_total_cents IS NOT NULL'):
            rows=c.execute("SELECT party,SUM(amount_cents) t FROM settlement_entry WHERE revision_id=? AND entry_type!='reversal' GROUP BY party",(rev['id'],)).fetchall()
            m={r['party']:r['t'] for r in rows}
            assert rev['invoice_customer_due_cents']+m.get('customer',0)+m.get('insurer',0)==rev['gross_total_cents']
    finally:c.close()

def test_contract_switch_after_invoice_starts_new_stream_keeps_history(tmp_path):
    db=seed(tmp_path);register_case(tmp_path,db,'C1',base_case())
    settle(db,'C1','parts_covered')
    settle(db,'C1','labor_covered')
    doc2=copy.deepcopy(base_case())
    doc2['contracts']=[{'id':'full_cover','covered_kinds':['labor','part']}]
    register_case(tmp_path,db,'C2',doc2,'case2.json')
    rev=settle(db,'C2','full_cover')
    assert rev['insurer_due_cents']==13000 and rev['customer_due_cents']==1000
    c=connect(db)
    try:
        ids={r[0] for r in c.execute('SELECT contract_id FROM settlement_stream')}
        assert ids=={'parts_covered','labor_covered','full_cover'}
        # prior streams remain untouched and replayable
        assert c.execute('SELECT COUNT(*) FROM settlement_revision').fetchone()[0]==3
    finally:c.close()

def test_report_replays_every_cent(tmp_path):
    db=seed(tmp_path);register_case(tmp_path,db,'C1',base_case())
    settle(db,'C1','parts_covered');settle(db,'C1','labor_covered')
    doc2=copy.deepcopy(base_case());doc2['lines'][1]['gross_cents']=5000
    register_case(tmp_path,db,'C2',doc2,'case2.json');settle(db,'C2','parts_covered')
    out=settlement_report(db,tmp_path/'settlement.json')
    data=json.loads(out.read_text(encoding='utf-8'))
    assert len(data['streams'])==2
    for s in data['streams']:
        # net positions equal the sum of every voucher entry, including reversals
        ent_cust=sum(e['amount_cents'] for rev in s['revisions'] for e in rev['entries'] if e['party']=='customer')
        ent_ins=sum(e['amount_cents'] for rev in s['revisions'] for e in rev['entries'] if e['party']=='insurer')
        assert ent_cust==s['customer_net_cents'] and ent_ins==s['insurer_net_cents']
        latest=s['revisions'][-1]
        assert latest['customer_due_cents']==s['current_customer_due_cents']
        assert s['invoice']['id']=='INV-001' and s['invoice']['customer_due_cents']==8000
