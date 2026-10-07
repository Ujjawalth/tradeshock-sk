from types import SimpleNamespace

import pytest

import agent
import ai
from models import FarmProfile

CROPS = ["Canola", "CWRS Wheat", "Durum", "Barley", "Oats", "Yellow Peas", "Red Lentils", "Flax"]
PROFILE = FarmProfile(soil_zone="Dark Brown", total_acres=2000)


def state(changes=None):
    return agent.AgentState(profile=PROFILE, price_changes=changes or {}, crops=CROPS)


# ---------------------------------------------------------------- tools
def test_what_if_tool_merges_onto_current_scenario():
    st = state({"Canola": -18})
    res, err = agent.run_tool(st, "what_if", {"price_changes": [{"crop": "Yellow Peas", "price_change_pct": -20}],
                                              "limits": {"canola_max_pct": 25}, "total_acres": 2500})
    assert not err
    applied = res["applied_settings"]
    assert applied["price_changes_pct"] == {"Canola": -18, "Yellow Peas": -20}
    assert applied["limits_pct"]["canola_max_pct"] == 25 and applied["total_acres"] == 2500
    assert sum(m["acres"] for m in res["what_if_mix"]) == 2500
    assert st.proposal["price_changes"] == {"Canola": -18, "Yellow Peas": -20}


def test_what_if_clamps_and_reports_errors():
    st = state()
    res, err = agent.run_tool(st, "what_if", {"price_changes": [{"crop": "Canola", "price_change_pct": -500}]})
    assert not err and res["applied_settings"]["price_changes_pct"] == {"Canola": -60}
    res, err = agent.run_tool(st, "what_if", {"exclude_crops": ["CWRS Wheat", "Durum", "Barley", "Oats"]})
    assert err and "cereal" in res["error"].lower()
    res, err = agent.run_tool(st, "nope", {})
    assert err


def test_sweep_and_budgets_tools():
    st = state()
    res, err = agent.run_tool(st, "price_sweep", {"crop": "canola"})
    assert not err and res["crop"] == "Canola" and res["tipping_points"]
    res, err = agent.run_tool(st, "crop_budgets", {})
    assert not err and len(res["budgets"]) == len(CROPS)


# ---------------------------------------------------------------- offline parser
@pytest.mark.parametrize("q, expect", [
    ("What if peas drop 20% and I cap canola at 25%?",
     {"price_changes": [{"crop": "Yellow Peas", "price_change_pct": -20}], "limits": {"canola_max_pct": 25}}),
    ("canola -15%", {"price_changes": [{"crop": "Canola", "price_change_pct": -15}]}),
    ("oats up 10%", {"price_changes": [{"crop": "Oats", "price_change_pct": 10}]}),
    ("add 500 acres", {"total_acres": 2500}),
    ("what if I farm 3000 acres", {"total_acres": 3000}),
    ("no lentils", {"exclude_crops": ["Red Lentils"]}),
    ("What if I don't grow lentils this year?", {"exclude_crops": ["Red Lentils"]}),
    ("What if I rent 500 more acres?", {"total_acres": 2500}),
    ("cereals at least 30%", {"limits": {"cereals_min_pct": 30}}),
])
def test_offline_parser(q, expect):
    assert agent.parse_question(q, state())["what_if"] == expect


def test_offline_answer_end_to_end():
    out = agent.answer("What if peas drop 20% and I cap canola at 25%?", PROFILE, {}, CROPS)
    assert out["source"] == "offline"
    assert "What-if plan" in out["answer"] and out["proposal"]
    assert out["proposal"]["profile"]["limits"]["canola_max_pct"] == 25
    out = agent.answer("Where does canola tip?", PROFILE, {}, CROPS)
    assert "tipping points" in out["answer"]


def test_bear_base_bull_tool_and_offline():
    cases = {"bear": {"Canola": -30}, "base": {"Canola": -18}, "bull": {"Canola": -8}}
    st = agent.AgentState(profile=PROFILE, price_changes={"Canola": -18}, crops=CROPS, cases=cases)
    res, err = agent.run_tool(st, "bear_base_bull", {})
    assert not err and res["plans"] and "least-regret" in res["recommended_least_regret"]
    out = agent.answer("Which plan is safest if I'm wrong about canola?", PROFILE, {"Canola": -18}, CROPS,
                       cases=cases)
    assert "Bear / base / bull" in out["answer"] and out["steps"][0]["tool"] == "bear_base_bull"
    out = agent.answer("what if I'm wrong?", PROFILE, {}, CROPS)
    assert "can't compare" in out["answer"]


def test_unclear_question_gets_help_not_a_plan():
    out = agent.answer("are you live", PROFILE, {}, CROPS)
    assert "offline mode I understand" in out["answer"]
    assert out["proposal"] is None and out["steps"] == []
    out = agent.answer("plan for barley", PROFILE, {}, CROPS)
    assert "price change for Barley" in out["answer"]


def test_offline_answer_says_why(monkeypatch):
    monkeypatch.setattr(ai, "status", lambda: "no_credit")
    out = agent.answer("canola -20%", PROFILE, {}, CROPS)
    assert out["answer"].startswith("(Offline mode: live AI is paused")


def test_single_plan_cases_wording():
    same = {"bear": {"Oats": -10}, "base": {"Oats": -5}, "bull": {"Oats": 0}}  # oats not in plan
    out = agent.answer("what if I'm wrong?", PROFILE, {"Oats": -5}, CROPS, cases=same)
    assert "best plan in all three cases" in out["answer"]


def test_empty_question_rejected():
    with pytest.raises(ValueError):
        agent.answer("   ", PROFILE, {}, CROPS)


# ---------------------------------------------------------------- Claude loop (mocked API)
def _resp(*blocks):
    return SimpleNamespace(content=list(blocks), stop_reason="end_turn")


def _tool_use(name, args, id_="tu_1"):
    return SimpleNamespace(type="tool_use", name=name, input=args, id=id_)


def _text(t):
    return SimpleNamespace(type="text", text=t)


def test_agent_loop_runs_tools_and_guards_numbers(monkeypatch):
    calls = []

    def fake_create(version, **kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            return _resp(_tool_use("what_if", {"price_changes": [{"crop": "Canola", "price_change_pct": -20}]}))
        # second call: tool result is present; answer with a number from it
        result = kwargs["messages"][-1]["content"][0]
        assert result["type"] == "tool_result" and result["tool_use_id"] == "tu_1"
        import json
        ret = json.loads(result["content"])["what_if_return"]
        return _resp(_text(f"Re-planning gives {ret}. Main risk: prices may recover."))

    monkeypatch.setattr(ai, "available", lambda: True)
    monkeypatch.setattr(ai, "create_message", fake_create)
    out = agent.answer("canola down 20%?", PROFILE, {}, CROPS)
    assert out["source"] == "ai" and out["steps"] == [{"tool": "what_if", "error": False}]
    assert out["proposal"]["price_changes"] == {"Canola": -20}
    # tools were offered with a crop enum
    assert calls[0]["tools"][0]["name"] == "what_if"


def test_agent_invented_numbers_fall_back_offline(monkeypatch):
    monkeypatch.setattr(ai, "available", lambda: True)
    monkeypatch.setattr(ai, "create_message",
                        lambda version, **kw: _resp(_text("China's tariff is 75.8%, so you lose $123,456.")))
    out = agent.answer("canola down 20%?", PROFILE, {}, CROPS)
    assert out["source"] == "offline"
    assert "75.8" not in out["answer"]
