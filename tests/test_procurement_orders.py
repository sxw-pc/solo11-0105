import hashlib
import itertools
import json
from pathlib import Path

import pytest

from maintlog.__main__ import initialize
from maintlog.ingest import ingest
from maintlog.procurement import plan, procurement_report, register
from maintlog.rules import evaluate
from maintlog.store import connect

FIX = Path(__file__).resolve().parents[1] / "fixtures"
HEADER = "vendor,sku,unit_cents,available_qty,arrival_day,min_order_cents"


def prepare(tmp_path, name="procurement.db"):
    db = tmp_path / name
    initialize(db, FIX / "config.json")
    assert ingest(FIX / "events.jsonl", db)["imported"] == 6
    assert evaluate(db) == {"WO-1": "完成"}
    return db


_CASE_SEQ = [0]


def write_case(tmp_path, quotes, demand, contracts=("min_total_cost", "min_max_lateness")):
    """Write a fresh local case + quote CSV in the case's own directory."""
    _CASE_SEQ[0] += 1
    case_dir = tmp_path / f"case{_CASE_SEQ[0]}"
    case_dir.mkdir(parents=True, exist_ok=True)
    quote_path = case_dir / "quotes.csv"
    lines = [HEADER] + [
        "{},{},{},{},{},{}".format(*row) for row in quotes
    ]
    quote_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    case = {
        "work_order": "WO-1",
        "quotes_file": "quotes.csv",
        "demand": [
            {"sku": sku, "qty": qty, "deadline_day": deadline}
            for sku, qty, deadline in demand
        ],
        "contracts": list(contracts),
    }
    case_path = case_dir / "case.json"
    case_path.write_text(json.dumps(case), encoding="utf-8")
    return case_path


def brute_force(quotes, demand):
    """Independent exhaustive reference: every integer split of every SKU,
    jointly checking supplier minima over all SKUs."""
    vendors = sorted({q[0] for q in quotes})
    rows = {(q[0], q[1]): q for q in quotes}
    choices = []
    for sku, qty, deadline in demand:
        options = [v for v in vendors if (v, sku) in rows]
        splits = []
        for combo in itertools.product(*[range(qty + 1) for _ in options]):
            if sum(combo) != qty:
                continue
            if all(
                q <= rows[(v, sku)][3]
                for v, q in zip(options, combo)
                if q
            ):
                splits.append(dict(zip(options, combo)))
        if not splits:
            return None
        choices.append((sku, deadline, splits))
    feasible = []
    for combo in itertools.product(*[c[2] for c in choices]):
        totals = {v: 0 for v in vendors}
        max_late = 0
        cost = 0
        allocation = {}
        for (sku, deadline, _), split in zip(choices, combo):
            for vendor, qty in split.items():
                if not qty:
                    continue
                row = rows[(vendor, sku)]
                cost += qty * row[2]
                totals[vendor] += qty * row[2]
                max_late = max(max_late, max(0, row[4] - deadline))
                allocation[(vendor, sku)] = qty
        rejected = False
        for vendor in vendors:
            min_orders = {
                rows[(vendor, sku)][5] for _v, sku in rows if _v == vendor
            }
            if len(min_orders) != 1:
                raise AssertionError(f"inconsistent minimum for {vendor}")
            minimum = next(iter(min_orders))
            if totals[vendor] and totals[vendor] < minimum:
                rejected = True
                break
        if rejected:
            continue
        feasible.append(
            {
                "cost": cost,
                "max_late": max_late,
                "allocation": allocation,
                "vendor_totals": {
                    v: amount for v, amount in totals.items() if amount
                },
            }
        )
    if not feasible:
        return None

    def rank_cost(item):
        return (item["cost"], item["max_late"])

    def rank_late(item):
        return (item["max_late"], item["cost"])

    return {
        "min_total_cost": min(feasible, key=rank_cost),
        "min_max_lateness": min(feasible, key=rank_late),
    }


