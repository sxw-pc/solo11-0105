"""Per-container material flow and final disposition of service waste fluid.

The frozen waste case (see ``register``) only proves identity and mass of the
sealed containers.  This module moves real material ownership and custody:

* ``closed_loop_sep`` atomically consumes a sealed parent batch under a valid
  process certificate and creates traceable clean-fluid / concentrated-residue
  child batches plus a re-verification voucher;
* ``sealed_ship`` keeps the parent's sealed identity and tracks carrier
  sign-off mass and manifests, including rejection, partial sign-off and
  later carrier returns.

Every physical fact is an immutable ``material_event`` row with decimal mass
deltas.  Mass and contaminant mass balance before and after every conversion
or handover; pollutants can never enter the clean reusable fluid.  Errors that
cannot be trusted (missing weighing, seal mismatch, negative mass,
contaminant over gross, expired qualification, parent already transferred)
put the affected batch into ``待核`` custody instead of moving material.
"""
import csv, hashlib, json
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from .ingest import when
from .store import connect

Q = Decimal('0.001')
ROUTES = ('closed_loop_sep', 'sealed_ship')
HAZARD_PARENT = '危废'


def _canon(obj):
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode('utf-8')


def _digest(obj):
    return hashlib.sha256(_canon(obj)).hexdigest()


def _mass(value, code):
    """Parse an exact decimal mass; reject negatives, non-finite and >3 decimals."""
    try:
        d = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError(code)
    if not d.is_finite() or d < 0:
        raise ValueError(code)
    if d.as_tuple().exponent < -3:
        raise ValueError('mass_precision_exceeded')
    return d


def _q(d):
    return d.quantize(Q)


def _now():
    return datetime.now(timezone.utc).isoformat()


def _event(db, case_id, at, etype, contract_id, batch_id, container, gross_delta, contam_delta, detail):
    payload = {'case_id': case_id, 'at': at, 'event_type': etype, 'contract_id': contract_id,
               'batch_id': batch_id, 'container': container,
               'gross_delta': str(gross_delta), 'contaminant_delta': str(contam_delta), 'detail': detail}
    dig = _digest(payload)
    old = db.execute('SELECT id FROM material_event WHERE case_id=? AND digest=?', (case_id, dig)).fetchone()
    if old:
        return old['id']
    cur = db.execute(
        'INSERT INTO material_event(case_id,at,event_type,contract_id,batch_id,container,gross_delta,contaminant_delta,detail,digest)'
        ' VALUES (?,?,?,?,?,?,?,?,?,?)',
        (case_id, at, etype, contract_id, batch_id, container, str(gross_delta), str(contam_delta),
         json.dumps(detail, ensure_ascii=False, sort_keys=True), dig))
    return cur.lastrowid


def _hold(db, case_id, batch, contract_id, code, at, detail=None):
    """Mark a batch pending review. Never moves mass; event is immutable."""
    eid = _event(db, case_id, at, 'hold_pending_review', contract_id, batch['id'], batch['container'],
                 Decimal('0'), Decimal('0'), {'code': code, 'detail': detail or code})
    db.execute('UPDATE material_batch SET custody=?,last_event_id=? WHERE id=?', ('待核', eid, batch['id']))
    return eid


def _load_case(db, case_id):
    row = db.execute('SELECT * FROM waste_case WHERE id=?', (case_id,)).fetchone()
    if not row:
        raise ValueError('waste_case_unknown')
    payload = json.loads(row['payload'])
    return row, payload['case']


def _contract(db, case_id, contract_id):
    c = db.execute('SELECT * FROM waste_contract WHERE case_id=? AND contract_id=?', (case_id, contract_id)).fetchone()
    if not c:
        raise ValueError('contract_unknown')
    return c


def _parents(db, case_id):
    return [dict(r) for r in db.execute(
        "SELECT * FROM material_batch WHERE case_id=? AND kind='sealed_parent' ORDER BY container", (case_id,))]


