import argparse,json,tempfile
from pathlib import Path
from .store import connect
from .ingest import ingest
from .rules import evaluate
from .report import report
def initialize(db_path,config):
    c=json.loads(Path(config).read_text(encoding='utf-8'));db=connect(db_path)
    with db:
        for row in c.get('work_order',[]):
            db.execute('INSERT OR IGNORE INTO work_order VALUES (?,?,?,?,?)',(row['id'],row['equipment'],row['procedure_version'],row['status'],row.get('contract')))
            db.execute('UPDATE work_order SET contract=? WHERE id=?',(row.get('contract'),row['id']))
        for row in c.get('step',[]):
            db.execute('INSERT OR IGNORE INTO step VALUES (?,?,?,?,?)',(row['work_order'],row['position'],row['code'],row['required'],row.get('time_limit_minutes')))
            db.execute('UPDATE step SET code=?,required=?,time_limit_minutes=? WHERE work_order=? AND position=?',(row['code'],row['required'],row.get('time_limit_minutes'),row['work_order'],row['position']))
        for row in c.get('instrument',[]):
            db.execute('INSERT OR IGNORE INTO instrument VALUES (?,?,?,?,?)',(row['id'],row['unit'],row['min'],row['max'],row['calibrated_until']))
        for row in c.get('stock',[]):
            db.execute('INSERT OR IGNORE INTO stock VALUES (?,?)',(row['part'],row['issued_to']))
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
    q=p.parse_args()
    if q.command=='demo':print(demo(q.workdir))
    elif q.command=='init':initialize(q.db,q.config)
    elif q.command=='ingest':print(ingest(q.file,q.db))
    elif q.command=='evaluate':print(evaluate(q.db))
    elif q.command=='report':print(report(q.db,q.out))
    elif q.command=='inspect':
        db=connect(q.db);print(json.dumps([dict(z) for z in db.execute('SELECT * FROM decision')],ensure_ascii=False));db.close()
if __name__=='__main__':main()
