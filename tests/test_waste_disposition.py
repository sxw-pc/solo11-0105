import json, shutil
from decimal import Decimal
from pathlib import Path
import pytest
from maintlog.__main__ import initialize
from maintlog.ingest import ingest
from maintlog.rules import evaluate, service_basis
from maintlog.waste_disposition import (
    register, dispose, sign_receipt, reinspect, carrier_return,
    conservation_report, write_reports, Q,
)
from maintlog.store import connect

FIX = Path(__file__).resolve().parents[1] / 'fixtures'
CSV_HEADER = 'container,gross_kg,contaminant_kg,seal,inspection_batch\n'


def prepared_db(tmp):
    db = tmp / 'x.db'
    initialize(db, FIX / 'config.json')
    assert ingest(FIX / 'events.jsonl', db)['imported'] == 6
    assert evaluate(db) == {'WO-1': '完成'}
    return db


def write_case(tmp, rows, case_doc=None, name='case.json', csv_name='waste-containers.csv'):
    (tmp / csv_name).write_text(CSV_HEADER + ''.join(rows), encoding='utf-8')
    doc = case_doc if case_doc is not None else json.loads((FIX / 'waste-case.json').read_text())
    doc = dict(doc)
    doc['containers_file'] = csv_name
    p = tmp / name
    p.write_text(json.dumps(doc, ensure_ascii=False), encoding='utf-8')
    return p


def standard_case(tmp, case_id='W'):
    db = prepared_db(tmp)
    case = write_case(tmp, [
        'C1,6.000,1.500,S1,IB-WO1-01\n',
        'C2,4.000,0.500,S2,IB-WO1-02\n',
    ])
    register(db, case_id, case)
    return db


def assert_balanced(report):
    assert report['conservation']['balanced'] is True
    for check in report['conservation']['checks']:
        assert check['mass_balanced'] and check['contaminant_balanced']
        assert check['clean_fluid_pure'] and check['parent_child_structure']


def test_closed_loop_path_material_flow_and_conservation(tmp_path):
    db = standard_case(tmp_path)
    out = dispose(db, 'W', 'closed_loop_sep')
    by_c = {x['container']: x for x in out['processed']}
    assert by_c['C1']['reuse_fluid_kg'] == '4.500' and by_c['C1']['hazard_residue_kg'] == '1.500'
    assert by_c['C2']['reuse_fluid_kg'] == '3.500' and by_c['C2']['hazard_residue_kg'] == '0.500'
    r = conservation_report(db, 'W')
    assert_balanced(r)
    assert r['totals']['reusable_clean_kg'] == '8.000'
    assert r['totals']['hazard_residue_on_site_kg'] == '2.000'
    assert r['totals']['hazard_shipped_net_kg'] == '0.000'
    # pollutants can never enter the reusable fluid; identities remain unique
    c = connect(db)
    rows = c.execute("SELECT id,kind,hazard_class,contaminant_kg FROM material_batch ORDER BY id").fetchall()
    ids = [x['id'] for x in rows]
    assert len(ids) == len(set(ids))
    for x in rows:
        if x['kind'] == 'reuse_fluid':
            assert x['contaminant_kg'] == '0.000' and x['hazard_class'] == '非危废'
        else:
            assert x['hazard_class'] == '危废'
    c.close()


def test_sealed_ship_path_keeps_sealed_identity_and_manifest(tmp_path):
    db = standard_case(tmp_path)
    dispose(db, 'W', 'sealed_ship')
    sign_receipt(db, 'W')  # frozen receipt REC-M1: full 10.000
    r = conservation_report(db, 'W')
    assert_balanced(r)
    assert r['totals']['reusable_clean_kg'] == '0.000'
    assert r['totals']['hazard_shipped_net_kg'] == '10.000'
    assert r['totals']['sealed_hazard_on_site_kg'] == '0.000'
    m = r['manifests'][0]
    assert m['manifest_id'] == 'M-1' and m['status'] == '完全签收'
    c = connect(db)
    parents = c.execute("SELECT container,custody,remaining_kg FROM material_batch WHERE kind='sealed_parent' ORDER BY container").fetchall()
    assert [tuple(x) for x in parents] == [('C1', '已交接承运方', '0.000'), ('C2', '已交接承运方', '0.000')]
    # no reuse material exists on the sealed route
    assert c.execute("SELECT count(*) FROM material_batch WHERE kind='reuse_fluid'").fetchone()[0] == 0
    c.close()


