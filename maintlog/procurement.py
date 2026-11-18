"""Freeze part consumption and signed supplier quotes, then issue exact purchase orders.

The procurement case is deliberately offline: quote bytes and maintenance source
bytes are hashed when frozen, and planning only reads the frozen case from
SQLite.  Two authorised contracts are supported:

* ``min_total_cost``    -- minimise complete order cost, then max positive lateness
* ``min_max_lateness``  -- minimise max positive lateness, then complete order cost

Both contracts satisfy every demand line, every quoted capacity, and each
supplier minimum-order amount computed over all SKUs bought from that supplier
in the same planning run.  No partial order is ever written.
"""

from __future__ import annotations

import csv
import hashlib
import json
from collections import defaultdict
from pathlib import Path

from .store import connect

CONTRACTS = ("min_total_cost", "min_max_lateness")
QUOTE_COLUMNS = (
    "vendor",
    "sku",
    "unit_cents",
    "available_qty",
    "arrival_day",
    "min_order_cents",
)
PLANNER_VERSION = "procurement-1"
MAX_SKUS = 8
MAX_VENDORS = 3


# ---------------------------------------------------------------------------
# canonical hashing helpers


def _sha256(raw):
    if isinstance(raw, str):
        raw = raw.encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _canonical(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _row_sha(row):
    return _sha256(_canonical(row))


def _as_int(value, field, *, minimum=None, positive=False):
    if isinstance(value, bool):
        raise ValueError(f"bad_quote:{field}")
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        raise ValueError(f"bad_quote:{field}") from None
    if positive and number <= 0:
        raise ValueError(f"price_non_positive:{field}")
    if minimum is not None and number < minimum:
        raise ValueError(f"bad_quote:{field}")
    return number


# ---------------------------------------------------------------------------
# validation


def _read_quotes(raw):
    """Validate CSV quote bytes and return normalised quote rows.

    A duplicate supplier/SKU row is allowed only when every field agrees; a
    differing duplicate is a quote conflict and blocks the whole case.
    """
    try:
        text = raw.decode("utf-8-sig")
        reader = csv.DictReader(text.splitlines())
        if reader.fieldnames is None:
            raise ValueError("quotes_empty")
        names = [name.strip() for name in reader.fieldnames]
        missing = [name for name in QUOTE_COLUMNS if name not in names]
        if missing:
            raise ValueError("quote_columns_missing:" + ",".join(missing))
        reader.fieldnames = names
        raw_rows = list(reader)
    except (UnicodeDecodeError, csv.Error) as exc:
        raise ValueError("quote_unreadable") from exc
    if not raw_rows:
        raise ValueError("quotes_empty")

    quotes = []
    seen = {}
    vendor_min = {}
    for index, row in enumerate(raw_rows, start=2):  # header is line 1
        vendor = (row.get("vendor") or "").strip()
        sku = (row.get("sku") or "").strip()
        if not vendor or not sku:
            raise ValueError(f"quote_row_incomplete:{index}")
        unit = _as_int(row.get("unit_cents"), "unit_cents", positive=True)
        available = _as_int(row.get("available_qty"), "available_qty", minimum=0)
        arrival = _as_int(row.get("arrival_day"), "arrival_day", minimum=0)
        min_order = _as_int(row.get("min_order_cents"), "min_order_cents", minimum=0)
        clean = {
            "vendor": vendor,
            "sku": sku,
            "unit_cents": unit,
            "available_qty": available,
            "arrival_day": arrival,
            "min_order_cents": min_order,
        }
        # Preserve extra signed columns in row evidence without letting them
        # influence the maths.
        for extra in sorted(set(row) - set(QUOTE_COLUMNS)):
            if extra:
                clean[extra] = row.get(extra)
        key = (vendor, sku)
        if key in seen:
            if seen[key]["row_sha256"] != _row_sha(clean):
                raise ValueError(f"quote_conflict:{vendor}:{sku}")
            continue
        prior_min = vendor_min.setdefault(vendor, min_order)
        if prior_min != min_order:
            raise ValueError(f"quote_conflict:{vendor}:min_order_cents")
        seen[key] = {**clean, "row_sha256": _row_sha(clean)}
        quotes.append(seen[key])
    if len({quote["vendor"] for quote in quotes}) > MAX_VENDORS:
        raise ValueError("too_many_suppliers")
    return sorted(quotes, key=lambda q: (q["vendor"], q["sku"]))


def _normalise_demand(case):
    demand = case.get("demand")
    if not isinstance(demand, list) or not demand:
        raise ValueError("bad_procurement_case:demand")
    out = []
    seen = set()
    for item in demand:
        if not isinstance(item, dict):
            raise ValueError("bad_procurement_case:demand")
        sku = item.get("sku")
        if not isinstance(sku, str) or not sku.strip():
            raise ValueError("bad_procurement_case:demand")
        sku = sku.strip()
        qty = _as_int(item.get("qty"), "qty", positive=True)
        deadline = _as_int(item.get("deadline_day"), "deadline_day", minimum=0)
        if sku in seen:
            raise ValueError(f"duplicate_demand_sku:{sku}")
        seen.add(sku)
        out.append({"sku": sku, "qty": qty, "deadline_day": deadline})
    if len(out) > MAX_SKUS:
        raise ValueError("too_many_skus")
    return sorted(out, key=lambda d: d["sku"])


def _normalise_quotes(quotes):
    """Re-validate frozen quote evidence as plain dictionaries.

    Extra signed columns are preserved in row evidence, but only the six
    canonical columns drive capacity, price, arrival, and minimum-order maths.
    """
    if not isinstance(quotes, list) or not quotes:
        raise ValueError("quotes_empty")
    out = []
    seen = {}
    vendor_min = {}
    for row in quotes:
        if not isinstance(row, dict):
            raise ValueError("quote_unreadable")
        vendor = str(row.get("vendor", "")).strip()
        sku = str(row.get("sku", "")).strip()
        if not vendor or not sku:
            raise ValueError("quote_row_incomplete")
        unit = _as_int(row.get("unit_cents"), "unit_cents", positive=True)
        available = _as_int(row.get("available_qty"), "available_qty", minimum=0)
        arrival = _as_int(row.get("arrival_day"), "arrival_day", minimum=0)
        min_order = _as_int(row.get("min_order_cents"), "min_order_cents", minimum=0)
        clean = {
            "vendor": vendor,
            "sku": sku,
            "unit_cents": unit,
            "available_qty": available,
            "arrival_day": arrival,
            "min_order_cents": min_order,
        }
        for key in sorted(set(row) - set(QUOTE_COLUMNS)):
            if key != "row_sha256":
                clean[key] = row[key]
        key = (vendor, sku)
        row_hash = _row_sha(clean)
        if key in seen:
            if seen[key]["row_sha256"] != row_hash:
                raise ValueError(f"quote_conflict:{vendor}:{sku}")
            continue
        prior = vendor_min.setdefault(vendor, min_order)
        if prior != min_order:
            raise ValueError(f"quote_conflict:{vendor}:min_order_cents")
        seen[key] = {**clean, "row_sha256": row_hash}
        out.append(seen[key])
    if len({quote["vendor"] for quote in out}) > MAX_VENDORS:
        raise ValueError("too_many_suppliers")
    return sorted(out, key=lambda q: (q["vendor"], q["sku"]))


def _check_demand_against_quotes(demand, quotes):
    by_sku = defaultdict(list)
    for row in quotes:
        by_sku[row["sku"]].append(row)
    for item in demand:
        rows = by_sku.get(item["sku"], [])
        if not rows:
            raise ValueError(f"missing_sku:{item['sku']}")
        capacity = sum(row["available_qty"] for row in rows)
        if capacity < item["qty"]:
            raise ValueError(
                f"insufficient_capacity:{item['sku']}:{capacity}<{item['qty']}"
            )
    return by_sku


def _verified_consumption(db, payload, work_order):
    """Re-hash every frozen consumption reference against current SQLite rows."""
    consumed = payload.get("consumed")
    if not isinstance(consumed, list) or not consumed:
        raise ValueError("consumed_part_missing")
    evidence = []
    for item in consumed:
        try:
            event_id = int(item["id"])
        except (KeyError, TypeError, ValueError):
            raise ValueError("untrusted_source_evidence:consumption") from None
        event = db.execute(
            "SELECT e.*,s.sha256 AS source_sha256,s.raw AS source_raw "
            "FROM event e JOIN source s ON s.id=e.source_id WHERE e.id=?",
            (event_id,),
        ).fetchone()
        if not event:
            raise ValueError("untrusted_source_evidence:consumption")
        if event["work_order"] != work_order or event["kind"] != "replace":
            raise ValueError("untrusted_source_evidence:consumption")
        try:
            body = json.loads(event["payload"])
        except (TypeError, json.JSONDecodeError):
            raise ValueError("untrusted_source_evidence:payload") from None
        payload_sha = _sha256(_canonical(body))
        source_sha = _sha256(event["source_raw"])
        if (
            payload_sha != event["sha256"]
            or payload_sha != item.get("sha256")
            or source_sha != event["source_sha256"]
            or source_sha != item.get("source_sha256")
        ):
            raise ValueError("untrusted_source_evidence:digest")
        row = db.execute(
            "SELECT * FROM evidence WHERE event_id=? AND role='maintenance_event'",
            (event_id,),
        ).fetchone()
        if (
            not row
            or row["payload_sha256"] != payload_sha
            or row["source_sha256"] != source_sha
        ):
            raise ValueError("untrusted_source_evidence:evidence")
        try:
            new_part = body.get("new")
        except AttributeError:
            new_part = None
        evidence.append(
            {
                "event_id": event_id,
                "source_sha256": source_sha,
                "payload_sha256": payload_sha,
                "new_part": new_part,
                "at": event["at"],
            }
        )
    evidence.sort(key=lambda e: e["event_id"])
    return evidence


def _load_case(db, case_id):
    row = db.execute(
        "SELECT id,payload,digest,payload_sha256 FROM procurement_case WHERE id=?",
        (case_id,),
    ).fetchone()
    if not row:
        raise ValueError(f"unknown_case:{case_id}")
    payload_sha = _sha256(row["payload"])
    if row["payload_sha256"] and row["payload_sha256"] != payload_sha:
        raise ValueError("untrusted_case_evidence:payload_sha256")
    try:
        payload = json.loads(row["payload"])
        case = payload["case"]
        raw_quotes = payload["quotes"]
        quote_sha = payload["quote_sha256"]
    except (TypeError, KeyError, json.JSONDecodeError):
        raise ValueError("untrusted_case_evidence:payload") from None
    if not isinstance(quote_sha, str) or len(quote_sha) != 64:
        raise ValueError("untrusted_case_evidence:quote_sha256")
    work_order = case.get("work_order")
    if not isinstance(work_order, str) or not work_order:
        raise ValueError("bad_procurement_case:work_order")
    decision = db.execute(
        "SELECT status,detail FROM decision WHERE work_order=?", (work_order,)
    ).fetchone()
    if not decision or decision["status"] != "完成":
        raise ValueError("consumption_order_not_complete")
    demand = _normalise_demand(case)
    quotes = _normalise_quotes(raw_quotes)
    by_sku = _check_demand_against_quotes(demand, quotes)
    consumption = _verified_consumption(db, payload, work_order)
    return {
        "case_id": case_id,
        "payload": payload,
        "case": case,
        "demand": demand,
        "quotes": quotes,
        "by_sku": by_sku,
        "quote_sha256": quote_sha,
        "payload_sha256": payload_sha,
        "work_order": work_order,
        "decision": dict(decision),
        "consumption": consumption,
    }


# ---------------------------------------------------------------------------
# exact solver
#
# The instance is tiny by construction (<=8 SKUs, <=3 suppliers), so the solver
# is exhaustive rather than heuristic.  For every *subset* of suppliers that
# could receive an order we enumerate every full integer allocation of all SKU
# quantities across their quote rows, reject allocations that violate any quote
# capacity or the supplier-wide minimum-order amount, and compare the remainder
# on the contract's lexicographic objective.  Splitting one SKU across several
# suppliers is therefore evaluated jointly with the per-supplier minimum-order
# arithmetic, not by picking the cheapest row per SKU first.


def _eligible_rows(case, candidate, limit):
    deadlines = {item["sku"]: item["deadline_day"] for item in case["demand"]}
    rows = {}
    for row in case["quotes"]:
        if (row["vendor"], row["sku"]) not in candidate:
            continue
        if row["arrival_day"] <= deadlines[row["sku"]] + limit:
            rows[(row["vendor"], row["sku"])] = row
    return rows


def _solve_mask(case, rows, mask, vendors):
    """Exact minimum-cost integer allocation for one supplier subset.

    The DP runs over SKUs.  Its state is, for every active supplier, its spend
    so far capped at that supplier's minimum-order amount:

    * below the cap the capped value is the exact spend, so two paths that
      reach it have identical futures either way;
    * at or above the cap the supplier's minimum is already met, and no later
      decision depends on how far above it the spend is.

    For each SKU every integer split of its demand across the active suppliers
    is enumerated respecting quote capacity, and splits are reduced to the
    Pareto frontier of their spend vector: a split dominated in every supplier
    spend can always be replaced by its dominator, so pruning is exact.  At the
    end a supplier whose spend is positive but still below its minimum is
    rejected; a supplier with zero spend is simply not ordered from.

    Returns None when no allocation satisfies demand, capacity, and every
    ordered supplier's minimum amount.  Equal-cost allocations are broken
    deterministically by the vendor-by-SKU quantity tuple.
    """
    active = [vendors[i] for i in range(len(vendors)) if mask >> i & 1]
    if not active:
        return None

    min_order = {}
    for vendor in active:
        values = {row["min_order_cents"] for row in rows.values() if row["vendor"] == vendor}
        if len(values) > 1:
            raise ValueError(f"quote_conflict:{vendor}:min_order_cents")
        min_order[vendor] = next(iter(values), 0)
    caps = tuple(min_order[vendor] for vendor in active)

    sku_choices = []
    for item in case["demand"]:
        options = [
            (vendor, rows[(vendor, item["sku"])])
            for vendor in active
            if (vendor, item["sku"]) in rows
        ]
        if not options:
            return None
        positions = [active.index(vendor) for vendor, _row in options]
        prices = [row["unit_cents"] for _vendor, row in options]
        caps_by_option = [row["available_qty"] for _vendor, row in options]
        splits = []
        quantities = [0] * len(options)

        def distribute(pos, left):
            if pos == len(options) - 1:
                if left <= caps_by_option[pos]:
                    quantities[pos] = left
                    splits.append(tuple(quantities))
                return
            for qty in range(min(left, caps_by_option[pos]) + 1):
                quantities[pos] = qty
                distribute(pos + 1, left - qty)
            quantities[pos] = 0

        distribute(0, item["qty"])
        if not splits:
            return None
        sku_choices.append(
            (item["sku"], positions, prices, _pareto_splits(splits, prices))
        )

    zero = tuple(0 for _ in active)
    # state -> (cost, chosen split index per processed SKU)
    states = {zero: (0, ())}
    for _sku, positions, prices, splits in sku_choices:
        nxt = {}
        for state, (cost, chosen) in states.items():
            for split_index, split in enumerate(splits):
                add = 0
                updated = list(state)
                for i, qty in enumerate(split):
                    if qty:
                        spend = qty * prices[i]
                        add += spend
                        updated[positions[i]] += spend
                new_state = tuple(
                    min(updated[i], caps[i]) if caps[i] else 0
                    for i in range(len(active))
                )
                candidate = (cost + add, chosen + (split_index,))
                prior = nxt.get(new_state)
                if prior is None or candidate < prior:
                    nxt[new_state] = candidate
        states = nxt
        if not states:
            return None

    best = None
    for state, (cost, chosen) in states.items():
        # The capped state hides the exact spend only once it is at the cap,
        # so a positive state below the cap is exactly the spend.
        if any(
            cap and 0 < state[i] < cap for i, cap in enumerate(caps)
        ):
            continue
        expanded = _expanded(active, sku_choices, chosen)
        key = (cost, expanded)
        if best is None or key < best[0]:
            best = (key, cost, chosen)
    if best is None:
        return None
    _key, cost, chosen = best
    allocation = {}
    for (sku, positions, prices, splits), split_index in zip(sku_choices, chosen):
        for i, qty in enumerate(splits[split_index]):
            if qty:
                allocation[(active[positions[i]], sku)] = qty
    return {
        "cost_cents": cost,
        "allocation": dict(sorted(allocation.items())),
        "mask": mask,
        "active": active,
    }


def _pareto_splits(splits, prices):
    """Keep only splits not dominated in spend at every active supplier.

    Every split covers the same SKU quantity, so a split whose per-supplier
    spend vector is <= another's in all coordinates (and < in one) can never
    lead to a better complete order: minimum-order feasibility is monotone in
    spend, and the objective is a sum of those spends.
    """
    indexed = []
    for split in splits:
        vector = tuple(qty * price for qty, price in zip(split, prices))
        indexed.append((vector, split))
    indexed.sort(key=lambda item: (item[0], item[1]))
    frontier = []
    for vector, split in indexed:
        dominated = False
        for kept_vector, _kept in frontier:
            if all(a <= b for a, b in zip(kept_vector, vector)):
                dominated = True
                break
        if not dominated:
            frontier.append((vector, split))
    return [split for _vector, split in frontier]


def _expanded(active, sku_choices, chosen):
    """Stable vendor-by-SKU quantity tuple used as the tie-break key."""
    mapping = {}
    for (sku, positions, _prices, splits), split_index in zip(sku_choices, chosen):
        for i, qty in enumerate(splits[split_index]):
            mapping[(active[positions[i]], sku)] = qty
    return tuple(
        mapping.get((vendor, sku), 0)
        for vendor in active
        for sku in (item[0] for item in sku_choices)
    )


def _candidate_lateness(demand, quotes):
    deadline = {item["sku"]: item["deadline_day"] for item in demand}
    values = {0}
    for row in quotes:
        values.add(max(0, row["arrival_day"] - deadline[row["sku"]]))
    return sorted(values)


def _zero_lateness_rows(case):
    deadlines = {item["sku"]: item["deadline_day"] for item in case["demand"]}
    return {
        (row["vendor"], row["sku"])
        for row in case["quotes"]
        if row["arrival_day"] <= deadlines[row["sku"]]
    }


def _prune_unusable_vendors(rows, demand):
    """Drop suppliers that cannot reach their own minimum even at maximum use.

    For a supplier, the largest spend it could ever receive is the demand
    quantity per SKU (capped by quote capacity) at its own unit price.  If that
    is below a positive minimum-order amount, the supplier can never be part of
    a valid complete order, so its rows only enlarge the search.
    """
    by_vendor = defaultdict(list)
    for row in rows.values():
        by_vendor[row["vendor"]].append(row)
    quantities = {item["sku"]: item["qty"] for item in demand}
    keep = {}
    for vendor, vendor_rows in by_vendor.items():
        minimum = {row["min_order_cents"] for row in vendor_rows}
        if len(minimum) != 1:
            raise ValueError(f"quote_conflict:{vendor}:min_order_cents")
        min_order = next(iter(minimum))
        upper_bound = sum(
            min(quantities[row["sku"]], row["available_qty"]) * row["unit_cents"]
            for row in vendor_rows
        )
        if min_order and upper_bound < min_order:
            continue
        keep.update({(row["vendor"], row["sku"]): row for row in vendor_rows})
    return keep


def _free_allocation(rows, demand, active):
    """Cheapest allocation ignoring minimum-order amounts, by unit price.

    Returns (cost, allocation, minimums_ok).  This is a lower bound on any
    allocation that uses only these suppliers, and it is optimal whenever it
    already satisfies every supplier's minimum.
    """
    quantities = {item["sku"]: item["qty"] for item in demand}
    units = sorted(
        (row["unit_cents"], row["sku"], row["vendor"], row["available_qty"])
        for (vendor, _sku), row in rows.items()
        if vendor in active
    )
    remaining = dict(quantities)
    allocation = {}
    cost = 0
    for price, sku, vendor, capacity in units:
        take = min(capacity, remaining.get(sku, 0))
        if take:
            allocation[(vendor, sku)] = take
            cost += take * price
            remaining[sku] -= take
    if any(remaining.get(sku, 0) for sku in quantities):
        return None
    spend = defaultdict(int)
    for (vendor, sku), qty in allocation.items():
        spend[vendor] += qty * rows[(vendor, sku)]["unit_cents"]
    minimum = {}
    for vendor in active:
        values = {row["min_order_cents"] for row in rows.values() if row["vendor"] == vendor}
        if len(values) > 1:
            raise ValueError(f"quote_conflict:{vendor}:min_order_cents")
        minimum[vendor] = next(iter(values), 0)
    ok = all(
        spend[vendor] == 0 or spend[vendor] >= minimum[vendor] for vendor in active
    )
    return cost, dict(sorted(allocation.items())), ok


def _best_at_limit(case, candidate, limit):
    """Exact minimum-cost complete order with every line lateness <= limit."""
    rows = _eligible_rows(case, candidate, limit)
    pruned = _prune_unusable_vendors(rows, case["demand"])
    if not pruned:
        return None
    for item in case["demand"]:
        if not any(row["sku"] == item["sku"] for row in pruned.values()):
            return None
    # Masks index only suppliers with usable rows; ordering is by vendor name
    # so the tie-break (mask number) is stable across repeated planning.
    vendors = sorted({row["vendor"] for row in pruned.values()})
    best = None
    for mask in range(1, 1 << len(vendors)):
        active = [vendors[i] for i in range(len(vendors)) if mask >> i & 1]
        free = _free_allocation(pruned, case["demand"], active)
        if free is None:
            continue
        # The free allocation is a lower bound for this supplier subset.  Masks
        # are visited in ascending order, so a bound that merely ties the
        # incumbent cannot beat it on the (cost, mask) key.
        if best is not None and free[0] >= best[0][0]:
            continue
        if free[2]:
            result = {
                "cost_cents": free[0],
                "allocation": free[1],
                "mask": mask,
                "active": active,
            }
        else:
            result = _solve_mask(case, pruned, mask, vendors)
            if result is None:
                continue
        key = (result["cost_cents"], mask)
        if best is None or key < best[0]:
            best = (key, result)
    return best[1] if best else None


def _objective(case, best):
    allocation = best["allocation"]
    rows = {(r["vendor"], r["sku"]): r for r in case["quotes"]}
    deadlines = {item["sku"]: item["deadline_day"] for item in case["demand"]}
    lines = []
    max_lateness = 0
    total = 0
    for (vendor, sku), qty in allocation.items():
        row = rows[(vendor, sku)]
        line_late = max(0, row["arrival_day"] - deadlines[sku])
        max_lateness = max(max_lateness, line_late)
        line_total = qty * row["unit_cents"]
        total += line_total
        lines.append(
            {
                "vendor": vendor,
                "sku": sku,
                "qty": qty,
                "unit_cents": row["unit_cents"],
                "line_cents": line_total,
                "arrival_day": row["arrival_day"],
                "deadline_day": deadlines[sku],
                "lateness_days": line_late,
                "available_qty": row["available_qty"],
                "quote_row_sha256": row["row_sha256"],
                "quote_row": {
                    key: row[key] for key in sorted(row) if key not in ("row_sha256",)
                },
            }
        )
    if total != best["cost_cents"]:
        raise ValueError("internal_cost_mismatch")
    return total, max_lateness, sorted(lines, key=lambda line: (line["vendor"], line["sku"]))


def _solve(case, contract):
    if contract not in CONTRACTS:
        raise ValueError(f"unknown_contract:{contract}")
    limits = _candidate_lateness(case["demand"], case["quotes"])
    # Every row is usable at the largest lateness limit; use that candidate set
    # for all limits so that opening a supplier cannot remove rows.
    candidate = {(row["vendor"], row["sku"]) for row in case["quotes"]}
    frontier = []
    solutions = {}
    for limit in limits:
        best = _best_at_limit(case, candidate, limit)
        if best is None:
            frontier.append({"lateness_limit_days": limit, "feasible": False})
            continue
        total, max_lateness, lines = _objective(case, best)
        solutions[limit] = (best, total, max_lateness, lines)
        frontier.append(
            {
                "lateness_limit_days": limit,
                "feasible": True,
                "min_cost_cents": total,
                "max_lateness_days": max_lateness,
                "vendor_mask": best["mask"],
                "allocation": [
                    {"vendor": v, "sku": s, "qty": q}
                    for (v, s), q in best["allocation"].items()
                ],
            }
        )
    if not solutions:
        raise ValueError("no_feasible_order")

    if contract == "min_total_cost":
        global_cost = min(item[1] for item in solutions.values())
        chosen_limit = min(
            limit
            for limit, (best, total, ml, lines) in solutions.items()
            if total == global_cost
        )
        best, total, max_lateness, lines = solutions[chosen_limit]
        if max_lateness > chosen_limit:
            raise ValueError("internal_lateness_mismatch")
        objective = {
            "primary": "total_cost_cents",
            "secondary": "max_lateness_days",
            "total_cost_cents": total,
            "max_lateness_days": max_lateness,
        }
        proof = {
            "chosen_lateness_limit_days": chosen_limit,
            "global_min_cost_cents": global_cost,
            "limits_at_least_as_good": [
                item
                for item in frontier
                if item["lateness_limit_days"] <= chosen_limit
            ],
            "tighter_limits_infeasible_or_costlier": [
                item for item in frontier if item["lateness_limit_days"] < chosen_limit
            ],
        }
    else:
        chosen_limit = min(solutions)
        best, total, max_lateness, lines = solutions[chosen_limit]
        if max_lateness > chosen_limit:
            raise ValueError("internal_lateness_mismatch")
        objective = {
            "primary": "max_lateness_days",
            "secondary": "total_cost_cents",
            "max_lateness_days": max_lateness,
            "total_cost_cents": total,
        }
        proof = {
            "chosen_lateness_limit_days": chosen_limit,
            "tighter_limits_infeasible": [
                item for item in frontier if item["lateness_limit_days"] < chosen_limit
            ],
            "cost_at_chosen_limit_cents": total,
            "best_at_looser_limits": [
                item
                for item in frontier
                if item["lateness_limit_days"] >= chosen_limit
            ],
        }

    # Group selected lines into one order per supplier.
    vendors = sorted({line["vendor"] for line in lines})
    orders = []
    for vendor in vendors:
        vendor_lines = [line for line in lines if line["vendor"] == vendor]
        vendor_total = sum(line["line_cents"] for line in vendor_lines)
        vendor_min = next(
            row["min_order_cents"] for row in case["quotes"] if row["vendor"] == vendor
        )
        if vendor_total < vendor_min:
            raise ValueError(f"min_order_unmet:{vendor}")
        orders.append(
            {
                "vendor": vendor,
                "lines": vendor_lines,
                "total_cents": vendor_total,
                "min_order_cents": vendor_min,
                "latest_arrival_day": max(line["arrival_day"] for line in vendor_lines),
            }
        )

    # Independent completeness checks; a failure means no order is written.
    for item in case["demand"]:
        got = sum(line["qty"] for line in lines if line["sku"] == item["sku"])
        if got != item["qty"]:
            raise ValueError(f"incomplete_demand:{item['sku']}")
    for line in lines:
        if line["qty"] > line["available_qty"] or line["qty"] <= 0:
            raise ValueError(f"capacity_exceeded:{line['vendor']}:{line['sku']}")

    solution = {
        "contract": contract,
        "objective": objective,
        "orders": orders,
        "max_lateness_days": max_lateness,
        "total_cost_cents": total,
    }
    solution_sha = _sha256(_canonical(solution))
    return {
        "solution": solution,
        "solution_sha256": solution_sha,
        "objective": objective,
        "max_lateness_days": max_lateness,
        "total_cost_cents": total,
        "proof": {
            **proof,
            "method": "exhaustive supplier subset and integer allocation enumeration",
            "vendor_masks_examined": 1 << len({q["vendor"] for q in case["quotes"]}),
            "candidate_lateness_days": limits,
            "frontier": frontier,
            "no_better_solution": (
                "Every subset of authorised suppliers was solved by enumerating "
                "all integer allocations of the complete SKU demand; each "
                "allocation was checked jointly against row capacities and each "
                "supplier's minimum-order amount.  The chosen order is therefore "
                "optimal for the frozen lexicographic contract, and identical "
                "input reproduces the same order."
            ),
        },
    }


# ---------------------------------------------------------------------------
# register


def register(db_path, case_id, fixture, *, allow_revision=False):
    """Freeze a completed replacement and the supplier quote file into SQLite."""
    path = Path(fixture).resolve()
    raw = path.read_bytes()
    try:
        doc = json.loads(raw)
    except json.JSONDecodeError:
        raise ValueError("bad_procurement_case") from None
    if (
        not case_id
        or not isinstance(doc, dict)
        or not doc.get("demand")
        or not doc.get("quotes_file")
        or not doc.get("work_order")
    ):
        raise ValueError("bad_procurement_case")
    quote_path = (path.resolve().parent / doc["quotes_file"]).resolve()
    if quote_path.parent != path.resolve().parent:
        # Quotes must be part of the same frozen local case, not an arbitrary
        # path elsewhere on disk.
        raise ValueError("untrusted_quote_source:outside_case")
    quote_raw = quote_path.read_bytes()
    quotes = _read_quotes(quote_raw)
    # Validate demand early so a frozen case is always plannable in principle.
    demand = _normalise_demand(doc)
    _check_demand_against_quotes(demand, quotes)

    db = connect(db_path)
    try:
        with db:
            decision = db.execute(
                "SELECT status,detail FROM decision WHERE work_order=?",
                (doc["work_order"],),
            ).fetchone()
            if not decision or decision["status"] != "完成":
                raise ValueError("consumption_order_not_complete")
            consumed = []
            for e in db.execute(
                "SELECT e.id,e.payload,e.sha256,s.sha256 AS source_sha256 "
                "FROM event e JOIN source s ON s.id=e.source_id "
                "WHERE e.work_order=? AND e.kind='replace'",
                (doc["work_order"],),
            ):
                consumed.append(dict(e))
            if not any(
                isinstance(json.loads(e["payload"]), dict)
                and json.loads(e["payload"]).get("new") == "NEW-1"
                for e in consumed
            ):
                raise ValueError("consumed_part_missing")
            digest = _sha256(raw + quote_raw)
            payload = _canonical(
                {
                    "fixture": str(path),
                    "case": doc,
                    "quotes": quotes,
                    "quote_sha256": _sha256(quote_raw),
                    "decision": dict(decision),
                    "consumed": consumed,
                }
            )
            payload_sha = _sha256(payload)
            old = db.execute(
                "SELECT digest,payload_sha256 FROM procurement_case WHERE id=?",
                (case_id,),
            ).fetchone()
            if old and old["digest"] != digest:
                if not allow_revision:
                    raise ValueError("procurement_case_conflict")
                db.execute(
                    "INSERT INTO procurement_case_revision"
                    "(case_id,previous_digest,previous_payload_sha256,"
                    "new_digest,new_payload_sha256,recorded_at) "
                    "VALUES (?,?,?,?,?,CURRENT_TIMESTAMP)",
                    (
                        case_id,
                        old["digest"],
                        old["payload_sha256"],
                        digest,
                        payload_sha,
                    ),
                )
                db.execute(
                    "UPDATE procurement_case SET payload=?,digest=?,payload_sha256=? "
                    "WHERE id=?",
                    (payload, digest, payload_sha, case_id),
                )
            else:
                db.execute(
                    "INSERT OR IGNORE INTO procurement_case"
                    "(id,payload,digest,payload_sha256) VALUES (?,?,?,?)",
                    (case_id, payload, digest, payload_sha),
                )
    finally:
        db.close()
    return {
        "case_id": case_id,
        "digest": digest,
        "payload_sha256": payload_sha,
        "quotes": len(quotes),
    }


# ---------------------------------------------------------------------------
# plan


def _snapshot(db, plan_id):
    plan = db.execute("SELECT * FROM procurement_plan WHERE id=?", (plan_id,)).fetchone()
    if not plan:
        raise ValueError(f"unknown_plan:{plan_id}")
    orders = []
    for order in db.execute(
        "SELECT * FROM procurement_order WHERE plan_id=? ORDER BY vendor", (plan_id,)
    ):
        lines = [
            dict(line)
            for line in db.execute(
                "SELECT * FROM procurement_order_line WHERE order_id=? ORDER BY sku",
                (order["id"],),
            )
        ]
        for line in lines:
            # quote_row is the frozen signed CSV evidence for this exact line;
            # expose it parsed so the order report is self-contained.
            line["quote_row"] = json.loads(line["quote_row"])
        orders.append(
            {
                "order_id": order["id"],
                "vendor": order["vendor"],
                "work_order": order["work_order"],
                "contract": order["contract"],
                "total_cents": order["total_cents"],
                "min_order_cents": order["min_order_cents"],
                "min_order_met": order["total_cents"] >= order["min_order_cents"],
                "latest_arrival_day": order["latest_arrival_day"],
                "quote_sha256": order["quote_sha256"],
                "case_payload_sha256": order["case_payload_sha256"],
                "consumption": {
                    "event_id": order["consumption_event_id"],
                    "source_sha256": order["consumption_source_sha256"],
                    "payload_sha256": order["consumption_payload_sha256"],
                },
                "status": "issued",
                "lines": lines,
            }
        )
    evidence = json.loads(plan["evidence"])
    retirement = [
        dict(row)
        for row in db.execute(
            "SELECT * FROM procurement_order_retirement WHERE order_id IN "
            "(SELECT id FROM procurement_order WHERE plan_id=?) ORDER BY order_id",
            (plan_id,),
        )
    ]
    return {
        "plan_id": plan["id"],
        "case_id": plan["case_id"],
        "contract": plan["contract"],
        "input_digest": plan["input_digest"],
        "solution_sha256": plan["solution_sha256"],
        "status": plan["status"],
        "total_cost_cents": plan["total_cost_cents"],
        "max_lateness_days": plan["max_lateness_days"],
        "objective": json.loads(plan["objective"]),
        "created_at": plan["created_at"],
        "evidence": evidence,
        "orders": orders,
        "replaced_orders": retirement if plan["status"] == "superseded" else [],
    }


def _delta(old_snapshot, new_snapshot):
    def by_sku(snap):
        out = defaultdict(int)
        for order in snap["orders"]:
            for line in order["lines"]:
                out[line["sku"]] += line["qty"]
        return dict(out)

    def by_vendor(snap):
        out = {}
        for order in snap["orders"]:
            out[order["vendor"]] = {
                "total_cents": order["total_cents"],
                "order_id": order["order_id"],
            }
        return out

    old_sku, new_sku = by_sku(old_snapshot), by_sku(new_snapshot)
    skus = sorted(set(old_sku) | set(new_sku))
    sku_delta = {
        sku: {
            "old_qty": old_sku.get(sku, 0),
            "new_qty": new_sku.get(sku, 0),
            "delta_qty": new_sku.get(sku, 0) - old_sku.get(sku, 0),
        }
        for sku in skus
    }
    old_vendor, new_vendor = by_vendor(old_snapshot), by_vendor(new_snapshot)
    vendors = sorted(set(old_vendor) | set(new_vendor))
    vendor_delta = {
        vendor: {
            "old_total_cents": old_vendor.get(vendor, {}).get("total_cents", 0),
            "new_total_cents": new_vendor.get(vendor, {}).get("total_cents", 0),
            "delta_cents": new_vendor.get(vendor, {}).get("total_cents", 0)
            - old_vendor.get(vendor, {}).get("total_cents", 0),
        }
        for vendor in vendors
    }
    old_orders = [
        {
            "order_id": order["order_id"],
            "vendor": order["vendor"],
            "total_cents": order["total_cents"],
        }
        for order in old_snapshot["orders"]
    ]
    return {
        "supersedes_plan_id": old_snapshot["plan_id"],
        "cancel_orders": old_orders,
        "issue_orders": [
            {
                "order_id": order["order_id"],
                "vendor": order["vendor"],
                "total_cents": order["total_cents"],
            }
            for order in new_snapshot["orders"]
        ],
        "sku_quantity_delta": sku_delta,
        "vendor_amount_delta": vendor_delta,
        "instruction": (
            f"旧计划 {old_snapshot['plan_id']} 的订单必须整单取消，"
            f"以新计划 {new_snapshot['plan_id']} 的订单替代；"
            "不得把两份订单的数量或金额相加当作同一补货需求。"
        ),
    }


def plan(db_path, case_id, contract):
    """Issue an exact complete purchase plan; never writes a partial order."""
    if contract not in CONTRACTS:
        raise ValueError(f"unknown_contract:{contract}")
    db = connect(db_path)
    try:
        with db:
            case = _load_case(db, case_id)
            authored = case["case"].get("contracts")
            if authored is not None and contract not in authored:
                raise ValueError(f"contract_not_authorised:{contract}")
            solved = _solve(case, contract)
            input_digest = _sha256(
                _canonical(
                    {
                        "case_payload_sha256": case["payload_sha256"],
                        "contract": contract,
                    }
                )
            )
            old = db.execute(
                "SELECT * FROM procurement_plan WHERE case_id=? AND contract=? "
                "AND status='issued'",
                (case_id, contract),
            ).fetchone()
            if old and old["solution_sha256"] == solved["solution_sha256"]:
                snap = _snapshot(db, old["id"])
                snap["replanned"] = False
                snap["input_changed"] = old["input_digest"] != input_digest
                snap["order_unchanged"] = True
                if snap["input_changed"]:
                    snap["revision_note"] = (
                        "冻结输入已更新，但最优订单未变化；已签发订单保持有效，"
                        "不重复下单。"
                    )
                return snap

            previous_snapshot = _snapshot(db, old["id"]) if old else None
            if old:
                db.execute(
                    "UPDATE procurement_plan SET status='superseded' WHERE id=?",
                    (old["id"],),
                )
                for order in db.execute(
                    "SELECT id FROM procurement_order WHERE plan_id=?", (old["id"],)
                ):
                    db.execute(
                        "INSERT INTO procurement_order_retirement"
                        "(order_id,plan_id,reason,created_at) "
                        "VALUES (?,?,?,CURRENT_TIMESTAMP)",
                        (
                            order["id"],
                            old["id"],
                            f"superseded_by_plan_for:{case_id}:{contract}",
                        ),
                    )

            # Prefer a consumed replacement event for a demanded SKU.
            demanded = {item["sku"] for item in case["demand"]}
            primary = next(
                (
                    item
                    for item in case["consumption"]
                    if item["new_part"] in demanded
                ),
                case["consumption"][0],
            )
            evidence = {
                "planner_version": PLANNER_VERSION,
                "case_id": case_id,
                "work_order": case["work_order"],
                "case_payload_sha256": case["payload_sha256"],
                "quote_sha256": case["quote_sha256"],
                "planning_input_digest": input_digest,
                "consumption": case["consumption"],
                "demand": case["demand"],
                "quotes": [
                    {
                        "vendor": row["vendor"],
                        "sku": row["sku"],
                        "unit_cents": row["unit_cents"],
                        "available_qty": row["available_qty"],
                        "arrival_day": row["arrival_day"],
                        "min_order_cents": row["min_order_cents"],
                        "row_sha256": row["row_sha256"],
                    }
                    for row in case["quotes"]
                ],
                "objective": solved["objective"],
                "optimality": solved["proof"],
                "supersedes_plan_id": old["id"] if old else None,
            }
            plan_id = db.execute(
                "INSERT INTO procurement_plan"
                "(case_id,contract,input_digest,solution_sha256,status,"
                "total_cost_cents,max_lateness_days,objective,evidence,created_at) "
                "VALUES (?,?,?,?,'issued',?,?,?,?,CURRENT_TIMESTAMP)",
                (
                    case_id,
                    contract,
                    input_digest,
                    solved["solution_sha256"],
                    solved["total_cost_cents"],
                    solved["max_lateness_days"],
                    _canonical(solved["objective"]),
                    _canonical(evidence),
                ),
            ).lastrowid
            for order in solved["solution"]["orders"]:
                order_id = db.execute(
                    "INSERT INTO procurement_order"
                    "(plan_id,vendor,work_order,contract,total_cents,"
                    "min_order_cents,latest_arrival_day,quote_sha256,"
                    "case_payload_sha256,consumption_event_id,"
                    "consumption_source_sha256,consumption_payload_sha256,created_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)",
                    (
                        plan_id,
                        order["vendor"],
                        case["work_order"],
                        contract,
                        order["total_cents"],
                        order["min_order_cents"],
                        order["latest_arrival_day"],
                        case["quote_sha256"],
                        case["payload_sha256"],
                        primary["event_id"],
                        primary["source_sha256"],
                        primary["payload_sha256"],
                    ),
                ).lastrowid
                for line in order["lines"]:
                    db.execute(
                        "INSERT INTO procurement_order_line"
                        "(order_id,sku,qty,unit_cents,line_cents,arrival_day,"
                        "deadline_day,lateness_days,available_qty,"
                        "quote_row_sha256,quote_row) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            order_id,
                            line["sku"],
                            line["qty"],
                            line["unit_cents"],
                            line["line_cents"],
                            line["arrival_day"],
                            line["deadline_day"],
                            line["lateness_days"],
                            line["available_qty"],
                            line["quote_row_sha256"],
                            _canonical(line["quote_row"]),
                        ),
                    )
            snap = _snapshot(db, plan_id)
            snap["replanned"] = True
            snap["input_changed"] = bool(old)
            if previous_snapshot:
                changes = _delta(previous_snapshot, snap)
                evidence["changes"] = changes
                db.execute(
                    "UPDATE procurement_plan SET evidence=? WHERE id=?",
                    (_canonical(evidence), plan_id),
                )
                snap["evidence"] = evidence
                snap["changes"] = changes
            return snap
    finally:
        db.close()


