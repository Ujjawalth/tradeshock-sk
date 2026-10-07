import pytest

from models import CropBudget, FarmProfile, RotationLimits
from optimizer import PlanError, budgets_for_zone, compare, load_budgets, sensitivity, solve

ZONES = ["Brown", "Dark Brown", "Black"]


def shares(plan):
    A = plan["total_acres"]
    by_crop = {r["crop"]: r["acres"] for r in plan["crops"]}
    groups = {}
    for r in plan["crops"]:
        groups[r["crop_group"]] = groups.get(r["crop_group"], 0) + r["acres"]
    return A, by_crop, groups


# ---------------------------------------------------------------- data file
def test_budget_file_loads_and_validates():
    meta, rows = load_budgets()
    assert rows, "no budget rows"
    assert "status" in meta
    for z in ZONES:
        assert budgets_for_zone(z), f"no crops for {z}"
    # one row per crop per zone
    keys = [(r.crop, r.soil_zone) for r in rows]
    assert len(keys) == len(set(keys))


def test_unverified_rows_are_flagged():
    meta, rows = load_budgets()
    if meta.get("status") != "VERIFIED":
        assert not all(r.verified for r in rows)
    else:
        assert all(r.verified and r.source_page for r in rows)


# ---------------------------------------------------------------- constraints
@pytest.mark.parametrize("zone", ZONES)
@pytest.mark.parametrize("acres", [160, 2000, 5280])
def test_acres_sum_and_limits(zone, acres):
    lim = RotationLimits()
    plan = solve(FarmProfile(soil_zone=zone, total_acres=acres))
    A, by_crop, groups = shares(plan)
    tol = 1.0  # whole-acre rounding
    assert sum(by_crop.values()) == pytest.approx(acres, abs=1e-6)
    assert by_crop.get("Canola", 0) <= A * lim.canola_max_pct / 100 + tol
    assert groups.get("pulse", 0) <= A * lim.pulses_max_pct / 100 + tol
    assert groups.get("cereal", 0) >= A * lim.cereals_min_pct / 100 - tol
    assert max(by_crop.values()) <= A * lim.single_crop_max_pct / 100 + tol


def test_known_two_crop_answer():
    """Hand-checkable: crop A earns more, so it gets the single-crop cap (50%)."""
    rows = (
        CropBudget(crop="AlphaGrain", crop_group="cereal", soil_zone="Brown", target_yield=10,
                   unit="bu", price=10, variable_cost_per_acre=50, total_cost_per_acre=60),
        CropBudget(crop="BetaGrain", crop_group="cereal", soil_zone="Brown", target_yield=10,
                   unit="bu", price=8, variable_cost_per_acre=50, total_cost_per_acre=60),
    )
    plan = solve(FarmProfile(soil_zone="Brown", total_acres=1000), rows=rows)
    by_crop = {r["crop"]: r["acres"] for r in plan["crops"]}
    assert by_crop == {"AlphaGrain": 500, "BetaGrain": 500}
    # 500*(100-50) + 500*(80-50) = 40,000
    assert plan["totals"]["return_over_variable"] == pytest.approx(40_000)
    # Breaking the cap lets the better crop take everything
    prof = FarmProfile(soil_zone="Brown", total_acres=1000,
                       limits=RotationLimits(single_crop_max_pct=100))
    plan = solve(prof, rows=rows)
    assert {r["crop"]: r["acres"] for r in plan["crops"]} == {"AlphaGrain": 1000}


@pytest.mark.parametrize("zone", ZONES)
def test_price_drop_never_adds_acres(zone):
    prof = FarmProfile(soil_zone=zone, total_acres=2000)
    base = {r["crop"]: r["acres"] for r in solve(prof)["crops"]}
    for crop in base:
        shocked = {r["crop"]: r["acres"] for r in solve(prof, {crop: -30})["crops"]}
        assert shocked.get(crop, 0) <= base[crop] + 1


