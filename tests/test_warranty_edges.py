"""Held settlements, part/source identity conflicts and multi-line apportionment."""
import copy,json
from pathlib import Path
import pytest
from maintlog.store import connect
from maintlog.ingest import ingest
from maintlog.warranty_billing import settle,largest_remainder
from maintlog.rules import evaluate
from warranty_helpers import FIX,base_case,seed,register_case,write_events,event_rows,entry_sums

def test_multi_line_deductible_charged_once_with_largest_remainder(tmp_path):
    # three covered part lines 100/100/100, deductible 10 -> insurer 290 split 97/97/96
    db=tmp_path/'maintlog.db'
    from maintlog.__main__ import initialize
    initialize(db,FIX/'config.json')
    c=connect(db)
    c.execute("INSERT INTO stock VALUES ('P1','WO-1')")
    c.execute("INSERT INTO stock VALUES ('P2','WO-1')")
    c.commit();c.close()
    ingest(FIX/'events.jsonl',db)
    extra=[
        {'kind':'replace','device':'T2','seq':1,'work_order':'WO-1','at':'2026-01-01T08:16:00+00:00','old':'OLD-A','new':'P1'},
        {'kind':'replace','device':'T2','seq':2,'work_order':'WO-1','at':'2026-01-01T08:17:00+00:00','old':'OLD-B','new':'P2'},
    ]
    write_events(tmp_path,db,extra,'parts.jsonl')
    assert evaluate(db)=={'WO-1':'完成'}
    doc=base_case()
    doc['warranty_deductible_cents']=10
    doc['lines']=[
        {'kind':'part','item':'P1','gross_cents':100},
        {'kind':'part','item':'P2','gross_cents':100},
        {'kind':'part','item':'NEW-1','gross_cents':100},
    ]
    doc['contracts']=[{'id':'parts_only','covered_kinds':['part']}]
    register_case(tmp_path,db,'C1',doc)
    rev=settle(db,'C1','parts_only')
    assert rev['status']=='已出账'
    assert rev['gross_total_cents']==300 and rev['insurer_due_cents']==290 and rev['customer_due_cents']==10
    shares=[l['insurer_cents'] for l in rev['lines']]
    assert shares==[97,97,96]                           # largest remainder, index-stable ties
    assert [l['customer_cents'] for l in rev['lines']]==[3,3,4]
    assert sum(shares)==290
    # exactly one deductible across the whole order, never one per line
    assert sum(l['customer_cents'] for l in rev['lines'])==10

def test_largest_remainder_is_integer_and_stable():
    assert largest_remainder([1,1,1],10)==[4,3,3]
    assert largest_remainder([333,333,334],100)==[33,33,34]
    assert largest_remainder([0,0,5],7)==[0,0,7]
    assert largest_remainder([5,5,5],0)==[0,0,0]
    weights=list(range(1,50))
    for total in (1,7,99,1000):
        out=largest_remainder(weights,total)
        assert sum(out)==total and all(isinstance(x,int) for x in out)
    with pytest.raises(ValueError):largest_remainder([1,2],-1)
    with pytest.raises(ValueError):largest_remainder([1.5],1)

def test_deductible_larger_than_covered_gross_caps_insurer_at_zero(tmp_path):
    doc=base_case();doc['warranty_deductible_cents']=99999
    db=seed(tmp_path);register_case(tmp_path,db,'C1',doc)
    rev=settle(db,'C1','parts_covered')
    assert rev['insurer_due_cents']==0 and rev['customer_due_cents']==14000
    assert rev['customer_due_cents']+rev['insurer_due_cents']==rev['gross_total_cents']

def test_unfinished_order_isolation(tmp_path):
    # a second, not-completed work order must not produce any billable settlement
    db=seed(tmp_path)
    c=connect(db)
    c.execute("INSERT INTO work_order VALUES ('WO-2','EQ-2','P1','待核')")
    c.execute("INSERT INTO step VALUES ('WO-2',1,'x',1)")
    c.commit();c.close()
    evaluate(db)
    doc=copy.deepcopy(base_case());doc['work_order']='WO-2'
    doc['invoice']={'id':'INV-002','customer_due_cents':100}
    p=tmp_path/'case2.json';p.write_text(json.dumps(doc),encoding='utf-8')
    with pytest.raises(ValueError,match='billing_order_not_complete'):
        from maintlog.warranty_billing import register
        register(db,'C2',p)
    # WO-1's own settlement remains isolated and unaffected
    register_case(tmp_path,db,'C1',base_case())
    rev=settle(db,'C1','parts_covered')
    assert rev['status']=='已出账' and rev['work_order']=='WO-1'
    c=connect(db)
    try:assert c.execute("SELECT COUNT(*) FROM settlement_stream WHERE work_order='WO-2'").fetchone()[0]==0
    finally:c.close()

@pytest.mark.parametrize('mutation,expected',[
    (lambda r:r[1].__setitem__('position',2),'需返工'),
    (lambda r:r.append({'kind':'void','device':'T1','seq':7,'work_order':'WO-1','at':'2026-01-01T08:30:00+00:00'}),'作废'),
])
def test_rework_and_void_orders_cannot_be_registered(tmp_path,mutation,expected):
    db=tmp_path/'hold.db'
    from maintlog.__main__ import initialize as _init
    _init(db,FIX/'config.json')
    rows=event_rows();mutation(rows)
    write_events(tmp_path,db,rows,'bad.jsonl')
    assert evaluate(db)['WO-1']==expected
    p=tmp_path/'case.json';p.write_text(json.dumps(base_case()),encoding='utf-8')
    from maintlog.warranty_billing import register
    with pytest.raises(ValueError,match='billing_order_not_complete'):
        register(db,'CHOLD',p)
    c=connect(db)
    try:
        assert c.execute('SELECT COUNT(*) FROM settlement_stream').fetchone()[0]==0
        assert c.execute('SELECT COUNT(*) FROM settlement_revision').fetchone()[0]==0
    finally:c.close()