def issued(db, case_id, contract):
    return plan(db, case_id, contract)


def test_default_case_contracts_and_tradeoff(tmp_path):
    db = prepare(tmp_path)
    register(db, "BUY-1", FIX / "procurement-case.json")
    cheap = issued(db, "BUY-1", "min_total_cost")
    fast = issued(db, "BUY-1", "min_max_lateness")
    assert cheap["total_cost_cents"] == 10000
    assert cheap["max_lateness_days"] == 2
    assert [(o["vendor"], o["total_cents"]) for o in cheap["orders"]] == [("S", 10000)]
    assert fast["total_cost_cents"] == 16000
    assert fast["max_lateness_days"] == 0
    assert [(o["vendor"], o["total_cents"]) for o in fast["orders"]] == [("F", 16000)]
    for order in cheap["orders"]:
        assert order["min_order_met"] is True
        assert order["total_cents"] >= order["min_order_cents"]
        for line in order["lines"]:
            assert line["qty"] <= line["available_qty"]
            assert line["lateness_days"] == max(
                0, line["arrival_day"] - line["deadline_day"]
            )
            assert line["line_cents"] == line["qty"] * line["unit_cents"]
            assert len(line["quote_row_sha256"]) == 64
            assert line["quote_row"]["unit_cents"] == line["unit_cents"]
    for order in fast["orders"] + cheap["orders"]:
        assert order["quote_sha256"] and len(order["consumption"]["source_sha256"]) == 64
        assert order["consumption"]["event_id"]
    report_path = procurement_report(db, tmp_path / "procurement.json")
    data = json.loads(report_path.read_text(encoding="utf-8"))
    assert len(data["tradeoffs"]) == 1
    tradeoff = data["tradeoffs"][0]
    assert tradeoff["min_total_cost"]["total_cents"] == 10000
    assert tradeoff["min_max_lateness"]["max_lateness_days"] == 0
    assert "6000" in tradeoff["real_tradeoff"]
    assert "2 天" in tradeoff["real_tradeoff"]
    assert len(data["purchase_targets"]) == 1
    assert data["purchase_targets"][0]["demand"] == {"NEW-1": 2, "GASKET": 1}
    statuses = {p["contract"]: p["status"] for p in data["plans"]}
    assert statuses == {"min_total_cost": "issued", "min_max_lateness": "issued"}
    for plan_row in data["plans"]:
        assert plan_row["evidence"]["optimality"]["frontier"]
        assert plan_row["evidence"]["optimality"]["no_better_solution"]
        assert plan_row["evidence"]["quote_sha256"]


def test_row_by_row_choice_is_jointly_illegal(tmp_path):
    """NEW-1 from F is cheapest but buying only GASKET from S is 2000 < 10000."""
    db = prepare(tmp_path)
    case = write_case(
        tmp_path,
        [
            ("S", "NEW-1", 4000, 2, 5, 10000),
            ("S", "GASKET", 2000, 1, 5, 10000),
            ("F", "NEW-1", 6500, 2, 1, 0),
            ("F", "GASKET", 3000, 1, 1, 0),
        ],
        [("NEW-1", 2, 3), ("GASKET", 1, 6)],
    )
    register(db, "TRAP", case)
    cheap = plan(db, "TRAP", "min_total_cost")
    assert cheap["total_cost_cents"] == 10000
    assert [o["vendor"] for o in cheap["orders"]] == ["S"]
    for order in cheap["orders"]:
        assert order["total_cents"] >= order["min_order_cents"]
    assert cheap["max_lateness_days"] == 2