def test_price_changes_move_returns():
    prof = FarmProfile(soil_zone="Dark Brown", total_acres=2000)
    up = solve(prof, {"Canola": 20})["totals"]["return_over_variable"]
    base = solve(prof)["totals"]["return_over_variable"]
    down = solve(prof, {"Canola": -20})["totals"]["return_over_variable"]
    assert down <= base <= up


def test_infeasible_profile_gives_clear_error():
    prof = FarmProfile(soil_zone="Brown", total_acres=1000,
                       crops_allowed=["Canola", "Yellow Peas", "Red Lentils"],
                       limits=RotationLimits(cereals_min_pct=0))
    with pytest.raises(PlanError, match="at most 66%"):
        solve(prof)
    prof = FarmProfile(soil_zone="Brown", total_acres=1000, crops_allowed=["Canola", "Flax"],
                       limits=RotationLimits(single_crop_max_pct=100, canola_max_pct=100))
    with pytest.raises(PlanError, match="cereal"):
        solve(prof)


def test_no_crops_selected():
    with pytest.raises(PlanError):
        solve(FarmProfile(soil_zone="Brown", total_acres=1000, crops_allowed=[]))


# ---------------------------------------------------------------- comparisons
def test_compare_stand_still_never_beats_replanning():
    prof = FarmProfile(soil_zone="Dark Brown", total_acres=2000)
    for shock in ({"Canola": -20}, {"Red Lentils": -40, "Oats": 15}, {}):
        cmp = compare(prof, shock)
        s = cmp["summary"]
        assert s["shock_return"] >= s["stand_still_return"] - 1e-6
        assert s["value_of_replanning"] >= -1e-6
        if not shock:
            assert s["change_vs_baseline"] == pytest.approx(0)
            assert not s["mix_changed"]


def test_stress_cases_regret_logic():
    from optimizer import stress_cases
    prof = FarmProfile(soil_zone="Dark Brown", total_acres=2000)
    cases = {"bear": {"Canola": -30}, "base": {"Canola": -18}, "bull": {"Canola": -8}}
    out = stress_cases(prof, cases)
    labels = [l for p in out["plans"] for l in p["labels"]]
    assert sorted(labels) == sorted(["bear_plan", "base_plan", "bull_plan", "maxmin_plan", "regret_plan"])
    by = {l: p for p in out["plans"] for l in p["labels"]}
    # each case-optimal plan has zero regret in its own case
    for case in cases:
        assert by[f"{case}_plan"]["regret"][case] == pytest.approx(0, abs=1)
    # minimax-regret plan has the smallest worst regret; max-min has the best worst-case return
    assert by["regret_plan"]["max_regret"] <= min(p["max_regret"] for p in out["plans"]) + 1
    assert by["maxmin_plan"]["worst_return"] >= max(p["worst_return"] for p in out["plans"]) - 1
    for p in out["plans"]:
        assert sum(p["acres"].values()) == 2000


def test_scenario_ranges_are_ordered_and_assumed_when_missing():
    from models import repair_scenario
    crops = ["Canola", "Oats"]
    sc = repair_scenario({"affected": [
        {"crop": "Canola", "price_change_pct": -20, "confidence": "medium", "low_pct": -10, "high_pct": -40},
        {"crop": "Oats", "price_change_pct": 10, "confidence": "low"}]}, crops)
    canola, oats = sc.affected
    assert canola.low_pct <= -20 <= canola.high_pct and not canola.range_assumed
    assert oats.range_assumed and (oats.low_pct, oats.high_pct) == (0, 20)
    assert sc.cases()["bear"] == {"Canola": canola.low_pct, "Oats": 0}


def test_sensitivity_finds_tipping_point_for_canola():
    prof = FarmProfile(soil_zone="Dark Brown", total_acres=2000)
    sens = sensitivity(prof, "Canola", step=10)
    assert len(sens["points"]) == 13
    returns = [p["return_over_variable"] for p in sens["points"]]
    assert returns == sorted(returns)  # higher canola price never lowers best return
    assert sens["tipping_points"], "expected canola to drop out at some low price"
