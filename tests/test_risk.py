import pytest

import agent
import app as app_module
import risk
from models import FarmProfile
from optimizer import solve

pytestmark = pytest.mark.skipif(not risk.available(), reason="price model not fitted")

PROFILE = FarmProfile(soil_zone="Dark Brown", total_acres=2000)
CROPS = ["Canola", "CWRS Wheat", "Durum", "Barley", "Oats", "Yellow Peas", "Red Lentils", "Flax"]


def test_model_file_is_traceable():
    m = risk.load_model()
    assert "Statistics Canada" in m["meta"]["source"] and "32-10-0077" in m["meta"]["source"]
    assert m["meta"]["n_windows"] == len(m["windows"]) > 50
    # de-meaned: each crop's log changes average ~0
    for c in CROPS:
        vals = [w["log_change"][c] for w in m["windows"]]
        assert abs(sum(vals) / len(vals)) < 1e-3


def test_expected_return_matches_deterministic_plan():
    changes = {"Canola": -18}
    plan = solve(PROFILE, changes)
    acres = {r["crop"]: r["acres"] for r in plan["crops"]}
    r = risk.risk_of_mix(PROFILE, acres, changes, include_yield=True)
    assert r["expected"] == pytest.approx(plan["totals"]["return_over_variable"], rel=1e-9)
    assert r["worst"] <= r["bad_year"] <= r["p10"] <= r["expected"] <= r["p90"] <= r["best"]


def test_risk_aversion_trades_average_for_bad_year():
    out = risk.analyze(PROFILE, {"Canola": -18}, 1.0, with_frontier=True)
    mx, safe = out["max_return_plan"]["risk"], out["risk_aware_plan"]["risk"]
    assert safe["bad_year"] >= mx["bad_year"] - 1e-6
    assert safe["expected"] <= mx["expected"] + 1e-6
    assert sum(out["risk_aware_plan"]["acres"].values()) == 2000
    # frontier is monotone: more aversion -> lower average, better bad year
    f = out["frontier"]
    assert all(a["expected"] >= b["expected"] - 1e-6 for a, b in zip(f, f[1:]))
    assert all(a["bad_year"] <= b["bad_year"] + 1e-6 for a, b in zip(f, f[1:]))
    assert sum(out["histogram"]["max_return_plan"]) == out["n_scenarios"]


def test_yield_risk_widens_bad_years_but_keeps_expected():
    if not risk.yield_available():
        pytest.skip("yield model not fitted")
    changes = {"Canola": -18}
    price_only = risk.analyze(PROFILE, changes, 0.5, include_yield=False)
    both = risk.analyze(PROFILE, changes, 0.5, include_yield=True)
    pm, bm = price_only["max_return_plan"]["risk"], both["max_return_plan"]["risk"]
    assert pm["expected"] == pytest.approx(bm["expected"], rel=1e-9)  # both factors average 1
    assert bm["bad_year"] < pm["bad_year"]                           # more risk sources -> worse bad years
    assert both["n_scenarios"] == price_only["n_scenarios"] * risk.load_yield_model()["meta"]["n_years"]
    assert "yields" in bm["worst_label"] and both["sources"][1]["kind"] == "yield"
    ym = risk.load_yield_model()
    assert "32-10-0359" in ym["meta"]["source"]
    for c in CROPS:  # de-meaned factors
        vals = [y["factor"][c] for y in ym["years"]]
        assert abs(sum(vals) / len(vals) - 1) < 1e-3


def test_risk_aware_plan_respects_rotation_limits():
    acres = risk.optimize(PROFILE, {}, 1.0)
    assert acres.get("Canola", 0) <= 660 + 1
    pulses = acres.get("Yellow Peas", 0) + acres.get("Red Lentils", 0)
    cereals = sum(acres.get(c, 0) for c in ("CWRS Wheat", "Durum", "Barley", "Oats"))
    assert pulses <= 660 + 1 and cereals >= 400 - 1 and max(acres.values()) <= 1000 + 1


def test_risk_api_and_agent_tool():
    c = app_module.app.test_client()
    res = c.post("/api/risk", json={"profile": {"soil_zone": "Black", "total_acres": 1500},
                                    "scenario": {}, "risk_aversion": 0.75})
    assert res.status_code == 200 and res.get_json()["risk"]["risk_aware_plan"]["acres"]
    assert c.post("/api/risk", json={"risk_aversion": "lots"}).status_code == 400

    st = agent.AgentState(profile=PROFILE, price_changes={}, crops=CROPS)
    out, err = agent.run_tool(st, "risk_report", {"risk_aversion": 0.5})
    assert not err and "Statistics Canada" in out["price_history"]
    ans = agent.answer("How risky is my plan in a bad year?", PROFILE, {}, CROPS)
    assert "Price-risk check" in ans["answer"]
