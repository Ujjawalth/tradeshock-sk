import time

import pytest

import access
import ai
import app as app_module

STRONG = "correct-horse-battery-staple"
PROFILE = {"soil_zone": "Dark Brown", "total_acres": 2000}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("AI_ACCESS_PASSWORD", STRONG)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-not-real")
    for lim in (app_module.ai_limiter, app_module.unlock_limiter, app_module.unlock_global_limiter):
        lim.hits.clear()
    app_module.app.config["TESTING"] = True
    return app_module.app.test_client()


def test_tokens_sign_and_expire(monkeypatch):
    monkeypatch.setenv("AI_ACCESS_PASSWORD", STRONG)
    tok = access.make_token()
    assert access.token_valid(tok)
    assert not access.token_valid(tok[:-1] + ("0" if tok[-1] != "0" else "1"))  # tampered signature
    expiry, sig = tok.split(".")
    assert not access.token_valid(f"{int(expiry) + 999}.{sig}")                 # tampered expiry
    assert not access.token_valid(tok, now=time.time() + access.TOKEN_TTL_SECONDS + 5)  # expired
    monkeypatch.setenv("AI_ACCESS_PASSWORD", STRONG + "-rotated")
    assert not access.token_valid(tok)                                           # password change revokes


def test_weak_password_fails_closed(monkeypatch):
    monkeypatch.setenv("AI_ACCESS_PASSWORD", "short")
    assert access.lock_enabled() and access.password_too_weak()
    assert not access.check_password("short")
    assert not access.ai_allowed(access.make_token())


def test_no_password_means_open(monkeypatch):
    monkeypatch.delenv("AI_ACCESS_PASSWORD", raising=False)
    assert access.ai_allowed(None)


def test_locked_by_default_and_live_headline_refused(client):
    cfg = client.get("/api/config").get_json()
    assert cfg["ai_locked"] and not cfg["ai_available"] and cfg["ai_lock_enabled"]
    res = client.post("/api/scenario", json={"headline": "China raises canola tariffs", "profile": PROFILE})
    assert res.status_code == 403 and res.get_json()["locked"]
    # offline features still work while locked
    assert client.post("/api/scenario", json={"preset_id": "canola-duties"}).status_code == 200


def test_locked_requests_never_reach_the_api(client, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("live API must not be called while locked")
    monkeypatch.setattr(ai, "create_message", boom)
    out = client.post("/api/ask", json={"question": "canola down 20%?", "profile": PROFILE}).get_json()
    assert out["source"] == "offline"
    sc = client.post("/api/scenario", json={"preset_id": "canola-duties"}).get_json()["scenario"]
    ex = client.post("/api/explain", json={"profile": PROFILE, "scenario": sc}).get_json()
    assert ex["source"] in ("template", "cache")
    lease = client.post("/api/lease", json={"sample": True}).get_json()["lease"]
    assert lease["method"] == "regex"


def test_unlock_flow_and_cookie_flags(client):
    assert client.post("/api/unlock", json={"password": "nope"}).status_code == 401
    res = client.post("/api/unlock", json={"password": STRONG})
    assert res.status_code == 200
    cookie = res.headers["Set-Cookie"]
    assert "HttpOnly" in cookie and "SameSite=Strict" in cookie and access.COOKIE_NAME in cookie
    assert STRONG not in cookie
    cfg = client.get("/api/config").get_json()
    assert not cfg["ai_locked"] and cfg["ai_available"]
    client.post("/api/lock", json={})
    assert client.get("/api/config").get_json()["ai_locked"]


def test_forged_cookie_rejected(client):
    client.set_cookie(access.COOKIE_NAME, f"{int(time.time()) + 3600}.{'0' * 64}")
    assert client.get("/api/config").get_json()["ai_locked"]


def test_brute_force_is_rate_limited(client):
    codes = [client.post("/api/unlock", json={"password": f"guess{i}"}).status_code for i in range(7)]
    assert codes[:5] == [401] * 5 and codes[5:] == [429, 429]
    # even the right password is refused while rate-limited
    assert client.post("/api/unlock", json={"password": STRONG}).status_code == 429
