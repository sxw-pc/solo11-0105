import argparse,json,tempfile
from pathlib import Path
from .store import connect
from .ingest import ingest
from .rules import evaluate
from .report import report
from .warranty_billing import register,settle,settlement_report
def initialize(db_path,config):
    c=json.loads(Path(config).read_text(encoding='utf-8'));db=connect(db_path)
    with db:
        for table,cols in [('work_order',['id','equipment','procedure_version','status']),('step',['work_order','position','code','required']),('instrument',['id','unit','min','max','calibrated_until']),('stock',['part','issued_to'])]:
            for row in c[table]:db.execute(f'INSERT OR IGNORE INTO {table} VALUES ({",".join("?" for _ in cols)})',[row[k] for k in cols])
    db.close()
def demo(workdir=None):
    root=Path(workdir or tempfile.mkdtemp(prefix='maintlog-'));root.mkdir(parents=True,exist_ok=True);fix=Path(__file__).resolve().parent.parent/'fixtures';db=root/'maintlog.db'
    initialize(db,fix/'config.json');ingest(fix/'events.jsonl',db);evaluate(db);return report(db,root/'report.json')
def _settle_summary(rev):
    return {k:rev[k] for k in ('id','work_order','contract_id','revision_no','case_id','status',
                               'invoice_id','gross_total_cents','customer_due_cents','insurer_due_cents','reasons')}
def main():
    p=argparse.ArgumentParser(description='离线设备保养采录');s=p.add_subparsers(dest='command',required=True)
    d=s.add_parser('demo');d.add_argument('--workdir')
    i=s.add_parser('init');i.add_argument('--db',required=True);i.add_argument('--config',required=True)
    a=s.add_parser('ingest');a.add_argument('file');a.add_argument('--db',required=True)
    e=s.add_parser('evaluate');e.add_argument('--db',required=True)
    r=s.add_parser('report');r.add_argument('--db',required=True);r.add_argument('--out',required=True)
    x=s.add_parser('inspect');x.add_argument('--db',required=True)
    g=s.add_parser('register',help='冻结已完成工单证据与已发行发票/保修合同');g.add_argument('--db',required=True);g.add_argument('--case',required=True);g.add_argument('--fixture',required=True)
    b=s.add_parser('settle',help='按保修合同离线出账，生成/冲回结算 revision');b.add_argument('--db',required=True);b.add_argument('--case',required=True);b.add_argument('--contract',required=True)
    sr=s.add_parser('settlement-report',help='导出 JSON 结算/补差报告');sr.add_argument('--db',required=True);sr.add_argument('--out',required=True);sr.add_argument('--case');sr.add_argument('--work-order');sr.add_argument('--contract')
    q=p.parse_args()
    if q.command=='demo':print(demo(q.workdir))
    elif q.command=='init':initialize(q.db,q.config)
    elif q.command=='ingest':print(ingest(q.file,q.db))
    elif q.command=='evaluate':print(evaluate(q.db))
    elif q.command=='report':print(report(q.db,q.out))
    elif q.command=='register':print(register(q.db,q.case,q.fixture))
    elif q.command=='settle':print(json.dumps(_settle_summary(settle(q.db,q.case,q.contract)),ensure_ascii=False))
    elif q.command=='settlement-report':print(settlement_report(q.db,q.out,case_id=q.case,work_order=q.work_order,contract_id=q.contract))
    elif q.command=='inspect':
        db=connect(q.db);print(json.dumps([dict(z) for z in db.execute('SELECT * FROM decision')],ensure_ascii=False));db.close()
if __name__=='__main__':main()