def test_same_sku_split_across_suppliers(tmp_path):
    db = prepare(tmp_path)
    case = write_case(
        tmp_path,
        [("A", "X", 1000, 2, 1, 0), ("B", "X", 1100, 1, 1, 0)],
        [("X", 3, 1)],
    )
    register(db, "SPLIT", case)
    result = plan(db, "SPLIT", "min_total_cost")
    assert result["total_cost_cents"] == 3100
    assert result["max_lateness_days"] == 0
    lines = {
        (order["vendor"], line["sku"]): line["qty"]
        for order in result["orders"]
        for line in order["lines"]
    }
    assert lines == {("A", "X"): 2, ("B", "X"): 1}
    reference = brute_force(
        [
            ("A", "X", 1000, 2, 1, 0),
            ("B", "X", 1100, 1, 1, 0),
        ],
        [("X", 3, 1)],
    )
    assert result["total_cost_cents"] == reference["min_total_cost"]["cost"]


def test_cross_sku_minimum_order_is_joint(tmp_path):
    db = prepare(tmp_path)
    case = write_case(
        tmp_path,
        [("A", "X", 1000, 2, 1, 5000), ("A", "Y", 3000, 1, 1, 5000)],
        [("X", 2, 1), ("Y", 1, 1)],
    )
    register(db, "JOINT", case)
    result = plan(db, "JOINT", "min_total_cost")
    assert result["total_cost_cents"] == 5000
    assert len(result["orders"]) == 1
    order = result["orders"][0]
    assert order["vendor"] == "A"
    assert order["total_cents"] == 5000 >= order["min_order_cents"] == 5000


def test_minimum_order_makes_single_sku_order_impossible(tmp_path):
    db = prepare(tmp_path)
    case = write_case(
        tmp_path,
        [("A", "X", 1000, 2, 1, 50000), ("B", "X", 2000, 2, 1, 0)],
        [("X", 2, 1)],
    )
    register(db, "BLOCKED", case)
    result = plan(db, "BLOCKED", "min_total_cost")
    # Only B may be used: A alone cannot reach its 50000 minimum with 2 units.
    assert [o["vendor"] for o in result["orders"]] == ["B"]
    assert result["total_cost_cents"] == 4000
    singleton = write_case(
        tmp_path,
        [("A", "X", 1000, 2, 1, 50000)],
        [("X", 2, 1)],
    )
    register(db, "UNREACHABLE", singleton)
    with pytest.raises(ValueError, match="no_feasible_order"):
        plan(db, "UNREACHABLE", "min_total_cost")


def test_capacity_exactly_full(tmp_path):
    db = prepare(tmp_path)
    case = write_case(
        tmp_path,
        [("A", "X", 1000, 3, 1, 0), ("B", "X", 1000, 1, 1, 0)],
        [("X", 4, 1)],
    )
    register(db, "FULL", case)
    result = plan(db, "FULL", "min_total_cost")
    assert result["total_cost_cents"] == 4000
    assert sum(line["qty"] for order in result["orders"] for line in order["lines"]) == 4
    over = write_case(
        tmp_path,
        [("A", "X", 1000, 3, 1, 0), ("B", "X", 1000, 1, 1, 0)],
        [("X", 5, 1)],
    )
    with pytest.raises(ValueError, match="insufficient_capacity"):
        register(db, "OVER", over)


