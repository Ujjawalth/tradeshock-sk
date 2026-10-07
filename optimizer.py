"""Linear-programming crop-mix optimizer (PuLP + CBC).

Objective: maximize return over variable costs
    sum_c acres_c * (yield_c * price_c - variable_cost_c)
Fixed costs do not change with the crop mix, so they don't change the best
mix; we still report return over total costs.

Run directly for a quick check:
    python optimizer.py --zone "Dark Brown" --acres 2000
"""
from __future__ import annotations

import argparse
import json
from functools import lru_cache
from pathlib import Path

import pulp

from models import CropBudget, FarmProfile

DATA_PATH = Path(__file__).parent / "data" / "crop_budgets_2026.json"
EPS = 1e-6


class PlanError(Exception):
    """Raised when a plan can't be built (bad input or infeasible limits)."""


# ---------------------------------------------------------------- data
@lru_cache(maxsize=4)
def load_budgets(path: str = str(DATA_PATH)) -> tuple[dict, tuple[CropBudget, ...]]:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    rows = tuple(CropBudget(**r) for r in raw["rows"])
    return raw.get("meta", {}), rows


def budgets_for_zone(zone: str, rows=None) -> list[CropBudget]:
    rows = rows if rows is not None else load_budgets()[1]
    return [r for r in rows if r.soil_zone == zone]


# ---------------------------------------------------------------- helpers
def _shocked_price(b: CropBudget, price_changes: dict[str, float]) -> float:
    return b.price * (1 + price_changes.get(b.crop, 0.0) / 100.0)


def _round_acres(values: dict[str, float], total: int) -> dict[str, int]:
    """Round to whole acres while keeping the exact total (largest remainder)."""
    floors = {k: int(v + EPS) for k, v in values.items()}
    short = total - sum(floors.values())
    order = sorted(values, key=lambda k: values[k] - floors[k], reverse=True)
    for k in order[:max(0, short)]:
        floors[k] += 1
    return floors


def _explain_infeasible(budgets: list[CropBudget], profile: FarmProfile) -> str:
    lim = profile.limits
    has_cereal = any(b.crop_group == "cereal" for b in budgets)
    if lim.cereals_min_pct > 0 and not has_cereal:
        return "Cereals minimum is above 0% but no cereal crop is allowed."
    # Max share the allowed crops can cover under the caps
    cap = 0.0
    pulse_room = lim.pulses_max_pct
    for b in budgets:
        c = lim.single_crop_max_pct
        if b.crop == "Canola":
            c = min(c, lim.canola_max_pct)
        if b.crop_group == "pulse":
            c = min(c, pulse_room)
            pulse_room -= c
        cap += c
    if cap < 100 - EPS:
        return (f"The allowed crops can cover at most {cap:.0f}% of acres under the "
                "rotation limits. Allow more crops or relax the limits.")
    return "Rotation limits conflict with each other. Relax one of them."


# ---------------------------------------------------------------- core LP
def _new_var(prob, name: str):
    """Non-negative continuous variable (works on PuLP 3.x and the 4.x API)."""
    if hasattr(prob, "add_variable"):
        return prob.add_variable(name, lowBound=0)
    return pulp.LpVariable(name, lowBound=0)


def allowed_budgets(profile: FarmProfile, rows=None) -> list[CropBudget]:
    budgets = budgets_for_zone(profile.soil_zone, rows)
    if profile.crops_allowed is not None:
        allowed = set(profile.crops_allowed)
        budgets = [b for b in budgets if b.crop in allowed]
    if not budgets:
        raise PlanError("No crops selected for this soil zone.")
    return budgets


def margins(budgets: list[CropBudget], price_changes: dict[str, float]) -> dict[str, float]:
    """Return over variable cost per acre, per crop, at (shocked) prices."""
    return {b.crop: b.target_yield * _shocked_price(b, price_changes) - b.variable_cost_per_acre
            for b in budgets}