def register(db_path, case_id, fixture):
    """Freeze the completed work order, sealed containers and route contracts.

    Creates the on-site sealed parent material batches and immutable freeze
    events.  Re-importing or re-running the same fixture is idempotent and
    never creates duplicate material.
    """
    path = Path(fixture).resolve()
    raw = path.read_bytes()
    doc = json.loads(raw)
    if not case_id or not doc.get('work_order') or not doc.get('containers_file') or not doc.get('contracts'):
        raise ValueError('bad_waste_case')
    src = (path.parent / doc['containers_file']).resolve()
    source_raw = src.read_bytes()
    rows = list(csv.DictReader(source_raw.decode('utf-8-sig').splitlines()))
    if not rows:
        raise ValueError('waste_measurements_missing')
    required = {'container', 'gross_kg', 'contaminant_kg', 'seal'}
    if not required <= set(rows[0].keys()):
        raise ValueError('weighing_missing')
    seen = set()
    parsed = []
    for row in rows:
        cid = (row.get('container') or '').strip()
        seal = (row.get('seal') or '').strip()
        if not cid or not seal or not str(row.get('gross_kg', '')).strip() or not str(row.get('contaminant_kg', '')).strip():
            raise ValueError('weighing_missing')
        if cid in seen:
            raise ValueError('duplicate_container')
        seen.add(cid)
        gross = _mass(row['gross_kg'], 'invalid_waste_mass')
        contam = _mass(row['contaminant_kg'], 'invalid_waste_mass')
        if gross <= 0:
            raise ValueError('invalid_waste_mass')
        if not 0 <= contam <= gross:
            raise ValueError('invalid_waste_mass')
        batch_no = (row.get('inspection_batch') or '').strip() or f"{doc['work_order']}-IB-{cid}"
        parsed.append((cid, gross, contam, seal, batch_no))
    db = connect(db_path)
    try:
        with db:
            decision = db.execute('SELECT status,detail FROM decision WHERE work_order=?', (doc['work_order'],)).fetchone()
            if not decision or decision['status'] != '完成':
                raise ValueError('waste_order_not_complete')
            for cid, gross, contam, seal, batch_no in parsed:
                old = db.execute('SELECT work_order,gross_kg,contaminant_kg,seal FROM waste_container WHERE id=?', (cid,)).fetchone()
                expected = {'work_order': doc['work_order'], 'gross_kg': str(gross),
                            'contaminant_kg': str(contam), 'seal': seal}
                if old and dict(old) != expected:
                    raise ValueError('waste_container_conflict')
                db.execute('INSERT OR IGNORE INTO waste_container(id,work_order,gross_kg,contaminant_kg,seal) VALUES (?,?,?,?,?)',
                           (cid, doc['work_order'], str(gross), str(contam), seal))
                db.execute('UPDATE waste_container SET inspection_batch=? WHERE id=?', (batch_no, cid))
            for c in doc['contracts']:
                if c.get('id') not in ROUTES or not c.get('authorized_by'):
                    raise ValueError('bad_contract')
                cert_id = cert_until = license = license_until = manifest_id = None
                if c['id'] == 'closed_loop_sep':
                    cert_id = c.get('process_cert')
                    cert_until = c.get('cert_valid_until')
                    if not cert_id or not cert_until or not c.get('separation_rule'):
                        raise ValueError('bad_contract')
                    when(cert_until)
                else:
                    license = c.get('carrier_license')
                    manifest_id = c.get('manifest_id')
                    license_until = c.get('license_valid_until')
                    if not license or not manifest_id:
                        raise ValueError('bad_contract')
                    if license_until:
                        when(license_until)
                db.execute(
                    'INSERT OR IGNORE INTO waste_contract(case_id,contract_id,route,authorized_by,cert_id,cert_valid_until,'
                    'carrier_license,license_valid_until,manifest_id,payload) VALUES (?,?,?,?,?,?,?,?,?,?)',
                    (case_id, c['id'], c['id'], c['authorized_by'], cert_id, cert_until, license,
                     license_until, manifest_id, json.dumps(c, ensure_ascii=False, sort_keys=True)))
            source_sha = hashlib.sha256(source_raw).hexdigest()
            for cid, gross, contam, seal, batch_no in parsed:
                bid = f'{case_id}:P:{cid}'
                if not db.execute('SELECT 1 FROM material_batch WHERE id=?', (bid,)).fetchone():
                    db.execute(
                        'INSERT INTO material_batch(id,case_id,work_order,kind,parent_id,container,seal,inspection_batch,'
                        'gross_kg,contaminant_kg,remaining_kg,remaining_contaminant_kg,custody,hazard_class,source_sha256)'
                        ' VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                        (bid, case_id, doc['work_order'], 'sealed_parent', None, cid, seal, batch_no,
                         str(gross), str(contam), str(gross), str(contam), '现场封存', HAZARD_PARENT, source_sha))
                eid = _event(db, case_id, doc.get('inspection_at') or _now(), 'case_freeze', None, bid, cid,
                             gross, contam, {'seal': seal, 'inspection_batch': batch_no,
                                             'weighing_source_sha256': source_sha,
                                             'work_order': doc['work_order'], 'inspector': doc.get('inspector')})
                db.execute('UPDATE material_batch SET created_event_id=?,last_event_id=? WHERE id=?', (eid, eid, bid))
            digest = hashlib.sha256(raw + source_raw).hexdigest()
            payload = json.dumps({'fixture': str(path), 'case': doc, 'containers': rows,
                                  'source_sha256': source_sha, 'decision': dict(decision)},
                                 sort_keys=True, separators=(',', ':'))
            old = db.execute('SELECT digest FROM waste_case WHERE id=?', (case_id,)).fetchone()
            if old and old['digest'] != digest:
                raise ValueError('waste_case_conflict')
            db.execute('INSERT OR IGNORE INTO waste_case VALUES (?,?,?)', (case_id, payload, digest))
    finally:
        db.close()
    return {'case_id': case_id, 'digest': digest, 'containers': len(rows)}