def test_missing_sku_and_bad_quotes_block_entire_order(tmp_path):
    db = prepare(tmp_path)
    missing = write_case(
        tmp_path,
        [("A", "X", 1000, 2, 1, 0)],
        [("X", 2, 1), ("Z", 1, 1)],
    )
    with pytest.raises(ValueError, match="missing_sku:Z"):
        register(db, "MISSING", missing)

    zero_price = write_case(
        tmp_path,
        [("A", "X", 0, 2, 1, 0)],
        [("X", 2, 1)],
    )
    with pytest.raises(ValueError, match="price_non_positive"):
        register(db, "FREE", zero_price)

    conflict_dir = tmp_path / "conflict"
    conflict_dir.mkdir()
    conflicting = conflict_dir / "conflicting.csv"
    conflicting.write_text(
        HEADER
        + "\nA,X,1000,2,1,0\nA,X,1100,2,1,0\n",
        encoding="utf-8",
    )
    case_path = conflict_dir / "case.json"
    case_path.write_text(
        json.dumps(
            {
                "work_order": "WO-1",
                "quotes_file": "conflicting.csv",
                "demand": [{"sku": "X", "qty": 2, "deadline_day": 1}],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="quote_conflict:A:X"):
        register(db, "CONFLICT", case_path)
    c = connect(db)
    try:
        assert (
            c.execute("SELECT COUNT(*) FROM procurement_plan").fetchone()[0] == 0
        )
        assert (
            c.execute("SELECT COUNT(*) FROM procurement_order").fetchone()[0] == 0
        )
    finally:
        c.close()


def test_quote_evidence_tamper_blocks_planning(tmp_path):
    db = prepare(tmp_path)
    register(db, "BUY-1", FIX / "procurement-case.json")
    c = connect(db)
    with c:
        c.execute(
            "UPDATE procurement_case SET payload=replace(payload,'\"unit_cents\":4000','\"unit_cents\":3000') WHERE id='BUY-1'"
        )
    c.close()
    with pytest.raises(ValueError, match="untrusted_case_evidence"):
        plan(db, "BUY-1", "min_total_cost")


def test_consumption_event_tamper_blocks_planning(tmp_path):
    db = prepare(tmp_path)
    register(db, "BUY-1", FIX / "procurement-case.json")
    c = connect(db)
    with c:
        c.execute("UPDATE event SET payload=replace(payload,'NEW-1','NEW-2') WHERE kind='replace'")
    c.close()
    with pytest.raises(ValueError, match="untrusted_source_evidence"):
        plan(db, "BUY-1", "min_total_cost")


def test_repeat_run_is_stable_and_orders_immutable(tmp_path):
    db = prepare(tmp_path)
    register(db, "BUY-1", FIX / "procurement-case.json")
    first = plan(db, "BUY-1", "min_total_cost")
    second = plan(db, "BUY-1", "min_total_cost")
    assert second["replanned"] is False
    assert second["plan_id"] == first["plan_id"]
    assert second["orders"] == first["orders"]
    assert second["solution_sha256"] == first["solution_sha256"]
    c = connect(db)
    try:
        assert c.execute("SELECT COUNT(*) FROM procurement_plan").fetchone()[0] == 1
        with pytest.raises(Exception, match="immutable"):
            c.execute("UPDATE procurement_order SET total_cents=1")
        with pytest.raises(Exception, match="immutable"):
            c.execute("DELETE FROM procurement_order_line")
    finally:
        c.close()


def test_quote_revision_supersedes_old_order_with_delta(tmp_path):
    db = prepare(tmp_path)
    register(db, "BUY-1", FIX / "procurement-case.json")
    old = plan(db, "BUY-1", "min_total_cost")
    revised = write_case(
        tmp_path,
        [
            ("S", "NEW-1", 3500, 2, 5, 10000),
            ("S", "GASKET", 2000, 1, 5, 10000),
            ("F", "NEW-1", 6500, 2, 1, 0),
            ("F", "GASKET", 3000, 1, 1, 0),
        ],
        [("NEW-1", 2, 3), ("GASKET", 1, 6)],
    )
    with pytest.raises(ValueError, match="procurement_case_conflict"):
        register(db, "BUY-1", revised)
    register(db, "BUY-1", revised, allow_revision=True)
    new = plan(db, "BUY-1", "min_total_cost")
    assert new["plan_id"] != old["plan_id"]
    assert new["total_cost_cents"] == 16000
    changes = new["changes"]
    assert changes["supersedes_plan_id"] == old["plan_id"]
    assert [o["order_id"] for o in changes["cancel_orders"]] == [
        o["order_id"] for o in old["orders"]
    ]
    assert changes["vendor_amount_delta"]["S"]["delta_cents"] == -10000
    assert changes["vendor_amount_delta"]["F"]["delta_cents"] == 16000
    assert changes["sku_quantity_delta"]["NEW-1"]["delta_qty"] == 0
    assert "不得把两份订单" in changes["instruction"]
    c = connect(db)
    try:
        assert (
            c.execute(
                "SELECT COUNT(*) FROM procurement_plan WHERE status='issued'"
            ).fetchone()[0]
            == 1
        )
        status = c.execute(
            "SELECT status FROM procurement_plan WHERE id=?", (old["plan_id"],)
        ).fetchone()[0]
        assert status == "superseded"
        # The old order row still exists unchanged (not overwritten).
        grand_old = c.execute(
            "SELECT total_cents FROM procurement_order WHERE plan_id=?",
            (old["plan_id"],),
        ).fetchall()
        assert [row[0] for row in grand_old] == [10000]
    finally:
        c.close()
    again = plan(db, "BUY-1", "min_total_cost")
    assert again["replanned"] is False


def test_revision_that_keeps_same_optimal_order_does_not_duplicate(tmp_path):
    db = prepare(tmp_path)
    register(db, "BUY-1", FIX / "procurement-case.json")
    first = plan(db, "BUY-1", "min_total_cost")
    # A new quote that cannot compete changes the frozen input but not the
    # optimal order; the old order must stay in force.
    revised = write_case(
        tmp_path,
        [
            ("S", "NEW-1", 4000, 2, 5, 10000),
            ("S", "GASKET", 2000, 1, 5, 10000),
            ("F", "NEW-1", 6500, 2, 1, 0),
            ("F", "GASKET", 3000, 1, 1, 0),
            ("G", "NEW-1", 7000, 2, 2, 0),
            ("G", "GASKET", 4000, 1, 2, 0),
        ],
        [("NEW-1", 2, 3), ("GASKET", 1, 6)],
    )
    register(db, "BUY-1", revised, allow_revision=True)
    second = plan(db, "BUY-1", "min_total_cost")
    assert second["plan_id"] == first["plan_id"]
    assert second["replanned"] is False
    assert second["order_unchanged"] is True
    assert second["input_changed"] is True
    assert "不重复下单" in second["revision_note"]


def test_time_order_prefers_earlier_arrival_for_lateness_contract(tmp_path):
    db = prepare(tmp_path)
    case = write_case(
        tmp_path,
        [
            ("SLOW", "X", 1000, 2, 9, 0),
            ("FAST", "X", 1200, 2, 2, 0),
        ],
        [("X", 2, 5)],
    )
    register(db, "TIME", case)
    cost_plan = plan(db, "TIME", "min_total_cost")
    assert cost_plan["total_cost_cents"] == 2000
    assert cost_plan["max_lateness_days"] == 4
    assert [o["vendor"] for o in cost_plan["orders"]] == ["SLOW"]
    fast_plan = plan(db, "TIME", "min_max_lateness")
    assert fast_plan["total_cost_cents"] == 2400
    assert fast_plan["max_lateness_days"] == 0
    assert [o["vendor"] for o in fast_plan["orders"]] == ["FAST"]


def test_no_supplier_subset_is_feasible(tmp_path):
    db = prepare(tmp_path)
    case = write_case(
        tmp_path,
        [
            ("A", "X", 1000, 1, 1, 10000),
            ("B", "Y", 1000, 1, 1, 10000),
        ],
        [("X", 1, 1), ("Y", 1, 1)],
    )
    register(db, "NOSUBSET", case)
    with pytest.raises(ValueError, match="no_feasible_order"):
        plan(db, "NOSUBSET", "min_total_cost")
    c = connect(db)
    try:
        assert c.execute("SELECT COUNT(*) FROM procurement_order").fetchone()[0] == 0
        assert (
            c.execute("SELECT COUNT(*) FROM procurement_plan").fetchone()[0] == 0
        )
    finally:
        c.close()


@pytest.mark.parametrize(
    "quotes,demand",
    [
        (
            [
                ("A", "X", 4000, 2, 2, 0),
                ("A", "Y", 2500, 3, 2, 0),
                ("B", "X", 3900, 1, 1, 7000),
                ("B", "Y", 2400, 1, 1, 7000),
                ("C", "X", 4200, 2, 5, 0),
                ("C", "Y", 2600, 3, 5, 0),
            ],
            [("X", 3, 3), ("Y", 3, 4)],
        ),
        (
            [
                ("A", "X", 1000, 2, 7, 0),
                ("A", "Y", 5000, 1, 7, 0),
                ("B", "X", 4000, 2, 1, 0),
                ("B", "Y", 3000, 1, 1, 0),
                ("C", "Y", 2000, 1, 2, 9000),
            ],
            [("X", 2, 2), ("Y", 2, 3)],
        ),
    ],
)
def test_optimality_matches_independent_brute_force(tmp_path, quotes, demand):
    db = prepare(tmp_path)
    case = write_case(tmp_path, quotes, demand)
    register(db, "CHECK", case)
    cheap = plan(db, "CHECK", "min_total_cost")
    fast = plan(db, "CHECK", "min_max_lateness")
    reference = brute_force(quotes, demand)
    assert reference is not None
    best_cost = reference["min_total_cost"]
    best_late = reference["min_max_lateness"]
    assert cheap["total_cost_cents"] == best_cost["cost"]
    assert cheap["max_lateness_days"] == best_cost["max_late"]
    assert fast["max_lateness_days"] == best_late["max_late"]
    assert fast["total_cost_cents"] == best_late["cost"]
    for result, expected in ((cheap, best_cost), (fast, best_late)):
        got = {
            order["vendor"]: order["total_cents"] for order in result["orders"]
        }
        assert got == expected["vendor_totals"]
    c = connect(db)
    try:
        for row in c.execute("SELECT contract,evidence FROM procurement_plan"):
            proof = json.loads(row["evidence"])["optimality"]
            assert proof["no_better_solution"]
            assert proof["frontier"]
    finally:
        c.close()


def test_contract_must_be_authorised_by_case(tmp_path):
    db = prepare(tmp_path)
    only_cost = write_case(
        tmp_path,
        [("A", "X", 1000, 1, 1, 0)],
        [("X", 1, 1)],
        contracts=("min_total_cost",),
    )
    register(db, "ONE", only_cost)
    with pytest.raises(ValueError, match="contract_not_authorised"):
        plan(db, "ONE", "min_max_lateness")


def test_legacy_database_is_migrated(tmp_path):
    """A seed-version DB without the payload hash column still plans."""
    db = tmp_path / "legacy.db"
    import sqlite3

    raw = sqlite3.connect(db)
    raw.execute(
        "CREATE TABLE procurement_case(id TEXT PRIMARY KEY,payload TEXT NOT NULL,digest TEXT NOT NULL)"
    )
    raw.commit()
    raw.close()
    initialize(db, FIX / "config.json")
    assert ingest(FIX / "events.jsonl", db)["imported"] == 6
    evaluate(db)
    register(db, "BUY-1", FIX / "procurement-case.json")
    result = plan(db, "BUY-1", "min_total_cost")
    assert result["total_cost_cents"] == 10000
    c = connect(db)
    try:
        assert c.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        c.close()


def test_small_case_scale_limits(tmp_path):
    db = prepare(tmp_path)
    too_many = write_case(
        tmp_path,
        [("A", f"X{i}", 1000, 1, 1, 0) for i in range(9)],
        [(f"X{i}", 1, 1) for i in range(9)],
    )
    with pytest.raises(ValueError, match="too_many_skus"):
        register(db, "BIGSKU", too_many)
    too_many_vendors = write_case(
        tmp_path,
        [("A", "X", 1000, 1, 1, 0), ("B", "X", 1000, 1, 1, 0), ("C", "X", 1000, 1, 1, 0), ("D", "X", 1000, 1, 1, 0)],
        [("X", 1, 1)],
    )
    with pytest.raises(ValueError, match="too_many_suppliers"):
        register(db, "BIGVENDOR", too_many_vendors)