def test_two_routes_change_real_outcomes_but_both_balance(tmp_path):
    d1 = standard_case(tmp_path / 'a')
    dispose(d1, 'W', 'closed_loop_sep')
    r1 = conservation_report(d1, 'W')
    d2 = standard_case(tmp_path / 'b')
    dispose(d2, 'W', 'sealed_ship')
    sign_receipt(d2, 'W')
    r2 = conservation_report(d2, 'W')
    for r in (r1, r2):
        assert_balanced(r)
        assert r['totals']['original_gross_kg'] == '10.000'
        assert r['totals']['original_contaminant_kg'] == '2.000'
    assert (r1['totals']['reusable_clean_kg'], r1['totals']['hazard_shipped_net_kg']) == ('8.000', '0.000')
    assert (r2['totals']['reusable_clean_kg'], r2['totals']['hazard_shipped_net_kg']) == ('0.000', '10.000')


def test_duplicate_register_rerun_creates_no_material(tmp_path):
    db = prepared_db(tmp_path)
    case = write_case(tmp_path, ['C1,6.000,1.500,S1,IB1\n', 'C2,4.000,0.500,S2,IB2\n'])
    first = register(db, 'W', case)
    again = register(db, 'W', case)
    assert again == first
    dispose(db, 'W', 'closed_loop_sep')
    # re-register after processing still creates nothing and changes no mass
    assert register(db, 'W', case)['digest'] == first['digest']
    c = connect(db)
    assert c.execute("SELECT count(*) FROM material_batch").fetchone()[0] == 6
    assert c.execute("SELECT count(*) FROM material_event WHERE event_type='case_freeze'").fetchone()[0] == 2
    c.close()
    assert_balanced(conservation_report(db, 'W'))


def test_duplicate_container_row_rejected(tmp_path):
    db = prepared_db(tmp_path)
    case = write_case(tmp_path, ['C1,6.000,1.500,S1,IB1\n', 'C1,4.000,0.500,S2,IB2\n'])
    with pytest.raises(ValueError, match='duplicate_container'):
        register(db, 'W', case)
    c = connect(db)
    assert c.execute('SELECT count(*) FROM material_batch').fetchone()[0] == 0
    c.close()


def test_missing_weighing_negative_and_over_gross_rejected(tmp_path):
    cases = [
        ('C1,,1.500,S1,IB1\n', 'weighing_missing', 'empty_gross'),
        ('C1,6.000,,S1,IB1\n', 'weighing_missing', 'empty_contam'),
        ('C1,-1.000,0.500,S1,IB1\n', 'invalid_waste_mass', 'negative'),
        ('C1,1.000,1.500,S1,IB1\n', 'invalid_waste_mass', 'over_gross'),
        ('C1,1.000,-0.001,S1,IB1\n', 'invalid_waste_mass', 'negative_contam'),
    ]
    for row, code, tag in cases:
        d = tmp_path / tag
        d.mkdir()
        db = prepared_db(d)
        case = write_case(d, [row])
        with pytest.raises(ValueError, match=code):
            register(db, 'W', case)