def add_rotation_constraints(prob, x: dict, budgets: list[CropBudget], profile: FarmProfile) -> None:
    """Acres add up + rotation limits. Shared by every optimizer (return, robust, risk)."""
    A, lim = profile.total_acres, profile.limits
    prob += pulp.lpSum(x.values()) == A, "all_acres"
    for i, b in enumerate(budgets):
        prob += x[b.crop] <= A * lim.single_crop_max_pct / 100, f"single_{i}"
    if "Canola" in x:
        prob += x["Canola"] <= A * lim.canola_max_pct / 100, "canola_max"
    pulses = [x[b.crop] for b in budgets if b.crop_group == "pulse"]
    if pulses:
        prob += pulp.lpSum(pulses) <= A * lim.pulses_max_pct / 100, "pulses_max"
    cereals = [x[b.crop] for b in budgets if b.crop_group == "cereal"]
    if lim.cereals_min_pct > 0:
        prob += pulp.lpSum(cereals) >= A * lim.cereals_min_pct / 100, "cereals_min"


def solve_lp(prob, x: dict, budgets: list[CropBudget], profile: FarmProfile) -> dict[str, float]:
    """Solve and return whole-acre allocation of planted crops."""
    status = prob.solve(pulp.PULP_CBC_CMD(msg=False))
    if pulp.LpStatus[status] != "Optimal":
        raise PlanError(_explain_infeasible(budgets, profile))
    A = profile.total_acres
    exact = {c: max(0.0, v.value() or 0.0) for c, v in x.items()}
    acres = _round_acres(exact, int(round(A))) if float(A).is_integer() else exact
    return {c: a for c, a in acres.items() if a > EPS}  # only crops actually planted


def solve(profile: FarmProfile, price_changes: dict[str, float] | None = None,
          rows=None) -> dict:
    """Return the best crop mix for a farm profile and optional price changes (%)."""
    price_changes = price_changes or {}
    budgets = allowed_budgets(profile, rows)
    prob = pulp.LpProblem("crop_mix", pulp.LpMaximize)
    x = {b.crop: _new_var(prob, f"acres_{i}") for i, b in enumerate(budgets)}
    m = margins(budgets, price_changes)
    prob += pulp.lpSum(m[c] * x[c] for c in x)
    add_rotation_constraints(prob, x, budgets, profile)
    return evaluate(profile, solve_lp(prob, x, budgets, profile), price_changes, rows)


def solve_robust(profile: FarmProfile, cases: dict[str, dict[str, float]], rows=None) -> dict[str, float]:
    """Max-min plan: the mix whose WORST return across the cases is highest."""
    budgets = allowed_budgets(profile, rows)
    prob = pulp.LpProblem("robust_mix", pulp.LpMaximize)
    x = {b.crop: _new_var(prob, f"acres_{i}") for i, b in enumerate(budgets)}
    t = pulp.LpVariable("worst_case_return")
    prob += t
    for k, (name, changes) in enumerate(cases.items()):
        m = margins(budgets, changes)
        prob += t <= pulp.lpSum(m[c] * x[c] for c in x), f"case_{k}"
    add_rotation_constraints(prob, x, budgets, profile)
    return solve_lp(prob, x, budgets, profile)


def solve_min_regret(profile: FarmProfile, cases: dict[str, dict[str, float]],
                     best: dict[str, float], rows=None) -> dict[str, float]:
    """Minimax-regret plan: smallest worst-case shortfall vs the best plan for each case."""
    budgets = allowed_budgets(profile, rows)
    prob = pulp.LpProblem("regret_mix", pulp.LpMinimize)
    x = {b.crop: _new_var(prob, f"acres_{i}") for i, b in enumerate(budgets)}
    r = pulp.LpVariable("max_regret")
    prob += r
    for k, (name, changes) in enumerate(cases.items()):
        m = margins(budgets, changes)
        prob += r >= best[name] - pulp.lpSum(m[c] * x[c] for c in x), f"regret_{k}"
    add_rotation_constraints(prob, x, budgets, profile)
    return solve_lp(prob, x, budgets, profile)