def test_source_identity_conflict_holds_whole_settlement(tmp_path):
    db=seed(tmp_path)
    rows=event_rows();rows[1]['result']='fail'      # same device/seq, different payload
    write_events(tmp_path,db,rows)
    register_case(tmp_path,db,'C1',base_case())
    rev=settle(db,'C1','parts_covered')
    assert rev['status']=='待核' and rev['reasons']==['source_conflict']
    assert rev['lines']==[] and rev['entries']==[]  # no partial lines, no free parts
    c=connect(db)
    try:
        assert c.execute('SELECT status FROM settlement_stream').fetchone()[0]=='待核'
        assert c.execute('SELECT COUNT(*) FROM settlement_entry').fetchone()[0]==0
        assert c.execute('SELECT COUNT(*) FROM settlement_line').fetchone()[0]==0
    finally:c.close()
    # replay of the same held snapshot is idempotent: no duplicate revisions
    again=settle(db,'C1','parts_covered')
    assert again['id']==rev['id']

def test_part_identity_conflict_holds_settlement(tmp_path):
    db=seed(tmp_path)
    # billed part was never issued to this work order / has no matching replacement
    doc=base_case()
    doc['lines'].append({'kind':'part','item':'GHOST-9','gross_cents':500})
    register_case(tmp_path,db,'C1',doc)
    rev=settle(db,'C1','parts_covered')
    assert rev['status']=='待核' and 'part_identity_conflict' in rev['reasons']
    assert rev['entries']==[]
    assert entry_sums(db,'WO-1','parts_covered')=={'customer':0,'insurer':0}

def test_invoice_conflict_holds_and_original_invoice_untouched(tmp_path):
    db=seed(tmp_path)
    # the canonical case publishes INV-001 at 8000
    register_case(tmp_path,db,'COK',base_case(),'ok.json')
    # a later case claiming a different amount for the same invoice id is rejected at freeze time
    doc=base_case();doc['invoice']={'id':'INV-001','customer_due_cents':9999}
    with pytest.raises(ValueError,match='invoice_conflict'):
        register_case(tmp_path,db,'CINV',doc,'inv.json')
    c=connect(db)
    try:assert c.execute('SELECT customer_due_cents FROM issued_invoice WHERE id=?',('INV-001',)).fetchone()[0]==8000
    finally:c.close()
    # if the live invoice row is later tampered with, settlement holds instead of billing
    c=connect(db);c.execute("UPDATE issued_invoice SET customer_due_cents=7777 WHERE id='INV-001'");c.commit();c.close()
    rev=settle(db,'COK','parts_covered')
    assert rev['status']=='待核' and 'invoice_conflict' in rev['reasons']
    assert rev['entries']==[]

def test_invalid_prices_and_contracts_hold(tmp_path):
    db=seed(tmp_path)
    bad=base_case();bad['lines'][0]['gross_cents']=-1
    register_case(tmp_path,db,'CBAD',bad,'bad.json')
    rev=settle(db,'CBAD','parts_covered')
    assert rev['status']=='待核' and 'lines_invalid' in rev['reasons']
    bad2=base_case();bad2['warranty_deductible_cents']=1.5
    register_case(tmp_path,db,'CBAD2',bad2,'bad2.json')
    rev2=settle(db,'CBAD2','parts_covered')
    assert 'deductible_invalid' in rev2['reasons']
    bad3=base_case();bad3['contracts']=[{'id':'x','covered_kinds':[]}]
    register_case(tmp_path,db,'CBAD3',bad3,'bad3.json')
    rev3=settle(db,'CBAD3','x')
    assert 'contracts_invalid' in rev3['reasons']

def test_held_then_recovered_opens_billed_revision_without_phantom_reversals(tmp_path):
    # first frozen case has invalid prices -> held revision with no entries
    db=seed(tmp_path)
    bad=base_case();bad['lines'][0]['gross_cents']=-1
    register_case(tmp_path,db,'CHELD',bad,'held.json')
    held=settle(db,'CHELD','parts_covered')
    assert held['status']=='待核' and held['revision_no']==1 and held['entries']==[]
    # repaired case on the same (work order, contract) stream -> billed revision 2,
    # and since the held revision created no entries, no phantom reversals appear
    register_case(tmp_path,db,'COK',base_case(),'clean.json')
    ok=settle(db,'COK','parts_covered')
    assert ok['status']=='已出账' and ok['revision_no']==2
    assert not any(e['entry_type']=='reversal' for e in ok['entries'])
    c=connect(db)
    try:
        assert c.execute("SELECT status FROM settlement_stream WHERE contract_id='parts_covered'").fetchone()[0]=='已出账'
    finally:c.close()

def test_contract_covering_no_billed_line_leaves_whole_gross_to_customer(tmp_path):
    db=seed(tmp_path)
    doc=base_case()
    doc['contracts']=[{'id':'none_such','covered_kinds':['part']}]
    doc['lines']=[{'kind':'labor','item':'service','gross_cents':10000}]
    register_case(tmp_path,db,'C1',doc)
    rev=settle(db,'C1','none_such')
    assert rev['status']=='已出账'
    assert rev['insurer_due_cents']==0 and rev['customer_due_cents']==10000
    # nothing covered: no deductible effect, invoice gap only
    assert rev['entries'][-1]['amount_cents']==10000-8000