def test_decimal_boundary_precision_and_exact_arithmetic(tmp_path):
    # four-decimal masses violate the weighing precision rule
    dp = tmp_path / 'prec'
    dp.mkdir()
    db = prepared_db(dp)
    with pytest.raises(ValueError, match='mass_precision_exceeded'):
        register(db, 'W', write_case(dp, ['C1,0.0001,0.000,S1,IB1\n']))
    # 0.000 contaminant and 3-decimal masses are valid; exact decimals throughout
    d = tmp_path / 'ok'
    d.mkdir()
    db = prepared_db(d)
    case = write_case(d, ['C1,0.001,0.000,S1,IB1\n'])
    register(db, 'W', case)
    dispose(db, 'W', 'closed_loop_sep')
    r = conservation_report(db, 'W')
    assert_balanced(r)
    c = connect(db)
    vals = [x[0] for x in c.execute("SELECT gross_kg FROM material_batch").fetchall()]
    assert all(Decimal(v).as_tuple().exponent >= -3 for v in vals)
    assert c.execute("SELECT gross_kg FROM material_batch WHERE kind='reuse_fluid'").fetchone()[0] == '0.001'
    c.close()


def test_seal_mismatch_holds_container_without_moving_mass(tmp_path):
    db = standard_case(tmp_path)
    c = connect(db)
    c.execute("UPDATE waste_container SET seal='S9' WHERE id='C1'")
    c.commit()
    c.close()
    out = dispose(db, 'W', 'closed_loop_sep')
    assert out['holds'] == [{'container': 'C1', 'code': 'seal_mismatch'}]
    r = conservation_report(db, 'W')
    assert_balanced(r)
    assert 'C1' in r['totals']['pending_review_containers']
    c = connect(db)
    assert c.execute("SELECT custody FROM material_batch WHERE id='W:P:C1'").fetchone()[0] == '待核'
    # C2 may still be processed; C1 has no children
    assert c.execute("SELECT count(*) FROM material_batch WHERE parent_id='W:P:C1'").fetchone()[0] == 0
    c.close()


def test_container_cannot_be_both_separated_and_shipped(tmp_path):
    db = standard_case(tmp_path)
    dispose(db, 'W', 'sealed_ship')
    out = dispose(db, 'W', 'closed_loop_sep')
    assert {h['code'] for h in out['holds']} == {'route_conflict'}
    r = conservation_report(db, 'W')
    c = connect(db)
    assert c.execute("SELECT count(*) FROM material_batch WHERE kind='reuse_fluid'").fetchone()[0] == 0
    assert c.execute("SELECT count(*) FROM material_event WHERE event_type='separation'").fetchone()[0] == 0
    c.close()
    assert_balanced(r)


def test_partial_signoff_keeps_unsigned_mass_in_real_custody(tmp_path):
    db = standard_case(tmp_path)
    dispose(db, 'W', 'sealed_ship')
    receipt = {'id': 'REC-C1', 'carrier': 'CAR-1', 'signed_by': 'DRIVER-2',
               'at': '2026-01-01T11:00:00+00:00',
               'lines': [{'container': 'C1', 'accepted_kg': '6.000'},
                         {'container': 'C2', 'accepted_kg': '0.000'}]}
    out = sign_receipt(db, 'W', receipt)
    statuses = {x['container']: (x['status'], x['signed_kg'], x['unsigned_kg']) for x in out['lines']}
    assert statuses['C1'] == ('已签收', '6.000', '0.000')
    assert statuses['C2'] == ('拒收', '0.000', '4.000')
    r = conservation_report(db, 'W')
    assert_balanced(r)
    assert r['totals']['hazard_shipped_net_kg'] == '6.000'
    assert r['totals']['sealed_hazard_on_site_kg'] == '4.000'
    assert r['manifests'][0]['status'] == '部分签收'
    c = connect(db)
    assert tuple(c.execute("SELECT custody,remaining_kg FROM material_batch WHERE id='W:P:C2'").fetchone()) \
        == ('待运', '4.000')
    c.close()