def _verification(db, case_id, batch_id, kind, result, voucher_id, inspector, at, detail):
    db.execute('INSERT INTO waste_verification(case_id,batch_id,kind,result,inspector,at,voucher_id,detail)'
               ' VALUES (?,?,?,?,?,?,?,?)',
               (case_id, batch_id, kind, result, inspector, at, voucher_id,
                json.dumps(detail, ensure_ascii=False, sort_keys=True)))
    return _event(db, case_id, at, 'reinspection', None, batch_id,
                  db.execute('SELECT container FROM material_batch WHERE id=?', (batch_id,)).fetchone()[0],
                  Decimal('0'), Decimal('0'),
                  {'voucher_id': voucher_id, 'kind': kind, 'result': result, 'inspector': inspector, 'detail': detail})


def dispose(db_path, case_id, contract_id, at=None, verification=None):
    """Execute the chosen frozen route contract for every sealed parent.

    On-site separation atomically consumes the parent and emits children.
    Sealed shipment locks the route and opens a carrier manifest.  Any batch
    failing a trust check is held (``待核``) with an immutable hold event.
    """
    at = at or _now()
    db = connect(db_path)
    try:
        with db:
            _, doc = _load_case(db, case_id)
            contract = _contract(db, case_id, contract_id)
            parents = _parents(db, case_id)
            if not parents:
                raise ValueError('no_material_batch')
            holds, processed = [], []
            plan = []
            systemic = None
            if contract_id == 'closed_loop_sep':
                if when(contract['cert_valid_until']) < when(at):
                    systemic = 'cert_expired'
            else:
                if contract['license_valid_until'] and when(contract['license_valid_until']) < when(at):
                    systemic = 'carrier_license_expired'
            if systemic and any(p['custody'] not in ('待核',) for p in parents):
                for p in parents:
                    if p['custody'] != '待核':
                        _hold(db, case_id, p, contract_id, systemic, at)
                        holds.append({'container': p['container'], 'code': systemic})
                return {'case_id': case_id, 'contract_id': contract_id, 'route': contract_id,
                        'processed': [], 'holds': holds}
            wc = {r['id']: dict(r) for r in db.execute('SELECT * FROM waste_container')}
            for p in parents:
                if p['custody'] == '待核':
                    holds.append({'container': p['container'], 'code': 'already_pending_review'})
                    continue
                if p['custody'] in ('已交接承运方', '已消耗分离', '待运'):
                    systemic = 'route_conflict' if p['custody'] == '待运' else 'parent_already_transferred'
                    break
                ref = wc.get(p['container'])
                if not ref or ref['seal'] != p['seal']:
                    plan.append((p, 'seal_mismatch'))
                    continue
                if Decimal(ref['gross_kg']) != Decimal(p['gross_kg']) or Decimal(ref['contaminant_kg']) != Decimal(p['contaminant_kg']):
                    plan.append((p, 'weighing_mismatch'))
                    continue
                plan.append((p, None))
            if systemic:
                for p in parents:
                    if p['custody'] in ('现场封存', '待运'):
                        _hold(db, case_id, p, contract_id, systemic, at)
                        holds.append({'container': p['container'], 'code': systemic})
                    elif p['custody'] in ('已交接承运方', '已消耗分离'):
                        holds.append({'container': p['container'], 'code': 'parent_already_transferred'})
                    else:
                        holds.append({'container': p['container'], 'code': 'already_pending_review'})
                return {'case_id': case_id, 'contract_id': contract_id, 'route': contract_id,
                        'processed': [], 'holds': holds}
            if contract_id == 'closed_loop_sep':
                for p, err in plan:
                    if err:
                        _hold(db, case_id, p, contract_id, err, at)
                        holds.append({'container': p['container'], 'code': err})
                        continue
                    if verification and verification.get('result') == 'fail':
                        vid = f"{verification['voucher_id']}-{p['container']}"
                        vat = verification.get('at') or at
                        eid = _verification(db, case_id, p['id'], '现场复验', 'fail',
                                            vid, verification.get('inspector') or doc.get('inspector'), vat,
                                            verification.get('detail', {}))
                        _hold(db, case_id, p, contract_id, 'reinspection_failed', at,
                              {'voucher_id': vid, 'event_id': eid})
                        holds.append({'container': p['container'], 'code': 'reinspection_failed'})
                        continue
                    if verification and verification.get('result') == 'pass':
                        _verification(db, case_id, p['id'], '现场复验', 'pass',
                                      f"{verification['voucher_id']}-{p['container']}",
                                      verification.get('inspector') or doc.get('inspector'),
                                      verification.get('at') or at, verification.get('detail', {}))
                    g, c = Decimal(p['gross_kg']), Decimal(p['contaminant_kg'])
                    clean = _q(g - c)
                    eid = _event(db, case_id, at, 'separation', contract_id, p['id'], p['container'],
                                 -g, -c, {'cert_id': contract['cert_id'], 'rule': doc_contract_rule(doc, contract_id)})
                    db.execute("UPDATE material_batch SET custody='已消耗分离',remaining_kg='0.000',"
                               "remaining_contaminant_kg='0.000',last_event_id=? WHERE id=?", (eid, p['id']))
                    fid = f'{case_id}:F:{p["container"]}'
                    rid = f'{case_id}:R:{p["container"]}'
                    db.execute('INSERT INTO material_batch(id,case_id,work_order,kind,parent_id,container,seal,inspection_batch,'
                               'gross_kg,contaminant_kg,remaining_kg,remaining_contaminant_kg,custody,hazard_class,source_sha256,created_event_id,last_event_id)'
                               ' VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                               (fid, case_id, doc['work_order'], 'reuse_fluid', p['id'], p['container'], None,
                                p['inspection_batch'], str(clean), '0.000', str(clean), '0.000',
                                '现场封存', '非危废', p['source_sha256'], eid, eid))
                    db.execute('INSERT INTO material_batch(id,case_id,work_order,kind,parent_id,container,seal,inspection_batch,'
                               'gross_kg,contaminant_kg,remaining_kg,remaining_contaminant_kg,custody,hazard_class,source_sha256,created_event_id,last_event_id)'
                               ' VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                               (rid, case_id, doc['work_order'], 'hazard_residue', p['id'], p['container'], None,
                                p['inspection_batch'], str(c), str(c), str(c), str(c),
                                '现场封存', HAZARD_PARENT, p['source_sha256'], eid, eid))
                    fe = _event(db, case_id, at, 'separation', contract_id, fid, p['container'],
                                clean, Decimal('0'), {'role': 'reuse_fluid', 'parent_id': p['id']})
                    re = _event(db, case_id, at, 'separation', contract_id, rid, p['container'],
                                c, c, {'role': 'hazard_residue', 'parent_id': p['id']})
                    db.execute('UPDATE material_batch SET created_event_id=?,last_event_id=? WHERE id=?', (fe, fe, fid))
                    db.execute('UPDATE material_batch SET created_event_id=?,last_event_id=? WHERE id=?', (re, re, rid))
                    voucher = f'{case_id}-SEP-{p["container"]}'
                    db.execute('INSERT INTO waste_verification(case_id,batch_id,kind,result,inspector,at,voucher_id,detail)'
                               ' VALUES (?,?,?,?,?,?,?,?)',
                               (case_id, fid, '分离复验', 'pass', doc.get('inspector'), at, voucher,
                                json.dumps({'fluid_batch': fid, 'residue_batch': rid, 'cert_id': contract['cert_id'],
                                            'clean_fluid_contaminant_kg': '0.000'}, ensure_ascii=False, sort_keys=True)))
                    processed.append({'container': p['container'], 'reuse_fluid_kg': str(clean),
                                      'hazard_residue_kg': str(c), 'fluid_batch': fid, 'residue_batch': rid})
            else:
                manifest = db.execute('SELECT * FROM waste_manifest WHERE case_id=? AND manifest_id=?',
                                      (case_id, contract['manifest_id'])).fetchone()
                if not manifest:
                    cur = db.execute('INSERT INTO waste_manifest(case_id,manifest_id,carrier_license,status,detail)'
                                     ' VALUES (?,?,?,?,?)',
                                     (case_id, contract['manifest_id'], contract['carrier_license'], '待签收',
                                      json.dumps({'contract_id': contract_id}, ensure_ascii=False, sort_keys=True)))
                    manifest_id = cur.lastrowid
                else:
                    manifest_id = manifest['id']
                for p, err in plan:
                    if err:
                        _hold(db, case_id, p, contract_id, err, at)
                        holds.append({'container': p['container'], 'code': err})
                        continue
                    eid = _event(db, case_id, at, 'route_lock', contract_id, p['id'], p['container'],
                                 Decimal('0'), Decimal('0'),
                                 {'manifest_id': contract['manifest_id'], 'sealed_identity_retained': True})
                    db.execute("UPDATE material_batch SET custody='待运',last_event_id=? WHERE id=?", (eid, p['id']))
                    if not db.execute('SELECT 1 FROM manifest_line WHERE manifest_id=? AND container=?',
                                      (manifest_id, p['container'])).fetchone():
                        db.execute('INSERT INTO manifest_line(manifest_id,container,batch_id,seal,sealed_gross_kg,unsigned_kg,status)'
                                   ' VALUES (?,?,?,?,?,?,?)',
                                   (manifest_id, p['container'], p['id'], p['seal'], p['gross_kg'],
                                    p['remaining_kg'], '待签收'))
                    processed.append({'container': p['container'], 'sealed_gross_kg': p['gross_kg'],
                                      'manifest_id': contract['manifest_id']})
    finally:
        db.close()
    return {'case_id': case_id, 'contract_id': contract_id, 'route': contract_id,
            'processed': processed, 'holds': holds}