def stress_cases(profile: FarmProfile, cases: dict[str, dict[str, float]], rows=None) -> dict:
    """Score case-optimal, max-min and minimax-regret plans in every case.

    Plans with the same crop mix are merged (labels joined), so the table stays short.
    """
    candidates = [(f"{name}_plan", acres_map(solve(profile, ch, rows))) for name, ch in cases.items()]
    best = {name: evaluate(profile, acres, cases[name], rows)["totals"]["return_over_variable"]
            for (label, acres), name in zip(candidates, cases)}
    candidates.append(("maxmin_plan", solve_robust(profile, cases, rows)))
    candidates.append(("regret_plan", solve_min_regret(profile, cases, best, rows)))

    merged: dict[tuple, dict] = {}
    for label, acres in candidates:
        key = tuple(sorted((c, round(a)) for c, a in acres.items()))
        if key in merged:
            merged[key]["labels"].append(label)
            continue
        row = {"labels": [label], "acres": acres, "returns": {}, "regret": {}}
        for name, ch in cases.items():
            ret = evaluate(profile, acres, ch, rows)["totals"]["return_over_variable"]
            row["returns"][name] = ret
            row["regret"][name] = round(max(0.0, best[name] - ret), 2)
        row["worst_return"] = min(row["returns"].values())
        row["max_regret"] = max(row["regret"].values())
        merged[key] = row
    table = list(merged.values())
    return {"cases": cases, "best_by_case": best, "plans": table}


def evaluate(profile: FarmProfile, acres: dict[str, float],
             price_changes: dict[str, float] | None = None, rows=None) -> dict:
    """Score a fixed crop mix at (possibly shocked) prices."""
    price_changes = price_changes or {}
    budgets = {b.crop: b for b in budgets_for_zone(profile.soil_zone, rows)}
    crops, tot_rev, tot_var, tot_cost = [], 0.0, 0.0, 0.0
    A = profile.total_acres
    for crop, a in acres.items():
        b = budgets[crop]
        price = _shocked_price(b, price_changes)
        rev = b.target_yield * price
        crops.append({
            "crop": crop,
            "crop_group": b.crop_group,
            "acres": a,
            "share_pct": round(100 * a / A, 1),
            "unit": b.unit,
            "target_yield": b.target_yield,
            "base_price": b.price,
            "price": round(price, 4),
            "price_change_pct": round(price_changes.get(crop, 0.0), 1),
            "revenue_per_acre": round(rev, 2),
            "variable_cost_per_acre": b.variable_cost_per_acre,
            "total_cost_per_acre": b.total_cost_per_acre,
            "return_over_variable_per_acre": round(rev - b.variable_cost_per_acre, 2),
            "return_over_total_per_acre": round(rev - b.total_cost_per_acre, 2),
            "breakeven_price": round(b.variable_cost_per_acre / b.target_yield, 4),
        })
        tot_rev += a * rev
        tot_var += a * b.variable_cost_per_acre
        tot_cost += a * b.total_cost_per_acre
    crops.sort(key=lambda r: (-r["acres"], r["crop"]))
    return {
        "soil_zone": profile.soil_zone,
        "total_acres": A,
        "crops": crops,
        "totals": {
            "revenue": round(tot_rev, 2),
            "variable_cost": round(tot_var, 2),
            "total_cost": round(tot_cost, 2),
            "return_over_variable": round(tot_rev - tot_var, 2),
            "return_over_total": round(tot_rev - tot_cost, 2),
            "return_over_variable_per_acre": round((tot_rev - tot_var) / A, 2),
        },
    }


def acres_map(plan: dict) -> dict[str, float]:
    return {r["crop"]: r["acres"] for r in plan["crops"]}


