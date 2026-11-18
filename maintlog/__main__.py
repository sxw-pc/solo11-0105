import argparse
import json
import tempfile
from pathlib import Path

from .store import connect
from .ingest import ingest
from .rules import evaluate
from .report import report
from .procurement import register as register_procurement
from .procurement import plan as plan_procurement
from .procurement import procurement_report


def initialize(db_path, config):
    c = json.loads(Path(config).read_text(encoding="utf-8"))
    db = connect(db_path)
    with db:
        for table, cols in [
            ("work_order", ["id", "equipment", "procedure_version", "status"]),
            ("step", ["work_order", "position", "code", "required"]),
            ("instrument", ["id", "unit", "min", "max", "calibrated_until"]),
            ("stock", ["part", "issued_to"]),
        ]:
            for row in c[table]:
                db.execute(
                    f'INSERT OR IGNORE INTO {table} VALUES ({",".join("?" for _ in cols)})',
                    [row[k] for k in cols],
                )
    db.close()


def demo(workdir=None):
    root = Path(workdir or tempfile.mkdtemp(prefix="maintlog-"))
    root.mkdir(parents=True, exist_ok=True)
    fix = Path(__file__).resolve().parent.parent / "fixtures"
    db = root / "maintlog.db"
    initialize(db, fix / "config.json")
    ingest(fix / "events.jsonl", db)
    evaluate(db)
    return report(db, root / "report.json")


def main():
    p = argparse.ArgumentParser(description="离线设备保养采录")
    s = p.add_subparsers(dest="command", required=True)
    d = s.add_parser("demo")
    d.add_argument("--workdir")
    i = s.add_parser("init")
    i.add_argument("--db", required=True)
    i.add_argument("--config", required=True)
    a = s.add_parser("ingest")
    a.add_argument("file")
    a.add_argument("--db", required=True)
    e = s.add_parser("evaluate")
    e.add_argument("--db", required=True)
    r = s.add_parser("report")
    r.add_argument("--db", required=True)
    r.add_argument("--out", required=True)
    x = s.add_parser("inspect")
    x.add_argument("--db", required=True)

    reg = s.add_parser("procure-register", help="冻结已验收换件事件与报价原文")
    reg.add_argument("case_id")
    reg.add_argument("--db", required=True)
    reg.add_argument("--case", required=True)
    reg.add_argument(
        "--allow-revision",
        action="store_true",
        help="以新报价修订同一采购案；旧计划将整单作废，绝不叠加",
    )

    pl = s.add_parser("procure-plan", help="按合同签发完整采购订单")
    pl.add_argument("case_id")
    pl.add_argument("contract", choices=["min_total_cost", "min_max_lateness"])
    pl.add_argument("--db", required=True)

    pr = s.add_parser("procure-report", help="导出采购订单与最优性 JSON 报告")
    pr.add_argument("--db", required=True)
    pr.add_argument("--out", required=True)
    pr.add_argument("--case-id")

    q = p.parse_args()
    if q.command == "demo":
        print(demo(q.workdir))
    elif q.command == "init":
        initialize(q.db, q.config)
    elif q.command == "ingest":
        print(ingest(q.file, q.db))
    elif q.command == "evaluate":
        print(evaluate(q.db))
    elif q.command == "report":
        print(report(q.db, q.out))
    elif q.command == "inspect":
        db = connect(q.db)
        print(
            json.dumps(
                [dict(z) for z in db.execute("SELECT * FROM decision")],
                ensure_ascii=False,
            )
        )
        db.close()
    elif q.command == "procure-register":
        print(
            json.dumps(
                register_procurement(
                    q.db, q.case_id, q.case, allow_revision=q.allow_revision
                ),
                ensure_ascii=False,
            )
        )
    elif q.command == "procure-plan":
        print(
            json.dumps(
                plan_procurement(q.db, q.case_id, q.contract),
                ensure_ascii=False,
                indent=2,
            )
        )
    elif q.command == "procure-report":
        print(procurement_report(q.db, q.out, q.case_id))


if __name__ == "__main__":
    main()
