import json

import pytest

from models import MAX_HEADLINE_CHARS, clean_headline, repair_scenario

CROPS = ["Canola", "CWRS Wheat", "Durum", "Barley", "Oats", "Yellow Peas", "Red Lentils", "Flax"]


def test_valid_scenario_passes():
    raw = {"affected": [{"crop": "Canola", "price_change_pct": -18, "confidence": "medium",
                         "reasoning": "x"}],
           "duration_months": 12, "assumptions": ["a"], "is_trade_related": True}
    sc = repair_scenario(raw, CROPS)
    assert sc.price_changes() == {"Canola": -18}
    assert sc.is_trade_related


def test_price_change_is_clamped():
    raw = {"affected": [{"crop": "Canola", "price_change_pct": -95, "confidence": "high", "reasoning": ""},
                        {"crop": "Oats", "price_change_pct": 400, "confidence": "low", "reasoning": ""}]}
    sc = repair_scenario(raw, CROPS)
    assert sc.price_changes() == {"Canola": -60, "Oats": 60}


def test_aliases_unknowns_and_duplicates():
    raw = {"affected": [
        {"crop": "canola meal", "price_change_pct": -10, "confidence": "HIGH", "reasoning": ""},
        {"crop": "Canola", "price_change_pct": -50, "confidence": "low", "reasoning": ""},
        {"crop": "peas", "price_change_pct": -5, "confidence": "maybe", "reasoning": ""},
        {"crop": "Soybeans", "price_change_pct": 3, "confidence": "low", "reasoning": ""},
    ]}
    sc = repair_scenario(raw, CROPS)
    assert sc.price_changes() == {"Canola": -10, "Yellow Peas": -5}
    assert sc.affected[0].confidence == "high"
    assert sc.affected[1].confidence == "low"  # unknown confidence -> low
    assert sc.dropped_crops == ["Soybeans"]


def test_json_text_with_fences_is_repaired():
    text = "```json\n" + json.dumps({"affected": [], "is_trade_related": False}) + "\n```"
    sc = repair_scenario(text, CROPS)
    assert not sc.is_trade_related and sc.affected == []


def test_garbage_is_rejected():
    with pytest.raises(ValueError):
        repair_scenario("I cannot help with that.", CROPS)


def test_bad_numbers_are_dropped_not_crashing():
    raw = {"affected": [{"crop": "Canola", "price_change_pct": "lots", "confidence": "low", "reasoning": ""}],
           "duration_months": "forever", "assumptions": ["x" * 1000] * 20}
    sc = repair_scenario(raw, CROPS)
    assert sc.affected == [] and sc.dropped_crops == ["Canola"]
    assert sc.duration_months == 12
    assert len(sc.assumptions) == 8 and len(sc.assumptions[0]) == 300


def test_duration_clamped():
    assert repair_scenario({"duration_months": 999}, CROPS).duration_months == 60
    assert repair_scenario({"duration_months": -3}, CROPS).duration_months == 0


def test_headline_cleaning():
    dirty = "China\x00 tariffs\n\n on   canola " + "x" * 2000
    h = clean_headline(dirty)
    assert "\x00" not in h and "\n" not in h
    assert h.startswith("China tariffs on canola")
    assert len(h) == MAX_HEADLINE_CHARS
