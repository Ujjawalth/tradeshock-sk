"""Risk engine: price risk and (optionally) yield risk from real Saskatchewan data.

Price:  every historical 12-month price move (data/price_model.json, Statistics
        Canada Table 32-10-0077), joint across crops.
Yield:  every detrended provincial yield year 1991-2025 (data/yield_model.json,
        Statistics Canada Table 32-10-0359), joint across crops.

With yield risk on, every price move is combined with every yield year
(ASSUMPTION: price and yield swings independent - conservative, since bad
harvests often lift prices). Both factors average 1, so the expected return
equals the deterministic plan exactly.

    return_s = sum_c acres_c * (yield_c * yf_sc * price_c * (1 + shock_c) * pf_sc - var_cost_c)

A risk-averse plan comes from a mean-CVaR linear program (Rockafellar & Uryasev, 2000):

    max  (1 - lam) * E[R(x)] + lam * CVaR_alpha(R(x))
    CVaR_alpha(R) = max_z  z - 1/(alpha*S) * sum_s max(0, z - R_s)
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

import numpy as np
import pulp

from models import FarmProfile
from optimizer import (PlanError, _new_var, add_rotation_constraints, allowed_budgets,
                       budgets_for_zone, evaluate, solve, solve_lp)

DATA = Path(__file__).parent / "data"
MODEL_PATH = DATA / "price_model.json"
YIELD_PATH = DATA / "yield_model.json"
ALPHA = 0.10           # "bad year" = average of the worst 10% of scenarios
LP_MAX_SCENARIOS = 1200  # CVaR LP uses an evenly spaced subset of the scenario grid (speed)


@lru_cache(maxsize=4)
def _load(path: str) -> dict | None:
    p = Path(path)
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def load_model() -> dict | None:
    return _load(str(MODEL_PATH))


def load_yield_model() -> dict | None:
    return _load(str(YIELD_PATH))


def available() -> bool:
    return load_model() is not None


def yield_available() -> bool:
    return load_yield_model() is not None


def _month(ym: str) -> str:
    import calendar
    y, m = ym.split("-")
    return f"{calendar.month_abbr[int(m)]} {y}"


@lru_cache(maxsize=4)
def _factor_grid(crops: tuple[str, ...], include_yield: bool):
    """Price factors PF[s, c], yield factors YF[s, c] and labels for every scenario."""
    pm = load_model()
    windows = pm["windows"]
    pf = np.array([[np.exp(w["log_change"].get(c, 0.0)) for c in crops] for w in windows])
    pf = pf / pf.mean(axis=0)  # mean 1 -> expected price = guide price
    plabels = [f"prices {_month(w['start'])}→{_month(str(int(w['start'][:4]) + 1) + w['start'][4:])}"
               for w in windows]
    ym = load_yield_model() if include_yield else None
    if ym is None:
        return pf, np.ones_like(pf), plabels
    years = ym["years"]
    yf = np.array([[y["factor"].get(c, 1.0) for c in crops] for y in years])
    yf = yf / yf.mean(axis=0)
    P, Y = len(windows), len(years)
    PF = np.repeat(pf, Y, axis=0)          # every price move ...
    YF = np.tile(yf, (P, 1))               # ... with every yield year
    labels = [f"{plabels[i]} + {years[j]['year']} yields" for i in range(P) for j in range(Y)]
    return PF, YF, labels


def _scenario_margins(profile: FarmProfile, price_changes: dict[str, float], crops: list[str],
                      include_yield: bool):
    """M[s, c] = per-acre return over variable cost of crop c in scenario s."""
    budgets = {b.crop: b for b in budgets_for_zone(profile.soil_zone)}
    PF, YF, labels = _factor_grid(tuple(crops), include_yield)
    base_rev = np.array([budgets[c].target_yield * budgets[c].price * (1 + price_changes.get(c, 0.0) / 100)
                         for c in crops])
    var = np.array([budgets[c].variable_cost_per_acre for c in crops])
    return PF * YF * base_rev - var, labels


def _histogram(values: np.ndarray, edges: np.ndarray) -> list[int]:
    counts, _ = np.histogram(values, bins=edges)
    return counts.tolist()


def risk_of_mix(profile: FarmProfile, acres: dict[str, float], price_changes: dict[str, float],
                alpha: float = ALPHA, include_yield: bool = False) -> dict:
    """Distribution of farm return for a fixed crop mix across all scenarios."""
    crops = list(acres)
    M, labels = _scenario_margins(profile, price_changes, crops, include_yield)
    x = np.array([acres[c] for c in crops])
    R = M @ x                                  # return over variable costs, per scenario
    k = max(1, int(np.ceil(alpha * len(R))))
    order = np.argsort(R)
    worst = order[0]
    return {
        "expected": float(R.mean()),
        "bad_year": float(R[order[:k]].mean()),       # CVaR: average of worst alpha share
        "worst": float(R[worst]),
        "worst_label": labels[worst],
        "best": float(R.max()),
        "p10": float(np.percentile(R, 10)),
        "p90": float(np.percentile(R, 90)),
        "chance_below_zero_pct": float(100 * (R < 0).mean()),
        "samples": R,
    }


def optimize(profile: FarmProfile, price_changes: dict[str, float], lam: float,
             alpha: float = ALPHA, include_yield: bool = False) -> dict:
    """Risk-aware plan. lam=0 -> max expected return; lam=1 -> max bad-year return."""
    budgets = allowed_budgets(profile)
    crops = [b.crop for b in budgets]
    M, _ = _scenario_margins(profile, price_changes, crops, include_yield)
    if len(M) > LP_MAX_SCENARIOS:  # evenly spaced subset keeps the LP fast and deterministic
        M = M[np.linspace(0, len(M) - 1, LP_MAX_SCENARIOS).astype(int)]
    S = M.shape[0]

    prob = pulp.LpProblem("risk_mix", pulp.LpMaximize)
    x = {c: _new_var(prob, f"x_{j}") for j, c in enumerate(crops)}
    z = pulp.LpVariable("z")  # free variable: the VaR level in the CVaR formula
    u = [_new_var(prob, f"u_{s}") for s in range(S)]
    mean_margin = M.mean(axis=0)

    expected = pulp.lpSum(float(mean_margin[j]) * x[c] for j, c in enumerate(crops))
    cvar = z - (1.0 / (alpha * S)) * pulp.lpSum(u)
    prob += (1 - lam) * expected + lam * cvar
    for s in range(S):
        prob += u[s] >= z - pulp.lpSum(float(M[s, j]) * x[c] for j, c in enumerate(crops))
    add_rotation_constraints(prob, x, budgets, profile)
    return solve_lp(prob, x, budgets, profile)


def _public(r: dict) -> dict:
    return {k: (round(v, 2) if isinstance(v, float) else v) for k, v in r.items() if k != "samples"}


def analyze(profile: FarmProfile, price_changes: dict[str, float], risk_aversion: float = 0.5,
            with_frontier: bool = False, include_yield: bool = True) -> dict:
    """Compare the return-max plan with a risk-aware plan (optionally a risk/return frontier)."""
    if not available():
        raise PlanError("Price-risk model not found. Run scripts/fit_price_model.py.")
    include_yield = include_yield and yield_available()
    lam = max(0.0, min(1.0, float(risk_aversion)))
    max_plan = solve(profile, price_changes)
    max_acres = {r["crop"]: r["acres"] for r in max_plan["crops"]}
    safe_acres = optimize(profile, price_changes, lam, include_yield=include_yield)
    r_max = risk_of_mix(profile, max_acres, price_changes, include_yield=include_yield)
    r_safe = risk_of_mix(profile, safe_acres, price_changes, include_yield=include_yield)

    lo = min(r_max["samples"].min(), r_safe["samples"].min())
    hi = max(r_max["samples"].max(), r_safe["samples"].max())
    edges = np.linspace(lo, hi, 21)

    frontier = []
    for l in ((0.0, 0.25, 0.5, 0.75, 1.0) if with_frontier else ()):
        acres = optimize(profile, price_changes, l, include_yield=include_yield)
        r = risk_of_mix(profile, acres, price_changes, include_yield=include_yield)
        frontier.append({"risk_aversion": l, "expected": round(r["expected"], 2),
                         "bad_year": round(r["bad_year"], 2),
                         "mix": {c: a for c, a in sorted(acres.items(), key=lambda kv: -kv[1])}})

    safe_plan = evaluate(profile, safe_acres, price_changes)
    pm, ym = load_model(), load_yield_model()
    sources = [{"kind": "price", **{k: pm["meta"][k] for k in ("source", "url", "method", "window", "note")},
                "n": pm["meta"]["n_windows"]}]
    if include_yield:
        sources.append({"kind": "yield", **{k: ym["meta"][k] for k in ("source", "url", "method", "window", "note")},
                        "n": ym["meta"]["n_years"]})
    return {
        "risk_aversion": lam,
        "alpha_pct": int(ALPHA * 100),
        "include_yield": include_yield,
        "n_scenarios": len(r_max["samples"]),
        "max_return_plan": {"acres": max_acres, "risk": _public(r_max)},
        "risk_aware_plan": {"acres": {r["crop"]: r["acres"] for r in safe_plan["crops"]},
                            "risk": _public(r_safe)},
        "tradeoff": {
            "expected_cost": round(r_max["expected"] - r_safe["expected"], 2),
            "bad_year_gain": round(r_safe["bad_year"] - r_max["bad_year"], 2),
        },
        "histogram": {
            "edges": [round(float(e), 0) for e in edges],
            "max_return_plan": _histogram(r_max["samples"], edges),
            "risk_aware_plan": _histogram(r_safe["samples"], edges),
        },
        "frontier": frontier,
        "sources": sources,
        "model": {k: pm["meta"][k] for k in ("source", "url", "method", "window", "n_windows", "note")},
        "volatility": {c: s["std_12m_pct"] for c, s in pm["stats"].items()},
        "yield_volatility": ({c: s["sd_pct"] for c, s in ym["stats"].items()} if include_yield else {}),
    }