def test_receipt_mass_and_manifest_mismatch_rejected(tmp_path):
    db = standard_case(tmp_path)
    dispose(db, 'W', 'sealed_ship')
    bad = {'id': 'REC-X', 'carrier': 'CAR-1', 'signed_by': 'D', 'at': '2026-01-01T11:00:00+00:00',
           'accepted_containers': ['C1', 'C2'], 'accepted_kg': '9.000'}
    with pytest.raises(ValueError, match='receipt_mass_mismatch'):
        sign_receipt(db, 'W', bad)
    bad_carrier = dict(bad, accepted_kg='10.000', carrier='CAR-9')
    with pytest.raises(ValueError, match='carrier_mismatch'):
        sign_receipt(db, 'W', bad_carrier)
    c = connect(db)
    assert c.execute("SELECT count(*) FROM material_event WHERE event_type='shipment_signoff'").fetchone()[0] == 0
    c.close()


def test_expired_cert_and_license_hold_entire_batch(tmp_path):
    for route, field, label in [('closed_loop_sep', 'cert_valid_until', 'cert_expired'),
                                ('sealed_ship', 'license_valid_until', 'carrier_license_expired')]:
        d = tmp_path / route
        d.mkdir()
        doc = json.loads((FIX / 'waste-case.json').read_text())
        for ct in doc['contracts']:
            if ct['id'] == route:
                ct[field] = '2025-12-31T00:00:00+00:00'
        db = prepared_db(d)
        case = write_case(d, ['C1,6.000,1.500,S1,IB1\n', 'C2,4.000,0.500,S2,IB2\n'], case_doc=doc)
        register(db, 'W', case)
        out = dispose(db, 'W', route, at='2026-02-01T00:00:00+00:00')
        assert {h['code'] for h in out['holds']} == {label}
        assert_balanced(conservation_report(db, 'W'))


def test_reinspection_failure_holds_then_pass_releases(tmp_path):
    db = standard_case(tmp_path)
    fail = {'result': 'fail', 'voucher_id': 'V-FAIL', 'inspector': 'ENV-TECH-2',
            'at': '2026-01-02T09:00:00+00:00', 'detail': {'note': 'turbidity'}}
    out = dispose(db, 'W', 'closed_loop_sep', verification=fail)
    assert {h['code'] for h in out['holds']} == {'reinspection_failed'}
    assert_balanced(conservation_report(db, 'W'))
    reinspect(db, 'W', 'W:P:C1', 'pass', 'V-PASS-1', 'ENV-TECH-2', at='2026-01-03T09:00:00+00:00')
    reinspect(db, 'W', 'W:P:C2', 'pass', 'V-PASS-2', 'ENV-TECH-2', at='2026-01-03T09:00:00+00:00')
    out = dispose(db, 'W', 'closed_loop_sep')
    assert out['holds'] == [] and len(out['processed']) == 2
    r = conservation_report(db, 'W')
    assert_balanced(r)
    # old failure events and vouchers remain append-only
    results = [(v['voucher_id'], v['result']) for v in r['verifications']]
    assert ('V-FAIL-C1', 'fail') in results and ('V-PASS-1', 'pass') in results


def test_carrier_return_never_silently_restores_stock(tmp_path):
    db = standard_case(tmp_path)
    dispose(db, 'W', 'sealed_ship')
    sign_receipt(db, 'W')
    before = conservation_report(db, 'W')
    event_count_before = len(before['events'])
    out = carrier_return(db, 'W', 'C1', '2.000', 'RET-1', 'DRIVER-1',
                         at='2026-01-02T08:00:00+00:00', reason='manifest correction')
    assert out['custody'] == '待核'
    r = conservation_report(db, 'W')
    assert_balanced(r)
    # old sign-off event retained; only the return event is appended for mass flow
    assert len(r['events']) == event_count_before + 1
    assert r['totals']['hazard_shipped_net_kg'] == '8.000'
    assert 'C1' in r['totals']['pending_review_containers']
    c = connect(db)
    row = c.execute("SELECT custody,remaining_kg,remaining_contaminant_kg FROM material_batch WHERE id='W:P:C1'").fetchone()
    assert tuple(row) == ('待核', '2.000', '0.500')
    # original sign-off event still present with its negative delta
    assert c.execute("SELECT count(*) FROM material_event WHERE event_type='shipment_signoff'").fetchone()[0] == 2
    with pytest.raises(ValueError, match='return_mass_exceeds'):
        carrier_return(db, 'W', 'C1', '9.000', 'RET-2', 'DRIVER-1')
    c.close()