def doc_contract_rule(doc, contract_id):
    for c in doc['contracts']:
        if c['id'] == contract_id:
            return c.get('separation_rule')
    return None


def _resolve_receipt(doc, receipt):
    r = receipt
    if r is None:
        for c in doc['contracts']:
            if c['id'] == 'sealed_ship' and c.get('receipt'):
                r = c['receipt']
                break
    if not r or not r.get('id') or not r.get('carrier') or not r.get('signed_by') or not r.get('at'):
        raise ValueError('receipt_missing')
    when(r['at'])
    return r


def sign_receipt(db_path, case_id, receipt=None, at=None):
    """Sign carrier receipt against the sealed-ship manifest.

    Full, partial and rejected sign-off keep the unsigned mass in real
    on-site custody (``待运``); only the signed mass leaves the books.
    Replaying the same receipt is idempotent.
    """
    at = at or _now()
    db = connect(db_path)
    try:
        with db:
            _, doc = _load_case(db, case_id)
            r = _resolve_receipt(doc, receipt)
            manifest = db.execute('SELECT * FROM waste_manifest WHERE case_id=? ORDER BY id', (case_id,)).fetchone()
            if not manifest:
                raise ValueError('manifest_missing')
            if manifest['carrier_license'] != r['carrier']:
                raise ValueError('carrier_mismatch')
            lines = [dict(x) for x in db.execute('SELECT * FROM manifest_line WHERE manifest_id=? ORDER BY container',
                                                 (manifest['id'],))]
            # Idempotency: a receipt whose booking was already fully applied is
            # replayed as-is even if later receipts have since updated the lines.
            applied = {json.loads(e['detail']).get('receipt_id')
                       for e in db.execute("SELECT detail FROM material_event WHERE case_id=? AND event_type='shipment_signoff'",
                                           (case_id,))}
            if r['id'] in applied:
                return {'case_id': case_id, 'receipt_id': r['id'], 'replayed': True, 'lines': lines}
            if 'lines' in r:
                unknown = {x['container'] for x in r['lines']} - {l['container'] for l in lines}
                if unknown:
                    raise ValueError('unknown_container')
                accepted = {x['container']: _mass(x['accepted_kg'], 'invalid_waste_mass') for x in r['lines']}
            else:
                listed = set(r.get('accepted_containers') or [])
                unknown = listed - {l['container'] for l in lines}
                if unknown:
                    raise ValueError('unknown_container')
                total = _mass(r['accepted_kg'], 'invalid_waste_mass')
                expected = sum((Decimal(l['sealed_gross_kg']) for l in lines if l['container'] in listed), Decimal('0'))
                if _q(expected) != total:
                    raise ValueError('receipt_mass_mismatch')
                accepted = {l['container']: (Decimal(l['sealed_gross_kg']) if l['container'] in listed else Decimal('0'))
                            for l in lines}
            results = []
            for l in lines:
                kg = _q(accepted.get(l['container'], Decimal('0')))
                g = Decimal(l['sealed_gross_kg'])
                batch = db.execute('SELECT * FROM material_batch WHERE id=?', (l['batch_id'],)).fetchone()
                available = Decimal(batch['remaining_kg'])
                if kg > available:
                    raise ValueError('signed_mass_exceeds')
                if kg == 0:
                    if not l['receipt_id']:
                        eid = _event(db, case_id, r['at'], 'shipment_signoff', 'sealed_ship', batch['id'],
                                     l['container'], Decimal('0'), Decimal('0'),
                                     {'receipt_id': r['id'], 'manifest_id': manifest['manifest_id'],
                                      'signed_by': r['signed_by'], 'accepted_kg': '0.000', 'rejected': True})
                        db.execute("UPDATE manifest_line SET status='拒收',receipt_id=?,signed_by=?,signed_at=? WHERE id=?",
                                   (r['id'], r['signed_by'], r['at'], l['id']))
                        results.append({'container': l['container'], 'status': '拒收', 'signed_kg': '0.000',
                                        'unsigned_kg': l['sealed_gross_kg'], 'event_id': eid})
                    else:
                        results.append({'container': l['container'], 'status': l['status'],
                                        'signed_kg': l['signed_kg'], 'unsigned_kg': l['unsigned_kg']})
                    continue
                # contaminant leaves proportionally with the mass signed in this receipt
                g_total = Decimal(batch['gross_kg'])
                c_total = Decimal(batch['contaminant_kg'])
                signed_c = _q(c_total * kg / g_total)
                remain_g = _q(available - kg)
                remain_c = _q(Decimal(batch['remaining_contaminant_kg']) - signed_c)
                eid = _event(db, case_id, r['at'], 'shipment_signoff', 'sealed_ship', batch['id'],
                             l['container'], -kg, -signed_c,
                             {'receipt_id': r['id'], 'manifest_id': manifest['manifest_id'],
                              'signed_by': r['signed_by'], 'accepted_kg': str(kg),
                              'signed_contaminant_kg': str(signed_c)})
                custody = '已交接承运方' if remain_g == 0 else '待运'
                db.execute('UPDATE material_batch SET remaining_kg=?,remaining_contaminant_kg=?,custody=?,last_event_id=? WHERE id=?',
                           (str(remain_g), str(remain_c), custody, eid, batch['id']))
                line_signed = _q(Decimal(l['signed_kg']) + kg)
                status = '已签收' if remain_g == 0 else '部分签收'
                db.execute('UPDATE manifest_line SET signed_kg=?,unsigned_kg=?,signed_contaminant_kg=?,status=?,receipt_id=?,signed_by=?,signed_at=? WHERE id=?',
                           (str(line_signed), str(remain_g),
                            str(_q(Decimal(l['signed_contaminant_kg']) + signed_c)),
                            status, r['id'], r['signed_by'], r['at'], l['id']))
                results.append({'container': l['container'], 'status': status, 'signed_kg': str(line_signed),
                                'unsigned_kg': str(remain_g), 'event_id': eid})
            statuses = {x['status'] for x in db.execute('SELECT status FROM manifest_line WHERE manifest_id=?', (manifest['id'],))}
            mstatus = '完全签收' if statuses == {'已签收'} else ('待签收' if statuses <= {'待签收', '拒收'} else '部分签收')
            db.execute('UPDATE waste_manifest SET status=? WHERE id=?', (mstatus, manifest['id']))
    finally:
        db.close()
    return {'case_id': case_id, 'receipt_id': r['id'], 'replayed': False, 'lines': results}


