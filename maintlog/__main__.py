import argparse,json,tempfile
from pathlib import Path
from .store import connect
from .ingest import ingest
from .rules import evaluate
from .report import report
from .safety_permit import register as permit_register,grant as permit_grant,revoke as permit_revoke,permits as permit_list
def initialize(db_path,config):
    c=json.loads(Path(config).read_text(encoding='utf-8'));db=connect(db_path)
    with db:
        for table,cols in [('work_order',['id','equipment','procedure_version','status']),('step',['work_order','position','code','required']),('instrument',['id','unit','min','max','calibrated_until']),('stock',['part','issued_to'])]:
            for row in c[table]:db.execute(f'INSERT OR IGNORE INTO {table} VALUES ({",".join("?" for _ in cols)})',[row[k] for k in cols])
    db.close()
def demo(workdir=None):
    root=Path(workdir or tempfile.mkdtemp(prefix='maintlog-'));root.mkdir(parents=True,exist_ok=True);fix=Path(__file__).resolve().parent.parent/'fixtures';db=root/'maintlog.db'
    initialize(db,fix/'config.json');ingest(fix/'events.jsonl',db);evaluate(db);return report(db,root/'report.json')
def main():
    p=argparse.ArgumentParser(description='离线设备保养采录');s=p.add_subparsers(dest='command',required=True)
    d=s.add_parser('demo');d.add_argument('--workdir')
    i=s.add_parser('init');i.add_argument('--db',required=True);i.add_argument('--config',required=True)
    a=s.add_parser('ingest');a.add_argument('file');a.add_argument('--db',required=True)
    e=s.add_parser('evaluate');e.add_argument('--db',required=True)
    r=s.add_parser('report');r.add_argument('--db',required=True);r.add_argument('--out',required=True)
    x=s.add_parser('inspect');x.add_argument('--db',required=True)
    g=s.add_parser('register');g.add_argument('--db',required=True);g.add_argument('--case',required=True);g.add_argument('--fixture',required=True)
    m=s.add_parser('permit');m.add_argument('--db',required=True);m.add_argument('--case',required=True);m.add_argument('--contract',required=True);m.add_argument('--at',required=True)
    v=s.add_parser('revoke');v.add_argument('--db',required=True);v.add_argument('--grant',required=True)
    ps=s.add_parser('permits');ps.add_argument('--db',required=True)
    q=p.parse_args()
    if q.command=='demo':print(demo(q.workdir))
    elif q.command=='init':initialize(q.db,q.config)
    elif q.command=='ingest':print(ingest(q.file,q.db))
    elif q.command=='evaluate':print(evaluate(q.db))
    elif q.command=='report':print(report(q.db,q.out))
    elif q.command=='register':print(json.dumps(permit_register(q.db,q.case,q.fixture),ensure_ascii=False))
    elif q.command=='permit':print(json.dumps(permit_grant(q.db,q.case,q.contract,q.at),ensure_ascii=False))
    elif q.command=='revoke':print(json.dumps(permit_revoke(q.db,q.grant),ensure_ascii=False))
    elif q.command=='permits':print(json.dumps(permit_list(q.db),ensure_ascii=False,indent=2))
    elif q.command=='inspect':
        db=connect(q.db);print(json.dumps([dict(z) for z in db.execute('SELECT * FROM decision')],ensure_ascii=False));db.close()
if __name__=='__main__':main()