def test_parent_child_no_double_counting(tmp_path):
    db = standard_case(tmp_path)
    dispose(db, 'W', 'closed_loop_sep')
    r = conservation_report(db, 'W')
    # site book = reusable fluid + residue; consumed parent contributes zero remaining
    t = r['totals']
    assert Decimal(t['reusable_clean_kg']) + Decimal(t['hazard_residue_on_site_kg']) \
        == Decimal(t['original_gross_kg'])
    for container in r['containers']:
        parent = next(b for b in container['batches'] if b['kind'] == 'sealed_parent')
        assert parent['remaining_kg'] == '0.000'
        book = sum((Decimal(b['remaining_kg']) for b in container['batches']), Decimal('0'))
        assert book == Decimal(parent['gross_kg'])


def test_decision_never_rewritten_and_parts_are_not_waste_vouchers(tmp_path):
    db = standard_case(tmp_path)
    before = service_basis(db, 'WO-1')
    dispose(db, 'W', 'closed_loop_sep')
    after = service_basis(db, 'WO-1')
    assert before == after and after['status'] == '完成'
    c = connect(db)
    d = c.execute('SELECT * FROM decision WHERE work_order=?', ('WO-1',)).fetchone()
    assert d['status'] == '完成'
    # waste material only derives from waste batches; stock table has no liquid voucher role
    assert c.execute("SELECT count(*) FROM material_batch").fetchone()[0] == 6
    assert c.execute("SELECT count(*) FROM material_batch WHERE id LIKE '%NEW-1%' OR id LIKE '%OLD-1%'").fetchone()[0] == 0
    c.close()


def test_report_files_and_event_provenance(tmp_path):
    db = standard_case(tmp_path)
    dispose(db, 'W', 'closed_loop_sep')
    out, manifest = write_reports(db, 'W', tmp_path / 'material.json')
    data = json.loads(out.read_text(encoding='utf-8'))
    assert data['work_order'] == 'WO-1' and data['conservation']['balanced']
    assert json.loads(manifest.read_text(encoding='utf-8'))['case_id'] == 'W'
    c = connect(db)
    # seal, weighing source SHA, work order and inspection linkage survive on every batch
    for b in c.execute('SELECT * FROM material_batch').fetchall():
        assert b['work_order'] == 'WO-1' and b['inspection_batch']
        assert len(b['source_sha256']) == 64
        if b['kind'] == 'sealed_parent':
            assert b['seal'] in ('S1', 'S2')
    # immutable event digest uniqueness
    digests = [x[0] for x in c.execute('SELECT digest FROM material_event').fetchall()]
    assert len(digests) == len(set(digests))
    c.close()


def test_parent_already_transferred_blocks_second_disposition(tmp_path):
    db = standard_case(tmp_path)
    dispose(db, 'W', 'sealed_ship')
    sign_receipt(db, 'W')
    out = dispose(db, 'W', 'sealed_ship')
    assert {h['code'] for h in out['holds']} == {'parent_already_transferred'}
    assert_balanced(conservation_report(db, 'W'))