# ---------------------------------------------------------------- comparisons
def compare(profile: FarmProfile, price_changes: dict[str, float], rows=None) -> dict:
    """Baseline vs shock. Also scores 'stand still': keep the baseline mix at shock prices."""
    baseline = solve(profile, None, rows)
    shock = solve(profile, price_changes, rows)
    stand_still = evaluate(profile, acres_map(baseline), price_changes, rows)

    base_acres, shock_acres = acres_map(baseline), acres_map(shock)
    all_crops = sorted(set(base_acres) | set(shock_acres),
                       key=lambda c: -(base_acres.get(c, 0) + shock_acres.get(c, 0)))
    changes = [{
        "crop": c,
        "baseline_acres": base_acres.get(c, 0),
        "shock_acres": shock_acres.get(c, 0),
        "change_acres": round(shock_acres.get(c, 0) - base_acres.get(c, 0), 1),
        "price_change_pct": round(price_changes.get(c, 0.0), 1),
    } for c in all_crops if base_acres.get(c, 0) or shock_acres.get(c, 0)]

    b, s, st = (p["totals"]["return_over_variable"] for p in (baseline, shock, stand_still))
    return {
        "baseline": baseline,
        "shock": shock,
        "stand_still": stand_still,
        "changes": changes,
        "summary": {
            "baseline_return": b,
            "shock_return": s,
            "stand_still_return": st,
            "change_vs_baseline": round(s - b, 2),
            "change_vs_baseline_pct": round(100 * (s - b) / b, 1) if b else None,
            "value_of_replanning": round(s - st, 2),
            "mix_changed": any(abs(c["change_acres"]) >= 1 for c in changes),
        },
    }


def sensitivity(profile: FarmProfile, crop: str, base_changes: dict[str, float] | None = None,
                lo: int = -60, hi: int = 60, step: int = 5, rows=None) -> dict:
    """Sweep one crop's price change and find where the best mix flips (tipping points)."""
    base_changes = dict(base_changes or {})
    points, tipping, prev_mix = [], [], None
    for pct in range(lo, hi + 1, step):
        changes = {**base_changes, crop: float(pct)}
        try:
            plan = solve(profile, changes, rows)
        except PlanError:
            continue
        acres = acres_map(plan)
        mix = tuple(sorted((c, round(a)) for c, a in acres.items() if a >= 1))
        points.append({
            "price_change_pct": pct,
            "return_over_variable": plan["totals"]["return_over_variable"],
            "crop_acres": acres.get(crop, 0),
        })
        if prev_mix is not None and {c for c, _ in mix} != {c for c, _ in prev_mix}:
            tipping.append({
                "price_change_pct": pct,
                "crops_in": sorted({c for c, _ in mix} - {c for c, _ in prev_mix}),
                "crops_out": sorted({c for c, _ in prev_mix} - {c for c, _ in mix}),
            })
        prev_mix = mix
    return {"crop": crop, "points": points, "tipping_points": tipping}


# ---------------------------------------------------------------- CLI
if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Print a baseline crop plan")
    ap.add_argument("--zone", default="Dark Brown", choices=["Brown", "Dark Brown", "Black"])
    ap.add_argument("--acres", type=float, default=2000)
    ap.add_argument("--shock", default="", help='e.g. "Canola=-20,Yellow Peas=-10"')
    args = ap.parse_args()

    prof = FarmProfile(soil_zone=args.zone, total_acres=args.acres)
    changes = {k.strip(): float(v) for k, v in
               (p.split("=") for p in args.shock.split(",") if "=" in p)}
    meta = load_budgets()[0]
    if meta.get("status") != "VERIFIED":
        print("!! Budget data status:", meta.get("status"), "- numbers are UNVERIFIED placeholders\n")
    result = compare(prof, changes) if changes else {"baseline": solve(prof)}
    for name, plan in result.items():
        if not isinstance(plan, dict) or "crops" not in plan:
            continue
        print(f"== {name} ({plan['soil_zone']}, {plan['total_acres']:.0f} ac)")
        for r in plan["crops"]:
            print(f"  {r['crop']:<12} {r['acres']:>7} ac  {r['share_pct']:>5}%  "
                  f"${r['return_over_variable_per_acre']:>8.2f}/ac")
        print(f"  Return over variable costs: ${plan['totals']['return_over_variable']:,.0f}\n")
    if "summary" in result:
        print(json.dumps(result["summary"], indent=2))