# ---------------------------------------------------------------------------
# report / offline command helper


def procurement_report(db_path, out, case_id=None):
    db = connect(db_path)
    try:
        where, args = ("WHERE case_id=?", (case_id,)) if case_id else ("", ())
        plan_ids = [
            row["id"]
            for row in db.execute(
                f"SELECT id FROM procurement_plan {where} ORDER BY case_id,contract,id",
                args,
            )
        ]
        plans = [_snapshot(db, plan_id) for plan_id in plan_ids]
        cases = []
        for row in db.execute(
            f"SELECT * FROM procurement_case {where} ORDER BY id", args
        ):
            try:
                frozen = json.loads(row["payload"])
            except (TypeError, json.JSONDecodeError):
                frozen = {}
            cases.append(
                {
                    "case_id": row["id"],
                    "digest": row["digest"],
                    "payload_sha256": row["payload_sha256"],
                    "case": frozen.get("case", {}),
                    "quote_sha256": frozen.get("quote_sha256"),
                }
            )
        revisions = [
            dict(row)
            for row in db.execute(
                f"SELECT * FROM procurement_case_revision {where} ORDER BY id", args
            )
        ] if case_id else [
            dict(row) for row in db.execute(
                "SELECT * FROM procurement_case_revision ORDER BY id"
            )
        ]
        purchase_targets = []
        issued_plans = [plan for plan in plans if plan["status"] == "issued"]
        for case in cases:
            try:
                demand = {
                    item["sku"]: item["qty"]
                    for item in _normalise_demand(case["case"])
                }
            except ValueError:
                demand = {}
            purchase_targets.append(
                {"case_id": case["case_id"], "demand": demand}
            )
        # Purchases are recorded per issued contract; contracts are alternative
        # answers to one demand and must never be added together.
        purchases = [
            {
                "case_id": plan["case_id"],
                "contract": plan["contract"],
                "plan_id": plan["plan_id"],
                "orders": [
                    {
                        "order_id": order["order_id"],
                        "vendor": order["vendor"],
                        "total_cents": order["total_cents"],
                        "lines": [
                            {"sku": line["sku"], "qty": line["qty"]}
                            for line in order["lines"]
                        ],
                    }
                    for order in plan["orders"]
                ],
            }
            for plan in issued_plans
        ]
        # The two contracts are alternative answers to one demand.  They are
        # compared side by side below and never summed into one purchase.
        tradeoffs = []
        for case in cases:
            issued = {
                p["contract"]: p
                for p in plans
                if p["case_id"] == case["case_id"] and p["status"] == "issued"
            }
            if len(issued) == 2:
                cheap = issued["min_total_cost"]
                fast = issued["min_max_lateness"]
                if (
                    cheap["max_lateness_days"] > fast["max_lateness_days"]
                    and cheap["total_cost_cents"] < fast["total_cost_cents"]
                ):
                    tradeoff = (
                        f"最低成本合同 {cheap['plan_id']} 总价 {cheap['total_cost_cents']} 分，"
                        f"最大缺料 {cheap['max_lateness_days']} 天；"
                        f"最短缺料合同 {fast['plan_id']} 总价 {fast['total_cost_cents']} 分，"
                        f"最大缺料 {fast['max_lateness_days']} 天。"
                        f"选择后者需多付 "
                        f"{fast['total_cost_cents'] - cheap['total_cost_cents']} 分，"
                        f"换取缩短 {cheap['max_lateness_days'] - fast['max_lateness_days']} 天缺料。"
                    )
                elif cheap["total_cost_cents"] == fast["total_cost_cents"]:
                    tradeoff = (
                        "两种合同得到同一订单：总价与最大缺料同时最优，不存在取舍。"
                    )
                else:
                    tradeoff = (
                        f"两种合同分别为 {cheap['total_cost_cents']} 分/"
                        f"{cheap['max_lateness_days']} 天与 {fast['total_cost_cents']} 分/"
                        f"{fast['max_lateness_days']} 天；"
                        "两者互不支配，须由仓方按授权目标选择。"
                    )
                tradeoffs.append(
                    {
                        "case_id": case["case_id"],
                        "min_total_cost": {
                            "plan_id": cheap["plan_id"],
                            "total_cents": cheap["total_cost_cents"],
                            "max_lateness_days": cheap["max_lateness_days"],
                            "vendors": [o["vendor"] for o in cheap["orders"]],
                            "lines": [
                                {
                                    "vendor": o["vendor"],
                                    "sku": line["sku"],
                                    "qty": line["qty"],
                                }
                                for o in cheap["orders"]
                                for line in o["lines"]
                            ],
                        },
                        "min_max_lateness": {
                            "plan_id": fast["plan_id"],
                            "total_cents": fast["total_cost_cents"],
                            "max_lateness_days": fast["max_lateness_days"],
                            "vendors": [o["vendor"] for o in fast["orders"]],
                            "lines": [
                                {
                                    "vendor": o["vendor"],
                                    "sku": line["sku"],
                                    "qty": line["qty"],
                                }
                                for o in fast["orders"]
                                for line in o["lines"]
                            ],
                        },
                        "real_tradeoff": tradeoff,
                    }
                )
        data = {
            "generated_from": str(Path(db_path).resolve()),
            "cases": cases,
            "revisions": revisions,
            "plans": plans,
            "purchase_targets": purchase_targets,
            "purchases": purchases,
            "tradeoffs": tradeoffs,
            "immutability": {
                "issued_orders": "append-only; revisions supersede whole plans",
                "revision_instruction": "never add superseded and new orders as one demand",
            },
        }
        path = Path(out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return path
    finally:
        db.close()