def test_staged_partial_signoff_conserves_contaminant(tmp_path):
    db = standard_case(tmp_path)
    dispose(db, 'W', 'sealed_ship')
    r1 = {'id': 'R1', 'carrier': 'CAR-1', 'signed_by': 'D', 'at': '2026-01-01T11:00:00+00:00',
          'lines': [{'container': 'C1', 'accepted_kg': '3.000'}, {'container': 'C2', 'accepted_kg': '0.000'}]}
    sign_receipt(db, 'W', r1)
    r2 = {'id': 'R2', 'carrier': 'CAR-1', 'signed_by': 'D', 'at': '2026-01-01T12:00:00+00:00',
          'lines': [{'container': 'C1', 'accepted_kg': '3.000'}, {'container': 'C2', 'accepted_kg': '4.000'}]}
    out = sign_receipt(db, 'W', r2)
    assert {x['status'] for x in out['lines']} == {'已签收'}
    # replaying the earlier receipt is idempotent and moves no additional mass
    assert sign_receipt(db, 'W', r1)['replayed'] is True
    r = conservation_report(db, 'W')
    assert_balanced(r)
    assert r['totals']['hazard_shipped_net_kg'] == '10.000'
    # contaminant left proportionally: 1.500 + 0.500 across the two receipts
    c = connect(db)
    total_c = sum((Decimal(x[0]) for x in c.execute("SELECT signed_contaminant_kg FROM manifest_line")), Decimal('0'))
    assert total_c.quantize(Q) == Decimal('2.000')
    c.close()


def test_separation_then_sealed_ship_is_blocked(tmp_path):
    db = standard_case(tmp_path)
    dispose(db, 'W', 'closed_loop_sep')
    out = dispose(db, 'W', 'sealed_ship')
    assert {h['code'] for h in out['holds']} == {'parent_already_transferred'}
    c = connect(db)
    assert c.execute("SELECT count(*) FROM waste_manifest").fetchone()[0] == 0
    c.close()
    assert_balanced(conservation_report(db, 'W'))


def test_separation_is_atomic_on_failure(tmp_path):
    import sqlite3
    db = standard_case(tmp_path)
    c = connect(db)
    c.execute("CREATE TRIGGER reject_child BEFORE INSERT ON material_batch "
              "WHEN NEW.kind='reuse_fluid' BEGIN SELECT RAISE(ABORT,'injected'); END")
    c.commit()
    c.close()
    with pytest.raises(sqlite3.IntegrityError):
        dispose(db, 'W', 'closed_loop_sep')
    c = connect(db)
    # no half-created children, no separation events, parents still sealed
    assert c.execute("SELECT count(*) FROM material_batch WHERE kind!='sealed_parent'").fetchone()[0] == 0
    assert c.execute("SELECT count(*) FROM material_event WHERE event_type='separation'").fetchone()[0] == 0
    assert c.execute("SELECT custody FROM material_batch WHERE id='W:P:C1'").fetchone()[0] == '现场封存'
    c.close()
    # once the trigger is gone, the same command succeeds normally
    c = connect(db)
    c.execute('DROP TRIGGER reject_child')
    c.commit()
    c.close()
    dispose(db, 'W', 'closed_loop_sep')
    assert_balanced(conservation_report(db, 'W'))


def test_contaminant_decimal_boundary_and_zero(tmp_path):
    d = tmp_path / 'z'
    d.mkdir()
    db = prepared_db(d)
    case = write_case(d, ['C1,6.000,0.000,S1,IB1\n'])
    register(db, 'W', case)
    dispose(db, 'W', 'closed_loop_sep')
    r = conservation_report(db, 'W')
    assert_balanced(r)
    assert r['totals']['reusable_clean_kg'] == '6.000'
    assert r['totals']['hazard_residue_on_site_kg'] == '0.000'
    c = connect(db)
    res = c.execute("SELECT custody FROM material_batch WHERE kind='hazard_residue'").fetchone()
    # zero-mass residue still has an identity batch but no material on the books
    assert res['custody'] == '现场封存'
    c.close()
