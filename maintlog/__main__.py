import argparse,json,tempfile
from pathlib import Path
from .store import connect
from .ingest import ingest
from .rules import evaluate
from .report import report
from . import waste_disposition as wd
def initialize(db_path,config):
    c=json.loads(Path(config).read_text(encoding='utf-8'));db=connect(db_path)
    with db:
        for table,cols in [('work_order',['id','equipment','procedure_version','status']),('step',['work_order','position','code','required']),('instrument',['id','unit','min','max','calibrated_until']),('stock',['part','issued_to'])]:
            for row in c[table]:db.execute(f'INSERT OR IGNORE INTO {table} VALUES ({",".join("?" for _ in cols)})',[row[k] for k in cols])
    db.close()
def demo(workdir=None):
    root=Path(workdir or tempfile.mkdtemp(prefix='maintlog-'));root.mkdir(parents=True,exist_ok=True);fix=Path(__file__).resolve().parent.parent/'fixtures';db=root/'maintlog.db'
    initialize(db,fix/'config.json');ingest(fix/'events.jsonl',db);evaluate(db);return report(db,root/'report.json')
def _j(v):
    return json.loads(Path(v).read_text(encoding='utf-8')) if v and Path(v).exists() else json.loads(v)
def main():
    p=argparse.ArgumentParser(description='离线设备保养采录');s=p.add_subparsers(dest='command',required=True)
    d=s.add_parser('demo');d.add_argument('--workdir')
    i=s.add_parser('init');i.add_argument('--db',required=True);i.add_argument('--config',required=True)
    a=s.add_parser('ingest');a.add_argument('file');a.add_argument('--db',required=True)
    e=s.add_parser('evaluate');e.add_argument('--db',required=True)
    r=s.add_parser('report');r.add_argument('--db',required=True);r.add_argument('--out',required=True)
    x=s.add_parser('inspect');x.add_argument('--db',required=True)
    wr=s.add_parser('waste-register',help='冻结完成工单、封条容器与路径合同');wr.add_argument('--db',required=True);wr.add_argument('--case',required=True);wr.add_argument('--fixture',required=True)
    wd2=s.add_parser('waste-dispose',help='按所选合同执行现场分离或原封外运锁定');wd2.add_argument('--db',required=True);wd2.add_argument('--case',required=True);wd2.add_argument('--contract',required=True,choices=wd.ROUTES);wd2.add_argument('--at');wd2.add_argument('--verification',help='JSON 字符串或文件：现场复验凭证')
    ws=s.add_parser('waste-sign',help='承运方签收（完整/部分/拒收）');ws.add_argument('--db',required=True);ws.add_argument('--case',required=True);ws.add_argument('--receipt',help='JSON 字符串或文件；缺省使用冻结合同内置签收凭证')
    wt=s.add_parser('waste-retest',help='追加后补复验凭证，不删除旧实物事件');wt.add_argument('--db',required=True);wt.add_argument('--case',required=True);wt.add_argument('--batch',required=True);wt.add_argument('--result',required=True,choices=['pass','fail']);wt.add_argument('--voucher',required=True);wt.add_argument('--inspector',required=True);wt.add_argument('--at');wt.add_argument('--detail')
    wk=s.add_parser('waste-return',help='承运方回退已签收物料，转入待核保管');wk.add_argument('--db',required=True);wk.add_argument('--case',required=True);wk.add_argument('--container',required=True);wk.add_argument('--kg',required=True);wk.add_argument('--voucher',required=True);wk.add_argument('--signed-by',required=True);wk.add_argument('--at');wk.add_argument('--reason')
    wo=s.add_parser('waste-report',help='输出 JSON 物料账与联单报告并做守恒校验');wo.add_argument('--db',required=True);wo.add_argument('--case',required=True);wo.add_argument('--out',required=True)
    q=p.parse_args()
    if q.command=='demo':print(demo(q.workdir))
    elif q.command=='init':initialize(q.db,q.config)
    elif q.command=='ingest':print(ingest(q.file,q.db))
    elif q.command=='evaluate':print(evaluate(q.db))
    elif q.command=='report':print(report(q.db,q.out))
    elif q.command=='inspect':
        db=connect(q.db);print(json.dumps([dict(z) for z in db.execute('SELECT * FROM decision')],ensure_ascii=False));db.close()
    elif q.command=='waste-register':
        print(json.dumps(wd.register(q.db,q.case,q.fixture),ensure_ascii=False))
    elif q.command=='waste-dispose':
        print(json.dumps(wd.dispose(q.db,q.case,q.contract,at=q.at,verification=_j(q.verification) if q.verification else None),ensure_ascii=False))
    elif q.command=='waste-sign':
        print(json.dumps(wd.sign_receipt(q.db,q.case,receipt=_j(q.receipt) if q.receipt else None),ensure_ascii=False))
    elif q.command=='waste-retest':
        print(json.dumps(wd.reinspect(q.db,q.case,q.batch,q.result,q.voucher,q.inspector,at=q.at,detail=_j(q.detail) if q.detail else None),ensure_ascii=False))
    elif q.command=='waste-return':
        print(json.dumps(wd.carrier_return(q.db,q.case,q.container,q.kg,q.voucher,q.signed_by,at=q.at,reason=q.reason),ensure_ascii=False))
    elif q.command=='waste-report':
        paths=wd.write_reports(q.db,q.case,q.out);print(json.dumps({'material_report':str(paths[0]),'manifest_report':str(paths[1])},ensure_ascii=False))
if __name__=='__main__':main()