def reinspect(db_path, case_id, batch_id, result, voucher_id, inspector, at=None, detail=None):
    """Append a re-inspection voucher; passes may release a hold, never mass."""
    if result not in ('pass', 'fail'):
        raise ValueError('bad_verification_result')
    at = at or _now()
    db = connect(db_path)
    try:
        with db:
            _load_case(db, case_id)
            batch = db.execute('SELECT * FROM material_batch WHERE id=? AND case_id=?', (batch_id, case_id)).fetchone()
            if not batch:
                raise ValueError('batch_unknown')
            if batch['custody'] == '已交接承运方':
                raise ValueError('parent_already_transferred')
            eid = _verification(db, case_id, batch_id, '现场复验', result, voucher_id, inspector, at, detail or {})
            if result == 'fail':
                db.execute("UPDATE material_batch SET custody='待核',last_event_id=? WHERE id=?", (eid, batch_id))
            elif batch['custody'] == '待核':
                db.execute("UPDATE material_batch SET custody='现场封存',last_event_id=? WHERE id=?", (eid, batch_id))
    finally:
        db.close()
    return {'case_id': case_id, 'batch_id': batch_id, 'voucher_id': voucher_id, 'result': result, 'event_id': eid}


def carrier_return(db_path, case_id, container, kg, voucher_id, signed_by, at=None, reason=None):
    """Carrier returns signed material. Old sign-off events stay; mass returns
    to ``待核`` custody, never silently back into on-site stock."""
    at = at or _now()
    kg = _mass(kg, 'invalid_waste_mass')
    db = connect(db_path)
    try:
        with db:
            _load_case(db, case_id)
            batch = db.execute("SELECT * FROM material_batch WHERE case_id=? AND container=? AND kind='sealed_parent'",
                               (case_id, container)).fetchone()
            if not batch:
                raise ValueError('batch_unknown')
            g, c = Decimal(batch['gross_kg']), Decimal(batch['contaminant_kg'])
            signed_out = _q(g - Decimal(batch['remaining_kg']))
            if kg <= 0 or kg > signed_out:
                raise ValueError('return_mass_exceeds')
            returned_c = _q(c * kg / g)
            new_remain = _q(Decimal(batch['remaining_kg']) + kg)
            new_remain_c = _q(Decimal(batch['remaining_contaminant_kg']) + returned_c)
            eid = _event(db, case_id, at, 'carrier_return', 'sealed_ship', batch['id'], container, kg, returned_c,
                         {'voucher_id': voucher_id, 'signed_by': signed_by, 'returned_kg': str(kg),
                          'returned_contaminant_kg': str(returned_c), 'reason': reason or 'carrier_return'})
            db.execute('UPDATE material_batch SET remaining_kg=?,remaining_contaminant_kg=?,custody=?,last_event_id=? WHERE id=?',
                       (str(new_remain), str(new_remain_c), '待核', eid, batch['id']))
            line = db.execute('SELECT ml.* FROM manifest_line ml JOIN waste_manifest wm ON ml.manifest_id=wm.id'
                              ' WHERE wm.case_id=? AND ml.container=?', (case_id, container)).fetchone()
            if line:
                db.execute("UPDATE manifest_line SET status='回退' WHERE id=?", (line['id'],))
                db.execute("UPDATE waste_manifest SET status='回退待核' WHERE id=?", (line['manifest_id'],))
            db.execute('INSERT INTO waste_verification(case_id,batch_id,kind,result,inspector,at,voucher_id,detail)'
                       ' VALUES (?,?,?,?,?,?,?,?)',
                       (case_id, batch['id'], '现场复验', 'fail', signed_by, at, voucher_id,
                        json.dumps({'carrier_return': True, 'kg': str(kg), 'reason': reason}, ensure_ascii=False, sort_keys=True)))
    finally:
        db.close()
    return {'case_id': case_id, 'container': container, 'returned_kg': str(kg), 'custody': '待核', 'event_id': eid}


