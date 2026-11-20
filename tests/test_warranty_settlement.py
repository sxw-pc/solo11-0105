"""Post-warranty customer/insurer settlement against the immutable issued invoice."""
import copy,json
from pathlib import Path
import pytest
from maintlog.store import connect
from maintlog.warranty_billing import settle,settlement_report,largest_remainder
from warranty_helpers import FIX,base_case,seed,register_case,registered,entry_sums,invoice_amount

def _lines(rev):return {l['line_no']:dict(l) for l in rev['lines']}
def _entries(rev):return [(e['entry_type'],e['party'],e['amount_cents']) for e in rev['entries']]

def test_parts_covered_contract_supplementary_charge(tmp_path):
    db=registered(tmp_path)
    rev=settle(db,'WARRANTY-1','parts_covered')
    assert rev['status']=='已出账' and rev['revision_no']==1
    # gross 14000 = labor 10000 + part 4000; deductible 1000 once on covered part lines
    assert rev['gross_total_cents']==14000
    assert rev['insurer_due_cents']==3000          # 4000 part - 1000 deductible
    assert rev['customer_due_cents']==11000        # 10000 labor + 1000 deductible
    assert rev['customer_due_cents']+rev['insurer_due_cents']==rev['gross_total_cents']
    lines=_lines(rev)
    assert lines[0]['customer_cents']==10000 and lines[0]['insurer_cents']==0 and lines[0]['covered']==0
    assert lines[1]['customer_cents']==1000 and lines[1]['insurer_cents']==3000 and lines[1]['covered']==1
    # INV-001 (8000) is never rewritten; the gap is a +3000 customer entry
    assert ('customer_adjustment','customer',3000) in _entries(rev)
    assert ('insurer_receivable','insurer',3000) in _entries(rev)
    assert invoice_amount(db)==8000
    nets=entry_sums(db,'WO-1','parts_covered')
    assert nets=={'customer':3000,'insurer':3000}

def test_labor_covered_contract_refund(tmp_path):
    db=registered(tmp_path)
    rev=settle(db,'WARRANTY-1','labor_covered')
    assert rev['status']=='已出账'
    assert rev['insurer_due_cents']==9000          # 10000 labor - 1000 deductible
    assert rev['customer_due_cents']==5000         # 1000 deductible + 4000 part
    assert rev['customer_due_cents']+rev['insurer_due_cents']==rev['gross_total_cents']
    # customer already paid 8000 on INV-001 but only owes 5000 -> refund 3000
    assert ('customer_adjustment','customer',-3000) in _entries(rev)
    assert ('insurer_receivable','insurer',9000) in _entries(rev)
    nets=entry_sums(db,'WO-1','labor_covered')
    assert nets=={'customer':-3000,'insurer':9000}

def test_two_contracts_are_independent_opposite_positions(tmp_path):
    # both contracts freeze the same technical facts; neither may be picked or merged
    db=registered(tmp_path)
    a=settle(db,'WARRANTY-1','parts_covered')
    b=settle(db,'WARRANTY-1','labor_covered')
    c=connect(db)
    try:
        streams=[dict(r) for r in c.execute('SELECT * FROM settlement_stream ORDER BY contract_id')]
        assert {s['contract_id'] for s in streams}=={'parts_covered','labor_covered'}
        assert all(s['status']=='已出账' for s in streams)
    finally:c.close()
    assert a['customer_due_cents']==11000 and b['customer_due_cents']==5000
    pa=entry_sums(db,'WO-1','parts_covered');lb=entry_sums(db,'WO-1','labor_covered')
    assert pa['customer']==3000 and lb['customer']==-3000   # real charge vs real refund

def test_settle_unknown_contract_rejected(tmp_path):
    db=registered(tmp_path)
    with pytest.raises(ValueError,match='unknown_contract'):
        settle(db,'WARRANTY-1','not_a_contract')

def test_settle_unknown_case_rejected(tmp_path):
    db=seed(tmp_path)
    with pytest.raises(ValueError,match='unknown_case'):
        settle(db,'NOPE','parts_covered')
