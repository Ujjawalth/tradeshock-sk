import pytest

import ai
import app as app_module
import watch
from models import FarmProfile, Scenario

PROFILE = FarmProfile(soil_zone="Dark Brown", total_acres=2000)
CROPS = ["Canola", "CWRS Wheat", "Durum", "Barley", "Oats", "Yellow Peas", "Red Lentils", "Flax"]

ATOM = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry><title>Canada responds to new tariffs on canola meal</title>
    <link href="https://www.canada.ca/en/news/1.html"/><updated>2026-10-05T10:00:00-04:00</updated>
    <summary>&lt;p&gt;Statement on &lt;b&gt;canola&lt;/b&gt; trade&lt;/p&gt;</summary></entry>
  <entry><title>Investing in rural broadband</title>
    <link href="javascript:alert(1)"/><updated>2026-10-04T10:00:00-04:00</updated></entry>
  <entry><title>Minister meets on internal trade</title>
    <link href="https://evil.example.com/x"/><updated>2026-10-03T10:00:00-04:00</updated></entry>
</feed>"""


@pytest.fixture(autouse=True)
def isolated_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(watch, "CACHE_PATH", tmp_path / "watch_cache.json")
    monkeypatch.setattr(watch, "REPORT_PATH", tmp_path / "watch_report.json")


def test_parse_atom_strips_html_and_unsafe_links():
    items = watch.parse_atom(ATOM, "Test feed")
    assert [i["title"] for i in items][0] == "Canada responds to new tariffs on canola meal"
    assert items[0]["link"].startswith("https://www.canada.ca/")
    assert items[0]["summary"] == "Statement on canola trade"
    assert items[1]["link"] == "" and items[2]["link"] == ""  # javascript: and foreign hosts dropped


def test_keyword_filter_and_needs_ai_offline():
    items = watch.triage(watch.parse_atom(ATOM, "f"), CROPS, use_ai=False)
    tri = {i["title"]: i["triage"] for i in items}
    assert tri["Investing in rural broadband"] == "keyword-skip"
    assert tri["Canada responds to new tariffs on canola meal"] == "needs-ai"


def test_ai_triage_is_cached(monkeypatch):
    calls = []

    def fake(text, crops):
        calls.append(text)
        return Scenario(affected=[{"crop": "Canola", "price_change_pct": -10, "confidence": "medium",
                                   "reasoning": "r"}], is_trade_related=True)

    monkeypatch.setattr(ai, "headline_to_scenario", fake)
    items = watch.triage(watch.parse_atom(ATOM, "f"), CROPS, use_ai=True)
    assert len(calls) == 2  # two keyword hits (tariffs/canola, trade)
    assert sum(i["triage"] == "ai" for i in items) == 2
    items = watch.triage(watch.parse_atom(ATOM, "f"), CROPS, use_ai=True)
    assert len(calls) == 2 and sum(i["triage"] == "cache" for i in items) == 2


def test_impact_ranks_alerts(monkeypatch):
    monkeypatch.setattr(watch, "fetch_all", lambda: ([], ["offline"]))
    report = watch.scan(PROFILE, "live")  # no feed -> falls back to the sample feed
    assert report["source"] == "sample"
    impacts = [abs(a["impact"]["change_vs_baseline"]) for a in report["alerts"]]
    assert impacts == sorted(impacts, reverse=True) and len(impacts) == 3
    assert all(a["title"].startswith("HYPOTHETICAL") for a in report["alerts"])
    assert len(report["other"]) == 2


def test_watch_api(monkeypatch):
    app_module.ai_limiter.hits.clear()
    c = app_module.app.test_client()
    res = c.post("/api/watch", json={"profile": {"soil_zone": "Black", "total_acres": 3000}, "mode": "sample"})
    assert res.status_code == 200
    w = res.get_json()["watch"]
    assert w["source"] == "sample" and w["alerts"]
    assert c.get("/api/watch/latest").status_code == 200
