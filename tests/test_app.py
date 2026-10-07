import pytest

import app as app_module

PROFILE = {"soil_zone": "Dark Brown", "total_acres": 2000}


@pytest.fixture
def client():
    app_module.app.config["TESTING"] = True
    app_module.ai_limiter.hits.clear()
    return app_module.app.test_client()


def test_index_and_config(client):
    assert client.get("/").status_code == 200
    cfg = client.get("/api/config").get_json()
    assert set(cfg["zones"]) == {"Brown", "Dark Brown", "Black"}
    assert len(cfg["presets"]) == 3
    assert all("HYPOTHETICAL" in p["headline"] for p in cfg["presets"])
    assert cfg["ai_available"] is False


def test_security_headers(client):
    res = client.get("/")
    csp = res.headers["Content-Security-Policy"]
    assert "script-src 'self' https://cdnjs.cloudflare.com" in csp and "unsafe-inline" not in csp
    assert res.headers["X-Content-Type-Options"] == "nosniff"
    assert b"<script>" not in res.data  # no inline scripts (CSP-safe)


def test_plan(client):
    res = client.post("/api/plan", json={"profile": PROFILE})
    assert res.status_code == 200
    assert res.get_json()["plan"]["totals"]["return_over_variable"] > 0


def test_plan_bad_input(client):
    res = client.post("/api/plan", json={"profile": {"soil_zone": "Mars", "total_acres": 10}})
    assert res.status_code == 400
    res = client.post("/api/plan", json={"profile": {**PROFILE, "crops_allowed": ["Canola"]}})
    assert res.status_code == 422


@pytest.mark.parametrize("preset", ["canola-duties", "pea-relief-expires", "oats-us-tariff"])
def test_presets_work_offline_end_to_end(client, preset):
    sc = client.post("/api/scenario", json={"preset_id": preset, "profile": PROFILE}).get_json()
    assert sc["source"] == "cache" and sc["scenario"]["affected"]
    shock = client.post("/api/shock", json={"profile": PROFILE, "scenario": sc["scenario"]}).get_json()
    assert "summary" in shock["comparison"]
    ex = client.post("/api/explain", json={"profile": PROFILE, "scenario": sc["scenario"]}).get_json()
    assert ex["explanation"] and ex["disclaimer"] == "Planning aid, not financial advice."


def test_live_headline_without_key_returns_503(client):
    res = client.post("/api/scenario", json={"headline": "China raises tariffs on canola meal",
                                             "profile": PROFILE})
    assert res.status_code == 503
    assert "preset" in res.get_json()["error"]


def test_shock_rejects_untrusted_slider_values(client):
    scenario = {"affected": [{"crop": "Canola", "price_change_pct": -500, "confidence": "low",
                              "reasoning": ""}]}
    data = client.post("/api/shock", json={"profile": PROFILE, "scenario": scenario}).get_json()
    assert data["scenario"]["affected"][0]["price_change_pct"] == -60


def test_sensitivity(client):
    res = client.post("/api/sensitivity", json={"profile": PROFILE, "crop": "Canola", "scenario": {}})
    assert res.status_code == 200
    assert res.get_json()["sensitivity"]["points"]
    res = client.post("/api/sensitivity", json={"profile": PROFILE, "crop": "Bananas"})
    assert res.status_code == 400


def test_ai_rate_limit(client, monkeypatch):
    monkeypatch.setattr(app_module.ai_limiter, "per_minute", 2)
    payload = {"preset_id": "canola-duties", "profile": PROFILE}
    codes = [client.post("/api/scenario", json=payload).status_code for _ in range(3)]
    assert codes == [200, 200, 429]
