import argparse,json,tempfile
from pathlib import Path
from .store import connect
from .ingest import ingest
from .rules import evaluate
from .report import report,audience_reports
from .technician_privacy import register,review_publish,recover,INTERNAL_AUDIENCE
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
    g=s.add_parser('register-privacy');g.add_argument('--db',required=True);g.add_argument('--case-id',required=True);g.add_argument('--fixture',required=True)
    v=s.add_parser('review');v.add_argument('--db',required=True);v.add_argument('--fixture',required=True)
    v.add_argument('--audience',required=True);v.add_argument('--out-dir',required=True);v.add_argument('--contract');v.add_argument('--report-id')
    m=s.add_parser('materialize');m.add_argument('--db',required=True);m.add_argument('--out-dir',required=True)
    c=s.add_parser('recover');c.add_argument('--db',required=True);c.add_argument('--out-dir',required=True);c.add_argument('--audience')
    l=s.add_parser('ledger');l.add_argument('--db',required=True);l.add_argument('--audience')
    x=s.add_parser('inspect');x.add_argument('--db',required=True)
    q=p.parse_args()
    if q.command=='demo':print(demo(q.workdir))
    elif q.command=='init':initialize(q.db,q.config)
    elif q.command=='ingest':print(ingest(q.file,q.db))
    elif q.command=='evaluate':print(evaluate(q.db))
    elif q.command=='report':print(report(q.db,q.out))
    elif q.command=='register-privacy':print(register(q.db,q.case_id,q.fixture))
    elif q.command=='review':print(json.dumps(review_publish(q.db,q.fixture,q.audience,q.out_dir,q.contract,q.report_id),ensure_ascii=False))
    elif q.command=='materialize':print(json.dumps(audience_reports(q.db,q.out_dir),ensure_ascii=False))
    elif q.command=='recover':print(json.dumps(recover(q.db,q.out_dir,q.audience),ensure_ascii=False))
    elif q.command=='ledger':
        db=connect(q.db)
        sql='SELECT id,report_id,audience,contract_id,status,reason,reading_count,report_digest,json_path,md_path,published_at FROM release_ledger'
        args=()
        if q.audience:sql+=' WHERE audience=?';args=(q.audience,)
        print(json.dumps([dict(z) for z in db.execute(sql+' ORDER BY id',args)],ensure_ascii=False,indent=2));db.close()
    elif q.command=='inspect':
        db=connect(q.db);print(json.dumps([dict(z) for z in db.execute('SELECT * FROM decision')],ensure_ascii=False));db.close()
if __name__=='__main__':main()