def _batch_map(db, case_id):
    return [dict(r) for r in db.execute('SELECT * FROM material_batch WHERE case_id=? ORDER BY id', (case_id,))]


def conservation_report(db_path, case_id):
    """Full material and manifest report with decimal conservation checks."""
    db = connect(db_path)
    try:
        case, doc = _load_case(db, case_id)
        batches = _batch_map(db, case_id)
        events = [dict(r) for r in db.execute('SELECT * FROM material_event WHERE case_id=? ORDER BY id', (case_id,))]
        contracts = [dict(r) for r in db.execute('SELECT * FROM waste_contract WHERE case_id=? ORDER BY contract_id', (case_id,))]
        verifs = [dict(r) for r in db.execute('SELECT * FROM waste_verification WHERE case_id=? ORDER BY id', (case_id,))]
        manifests = [dict(r) for r in db.execute('SELECT * FROM waste_manifest WHERE case_id=? ORDER BY id', (case_id,))]
        for m in manifests:
            m['lines'] = [dict(r) for r in db.execute('SELECT * FROM manifest_line WHERE manifest_id=? ORDER BY container', (m['id'],))]
        checks = []
        containers = []
        by_container = {}
        for b in batches:
            by_container.setdefault(b['container'], []).append(b)
        for cid in sorted(by_container):
            bs = by_container[cid]
            parent = next(b for b in bs if b['kind'] == 'sealed_parent')
            g, c = Decimal(parent['gross_kg']), Decimal(parent['contaminant_kg'])
            ev = [e for e in events if e['container'] == cid]
            d_gross = sum((Decimal(e['gross_delta']) for e in ev), Decimal('0'))
            d_contam = sum((Decimal(e['contaminant_delta']) for e in ev), Decimal('0'))
            book_gross = sum((Decimal(b['remaining_kg']) for b in bs), Decimal('0'))
            book_contam = sum((Decimal(b['remaining_contaminant_kg']) for b in bs), Decimal('0'))
            fluids = [b for b in bs if b['kind'] == 'reuse_fluid']
            clean_ok = all(Decimal(b['contaminant_kg']) == 0 and Decimal(b['remaining_contaminant_kg']) == 0 for b in fluids)
            children = [b for b in bs if b['parent_id'] == parent['id']]
            child_gross = sum((Decimal(b['gross_kg']) for b in children), Decimal('0'))
            structure_ok = parent['custody'] != '已消耗分离' or (
                len(children) == 2 and _q(child_gross) == g and
                abs(sum((Decimal(b['contaminant_kg']) for b in children), Decimal('0')) - c) == 0)
            ok = _q(d_gross) == _q(book_gross) and _q(d_contam) == _q(book_contam) and clean_ok and structure_ok
            checks.append({'container': cid, 'mass_balanced': _q(d_gross) == _q(book_gross),
                           'contaminant_balanced': _q(d_contam) == _q(book_contam),
                           'clean_fluid_pure': clean_ok, 'parent_child_structure': structure_ok})
            containers.append({'container': cid, 'seal': parent['seal'], 'inspection_batch': parent['inspection_batch'],
                               'gross_kg': str(g), 'contaminant_kg': str(c), 'custody': parent['custody'],
                               'remaining_kg': parent['remaining_kg'], 'batches': bs, 'events': ev,
                               'conservation_ok': ok})
        reusable = sum((Decimal(b['remaining_kg']) for b in batches if b['kind'] == 'reuse_fluid'), Decimal('0'))
        residue_on_site = sum((Decimal(b['remaining_kg']) for b in batches if b['kind'] == 'hazard_residue'), Decimal('0'))
        sealed_on_site = sum((Decimal(b['remaining_kg']) for b in batches if b['kind'] == 'sealed_parent'), Decimal('0'))
        shipped = -sum((Decimal(e['gross_delta']) for e in events if e['event_type'] == 'shipment_signoff'
                        and Decimal(e['gross_delta']) < 0), Decimal('0'))
        returned = sum((Decimal(e['gross_delta']) for e in events if e['event_type'] == 'carrier_return'), Decimal('0'))
        shipped_net = _q(shipped - returned)
        totals = {'original_gross_kg': str(sum((Decimal(b['gross_kg']) for b in batches if b['kind'] == 'sealed_parent'), Decimal('0'))),
                  'original_contaminant_kg': str(sum((Decimal(b['contaminant_kg']) for b in batches if b['kind'] == 'sealed_parent'), Decimal('0'))),
                  'reusable_clean_kg': str(_q(reusable)),
                  'hazard_residue_on_site_kg': str(_q(residue_on_site)),
                  'sealed_hazard_on_site_kg': str(_q(sealed_on_site)),
                  'hazard_shipped_net_kg': str(shipped_net),
                  'pending_review_containers': [c['container'] for c in containers if c['custody'] == '待核']}
        balanced = all(x['mass_balanced'] and x['contaminant_balanced'] and x['clean_fluid_pure']
                       and x['parent_child_structure'] for x in checks)
        return {'case_id': case_id, 'work_order': doc['work_order'], 'digest': case['digest'],
                'inspection_at': doc.get('inspection_at'), 'inspector': doc.get('inspector'),
                'contracts': contracts, 'containers': containers, 'totals': totals,
                'manifests': manifests, 'verifications': verifs, 'events': events,
                'conservation': {'balanced': balanced, 'checks': checks}}
    finally:
        db.close()


def write_reports(db_path, case_id, out_path):
    """Write the JSON material report and the sibling manifest (联单) report."""
    report = conservation_report(db_path, case_id)
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    manifest_path = out.with_name(out.stem + '.manifest.json')
    manifest_path.write_text(json.dumps({'case_id': case_id, 'manifests': report['manifests']},
                                        ensure_ascii=False, indent=2), encoding='utf-8')
    return out, manifest_path
