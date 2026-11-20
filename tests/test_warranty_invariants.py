"""Cross-cutting invariants: cent conservation, immutability and replayability."""
import copy,json
from pathlib import Path
from maintlog.store import connect
from maintlog.warranty_billing import settle,settlement_report
from warranty_helpers import base_case,seed,register_case

def test_every_billed_revision_conserves_cents_and_keeps_line_attribution(tmp_path):
    db=seed(tmp_path);register_case(tmp_path,db,'C1',base_case())
    for cid in ('parts_covered','labor_covered'):
        rev=settle(db,'C1',cid)
        # per-line attribution sums back to the revision totals
        assert sum(l['customer_cents']+l['insurer_cents'] for l in rev['lines'])==rev['gross_total_cents']
        assert sum(l['customer_cents'] for l in rev['lines'])==rev['customer_due_cents']
        assert sum(l['insurer_cents'] for l in rev['lines'])==rev['insurer_due_cents']
        # whole-order identity: customer + insurer = gross, cent for cent
        assert rev['customer_due_cents']+rev['insurer_due_cents']==rev['gross_total_cents']
        # every amount is a whole non-negative line figure; only the customer adjustment may be negative
        assert all(isinstance(l['gross_cents'],int) and l['gross_cents']>=0 for l in rev['lines'])
        assert all(e['amount_cents']>=0 for e in rev['entries'] if e['entry_type']!='customer_adjustment')

def test_voucher_replay_reproduces_net_balances(tmp_path):
    db=seed(tmp_path);register_case(tmp_path,db,'C1',base_case())
    settle(db,'C1','parts_covered')
    doc2=copy.deepcopy(base_case());doc2['lines'][1]['gross_cents']=5000
    register_case(tmp_path,db,'C2',doc2,'case2.json');settle(db,'C2','parts_covered')
    doc3=copy.deepcopy(doc2);doc3['warranty_deductible_cents']=2500
    register_case(tmp_path,db,'C3',doc3,'case3.json');settle(db,'C3','parts_covered')
    c=connect(db)
    try:
        # replay every voucher in insertion order: running totals must match the stored net
        running={'customer':0,'insurer':0}
        for e in c.execute("SELECT party,amount_cents FROM settlement_entry ORDER BY id"):
            running[e['party']]+=e['amount_cents']
        stored={r['party']:r[1] for r in c.execute(
            "SELECT party,SUM(amount_cents) FROM settlement_entry GROUP BY party")}
        assert running==stored
        # each new revision's reversal entries exactly cancel its immediate
        # predecessor's effective entries (the predecessor already cancelled older ones)
        rev_ids=[r[0] for r in c.execute('SELECT id FROM settlement_revision ORDER BY revision_no')]
        for rid,prev_id in zip(rev_ids[1:],rev_ids[:-1]):
            eff=dict(c.execute("SELECT party,COALESCE(SUM(amount_cents),0) FROM settlement_entry "
                               "WHERE revision_id=? AND entry_type!='reversal' GROUP BY party",(prev_id,)).fetchall())
            reversals=dict(c.execute("SELECT party,COALESCE(SUM(amount_cents),0) FROM settlement_entry "
                                     "WHERE revision_id=? AND entry_type='reversal' GROUP BY party",(rid,)).fetchall())
            for party in ('customer','insurer'):
                assert eff.get(party,0)+reversals.get(party,0)==0
    finally:c.close()

def test_invoice_identity_and_amount_never_rewritten_across_revisions(tmp_path):
    db=seed(tmp_path);register_case(tmp_path,db,'C1',base_case())
    settle(db,'C1','parts_covered')
    for gross in (4500,3000,6000):
        doc=copy.deepcopy(base_case());doc['lines'][1]['gross_cents']=gross
        register_case(tmp_path,db,f'C{gross}',doc,f'case{gross}.json')
        settle(db,f'C{gross}','parts_covered')
    c=connect(db)
    try:
        rows=[dict(r) for r in c.execute('SELECT * FROM issued_invoice')]
        assert rows==[{'id':'INV-001','work_order':'WO-1','customer_due_cents':8000}]
        # every revision points back at the same immutable invoice identity
        assert {r[0] for r in c.execute('SELECT DISTINCT invoice_id FROM settlement_revision')}=={'INV-001'}
        assert all(r[0]==8000 for r in c.execute('SELECT DISTINCT invoice_customer_due_cents FROM settlement_revision'))
    finally:c.close()

def test_report_covers_held_and_billed_streams(tmp_path):
    db=seed(tmp_path)
    bad=base_case();bad['lines'][0]['gross_cents']=-1
    bad['contracts']=[{'id':'held_contract','covered_kinds':['part']}]
    register_case(tmp_path,db,'CH',bad,'bad.json');settle(db,'CH','held_contract')
    register_case(tmp_path,db,'COK',base_case(),'ok.json');settle(db,'COK','parts_covered')
    out=settlement_report(db,tmp_path/'r.json')
    data=json.loads(out.read_text(encoding='utf-8'))
    statuses={s['status'] for s in data['streams']}
    assert statuses=={'待核','已出账'}
    held=next(s for s in data['streams'] if s['status']=='待核')
    assert held['customer_net_cents']==0 and held['insurer_net_cents']==0
    assert held['revisions'][-1]['reasons']==['lines_invalid']
