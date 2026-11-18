import hashlib,json
from pathlib import Path
from .store import connect,when
from .safety_permit import gate_start
KINDS={'start','step','reading','replace','pause','resume','void','sign'}
def ingest(path,db_path):
    raw=Path(path).read_bytes();h=hashlib.sha256(raw).hexdigest();db=connect(db_path)
    if db.execute('SELECT 1 FROM source WHERE sha256=?',(h,)).fetchone():db.close();return {'imported':0,'duplicate_file':True,'starts':[]}
    count=0;starts=[]
    with db:
        sid=db.execute('INSERT INTO source(name,sha256,raw) VALUES (?,?,?)',(str(path),h,raw)).lastrowid
        for line,text in enumerate(raw.decode('utf-8-sig').splitlines(),1):
            try:
                r=json.loads(text);kind=r['kind'];device=str(r['device']);seq=int(r['seq']);order=str(r['work_order']);when(r['at'])
                if kind not in KINDS:raise ValueError('unknown_kind')
                digest=hashlib.sha256(json.dumps(r,sort_keys=True,separators=(',',':')).encode()).hexdigest();old=db.execute('SELECT sha256 FROM event WHERE device=? AND seq=?',(device,seq)).fetchone()
                if old:
                    if old['sha256']!=digest:db.execute('INSERT INTO issue(code,work_order,detail) VALUES (?,?,?)',('identity_conflict',order,f'{device}:{seq}'))
                    continue
                if not db.execute('SELECT 1 FROM work_order WHERE id=?',(order,)).fetchone():
                    db.execute('INSERT INTO issue(code,work_order,detail) VALUES (?,?,?)',('unknown_order',order,f'line {line}'));continue
                eid=db.execute('INSERT INTO event(source_id,line_no,device,seq,work_order,kind,at,payload,sha256) VALUES (?,?,?,?,?,?,?,?,?)',(sid,line,device,seq,order,kind,r['at'],text,digest)).lastrowid;db.execute('INSERT INTO evidence(event_id,source_sha256,payload_sha256,role) VALUES (?,?,?,?)',(eid,h,digest,'maintenance_event'))
                if kind=='start':
                    st=gate_start(db,eid,order,r['at'])
                    if st:starts.append({'event_id':eid,'work_order':order,'status':st})
                count+=1
            except (ValueError,KeyError,TypeError,json.JSONDecodeError) as e:
                db.execute('INSERT INTO issue(code,work_order,detail) VALUES (?,?,?)',('parse_error',str(line),str(e)))
    db.close();return {'imported':count,'duplicate_file':False,'starts':starts}
