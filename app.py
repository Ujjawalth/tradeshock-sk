"""TradeShock SK - Flask app.

Routes
  GET  /                  single-page UI
  GET  /api/config        zones, crops, default limits, presets, data status
  POST /api/plan          farm profile -> baseline plan
  POST /api/scenario      headline or preset -> validated scenario   (AI, rate limited)
  POST /api/shock         profile + scenario -> baseline vs shock comparison
  POST /api/explain       profile + scenario -> plain-English summary (AI, rate limited)
  POST /api/sensitivity   profile + crop -> price sweep with tipping points
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from collections import defaultdict, deque
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request
from werkzeug.middleware.proxy_fix import ProxyFix
from pydantic import ValidationError

load_dotenv()

import access  # noqa: E402
import agent  # noqa: E402
import ai  # noqa: E402  (needs env loaded first)
import lease  # noqa: E402
import risk  # noqa: E402
import watch  # noqa: E402
from models import (MAX_HEADLINE_CHARS, FarmProfile, RotationLimits,  # noqa: E402
                    Scenario, clean_headline, repair_scenario)
from optimizer import (PlanError, budgets_for_zone, compare, load_budgets,  # noqa: E402
                       sensitivity, solve, stress_cases)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("tradeshock.app")

app = Flask(__name__)
# Behind Render's single proxy: take the client IP from the hop the proxy appended.
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1)
app.json.sort_keys = False  # keep soil zones in Brown -> Dark Brown -> Black order
LEASE_MAX_BYTES = 2 * 1024 * 1024
app.config["MAX_CONTENT_LENGTH"] = LEASE_MAX_BYTES + 64 * 1024  # small JSON, or one lease file

CACHE_PATH = Path(__file__).parent / "data" / "demo_cache.json"


def load_cache() -> dict:
    try:
        return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        log.warning("demo cache missing or invalid")
        return {"presets": [], "explanations": {}}


DEMO_CACHE = load_cache()


# ---------------------------------------------------------------- rate limit
class RateLimiter:
    """Sliding-window limit per key (in memory, per server process)."""

    def __init__(self, per_minute: int, window_s: float = 60):
        self.per_minute = per_minute   # max hits per window
        self.window_s = window_s
        self.hits: dict[str, deque] = defaultdict(deque)
        self.lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        with self.lock:
            q = self.hits[key]
            while q and now - q[0] > self.window_s:
                q.popleft()
            if len(q) >= self.per_minute:
                return False
            q.append(now)
            return True


ai_limiter = RateLimiter(int(os.getenv("AI_RATE_LIMIT_PER_MIN", "10")))
# Password guessing: 5 tries / 15 min per IP, and 30 / 15 min across everyone.
unlock_limiter = RateLimiter(5, window_s=15 * 60)
unlock_global_limiter = RateLimiter(30, window_s=15 * 60)


def client_ip() -> str:
    # ProxyFix trusts only the LAST X-Forwarded-For hop, which Render's proxy adds.
    # The first entries are client-supplied and could be faked to dodge the rate limit.
    return request.remote_addr or "unknown"


def rate_limited():
    if not ai_limiter.allow(client_ip()):
        return jsonify(error="Too many AI requests. Please wait a minute and try again."), 429
    return None


# ---------------------------------------------------------------- helpers
def body() -> dict:
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


def parse_profile(data: dict) -> FarmProfile:
    return FarmProfile(**(data.get("profile") or {}))


def zone_crops(zone: str) -> list[str]:
    return [b.crop for b in budgets_for_zone(zone)]


def parse_scenario(data: dict, crops: list[str]) -> Scenario:
    """Scenario edited by the user (sliders) - re-validated, never trusted."""
    return repair_scenario(data.get("scenario") or {}, crops)


def bad_request(msg: str, code: int = 400):
    return jsonify(error=msg), code


def facts_key(facts: dict) -> str:
    return hashlib.sha256(json.dumps(facts, sort_keys=True).encode()).hexdigest()[:24]


@app.before_request
def apply_ai_lock():
    """Admin lock: live AI only for requests carrying a valid unlock cookie."""
    ai.request_ai_allowed.set(access.ai_allowed(request.cookies.get(access.COOKIE_NAME)))


CSP = ("default-src 'self'; script-src 'self' https://cdnjs.cloudflare.com; style-src 'self'; "
       "img-src 'self' data:; connect-src 'self'; font-src 'self'; object-src 'none'; "
       "base-uri 'none'; frame-ancestors 'none'; form-action 'self'")


@app.after_request
def security_headers(resp):
    resp.headers.setdefault("Content-Security-Policy", CSP)
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("Referrer-Policy", "no-referrer")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    return resp


@app.errorhandler(ValidationError)
def on_validation_error(exc: ValidationError):
    first = exc.errors()[0] if exc.errors() else {}
    where = ".".join(str(p) for p in first.get("loc", []))
    return bad_request(f"Invalid input {where}: {first.get('msg', 'bad value')}".strip())


@app.errorhandler(PlanError)
def on_plan_error(exc: PlanError):
    return bad_request(str(exc), 422)


@app.errorhandler(413)
def on_too_large(_exc):
    return bad_request("Request too large.", 413)


# ---------------------------------------------------------------- pages
@app.get("/")
def index():
    return render_template("index.html", max_headline=MAX_HEADLINE_CHARS,
                           disclaimer=ai.DISCLAIMER)


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/api/config")
def config():
    meta, rows = load_budgets()
    zones = {}
    for r in rows:
        zones.setdefault(r.soil_zone, []).append({
            "crop": r.crop, "crop_group": r.crop_group, "unit": r.unit,
            "price": r.price, "target_yield": r.target_yield,
            "variable_cost_per_acre": r.variable_cost_per_acre,
            "total_cost_per_acre": r.total_cost_per_acre,
            "source_page": r.source_page, "verified": r.verified,
        })
    return jsonify(
        zones=zones,
        default_limits=RotationLimits().model_dump(),
        presets=[{"id": p["id"], "label": p["label"], "headline": p["headline"]}
                 for p in DEMO_CACHE.get("presets", [])],
        data_status=meta.get("status", "UNKNOWN"),
        data_source=meta.get("source", ""),
        data_verified=all(r.verified for r in rows),
        ai_available=ai.available(),
        ai_configured=bool(os.getenv("ANTHROPIC_API_KEY")),
        ai_locked=access.lock_enabled() and not ai.request_ai_allowed.get(),
        ai_lock_enabled=access.lock_enabled(),
        risk_available=risk.available(),
        yield_risk_available=risk.yield_available(),
        max_headline=MAX_HEADLINE_CHARS,
        disclaimer=ai.DISCLAIMER,
    )


# ---------------------------------------------------------------- planning
@app.post("/api/plan")
def plan():
    profile = parse_profile(body())
    return jsonify(plan=solve(profile))


@app.post("/api/shock")
def shock():
    data = body()
    profile = parse_profile(data)
    scenario = parse_scenario(data, zone_crops(profile.soil_zone))
    return jsonify(comparison=compare(profile, scenario.price_changes()),
                   scenario=scenario.model_dump())


@app.post("/api/sensitivity")
def sweep():
    data = body()
    profile = parse_profile(data)
    crops = zone_crops(profile.soil_zone)
    crop = data.get("crop")
    if crop not in crops:
        return bad_request("Unknown crop for this soil zone.")
    scenario = parse_scenario(data, crops)
    base = {k: v for k, v in scenario.price_changes().items() if k != crop}
    return jsonify(sensitivity=sensitivity(profile, crop, base))


# ---------------------------------------------------------------- AI
@app.post("/api/scenario")
def scenario_route():
    if (limited := rate_limited()):
        return limited
    data = body()
    zone = (data.get("profile") or {}).get("soil_zone", "Dark Brown")
    crops = zone_crops(zone) or zone_crops("Dark Brown")

    preset_id = data.get("preset_id")
    if preset_id:
        preset = next((p for p in DEMO_CACHE.get("presets", []) if p["id"] == preset_id), None)
        if not preset:
            return bad_request("Unknown preset.")
        # Presets always come from the cache: instant and works offline.
        scenario = repair_scenario(preset["scenario"], crops)
        return jsonify(scenario=scenario.model_dump(), source="cache",
                       headline=preset["headline"], hypothetical=True)

    headline = clean_headline(data.get("headline", ""))
    if len(headline) < 8:
        return bad_request("Please paste a headline (at least a few words).")
    try:
        if access.lock_enabled() and not ai.request_ai_allowed.get():
            return jsonify(error="Live AI is locked. Unlock it with the admin password (top of the page), "
                                 "or try a HYPOTHETICAL preset, which works without AI.", locked=True), 403
        scenario = ai.headline_to_scenario(headline, crops)
    except ai.AIUnavailable as exc:
        return jsonify(error=f"AI unavailable: {exc}. Try a HYPOTHETICAL preset (works offline) "
                             "or set the sliders by hand."), 503
    return jsonify(scenario=scenario.model_dump(), source="ai", headline=headline,
                   hypothetical=False)


@app.post("/api/explain")
def explain_route():
    if (limited := rate_limited()):
        return limited
    data = body()
    profile = parse_profile(data)
    scenario = parse_scenario(data, zone_crops(profile.soil_zone))
    # Recompute on the server: the explanation only ever sees optimizer output.
    comparison = compare(profile, scenario.price_changes())
    facts = ai.build_facts(comparison, scenario)

    cached = DEMO_CACHE.get("explanations", {}).get(facts_key(facts))
    if cached:
        text, source = cached, "cache"
    else:
        text, source = ai.explain_plan(comparison, scenario)
    return jsonify(explanation=text, source=source, disclaimer=ai.DISCLAIMER)


@app.post("/api/cases")
def cases_route():
    """Bear / base / bull stress cases and the robust (max-min) plan."""
    data = body()
    profile = parse_profile(data)
    scenario = parse_scenario(data, zone_crops(profile.soil_zone))
    result = stress_cases(profile, scenario.cases())
    result["range_assumed"] = any(a.range_assumed for a in scenario.affected)
    return jsonify(cases=result)


@app.post("/api/risk")
def risk_route():
    """Price-risk lens: replay historical price moves, compare with a risk-aware plan."""
    data = body()
    profile = parse_profile(data)
    scenario = parse_scenario(data, zone_crops(profile.soil_zone))
    try:
        lam = float(data.get("risk_aversion", 0.5))
    except (TypeError, ValueError):
        return bad_request("risk_aversion must be a number between 0 and 1.")
    include_yield = data.get("include_yield", True) is not False
    return jsonify(risk=risk.analyze(profile, scenario.price_changes(), lam, include_yield=include_yield))


@app.post("/api/unlock")
def unlock_route():
    """Admin password -> signed, HttpOnly unlock cookie (12 h)."""
    if not access.lock_enabled():
        return jsonify(ok=True, locked=False)
    if not unlock_limiter.allow(client_ip()) or not unlock_global_limiter.allow("all"):
        return jsonify(error="Too many attempts. Try again in 15 minutes."), 429
    if access.password_too_weak():
        log.error("AI_ACCESS_PASSWORD is shorter than %d characters; refusing to unlock", access.MIN_LENGTH)
        return jsonify(error="The admin password on the server is too short, so AI stays locked."), 503
    if not access.check_password(str(body().get("password", ""))[:200]):
        log.warning("failed AI unlock attempt")  # never log the attempted password
        return jsonify(error="Wrong password."), 401
    resp = jsonify(ok=True, locked=False)
    resp.set_cookie(access.COOKIE_NAME, access.make_token(), max_age=access.TOKEN_TTL_SECONDS,
                    httponly=True, secure=request.is_secure, samesite="Strict", path="/")
    return resp


@app.post("/api/lock")
def lock_route():
    """Forget this browser's unlock."""
    resp = jsonify(ok=True)
    resp.delete_cookie(access.COOKIE_NAME, path="/", samesite="Strict")
    return resp


@app.post("/api/watch")
def watch_route():
    """Trade Watch: scan official feeds (or the HYPOTHETICAL sample) and rank alerts by $ impact."""
    if (limited := rate_limited()):
        return limited
    data = body()
    profile = parse_profile(data)
    mode = "sample" if data.get("mode") == "sample" else "live"
    return jsonify(watch=watch.scan(profile, mode))


@app.get("/api/watch/latest")
def watch_latest():
    """Last report written by the scheduled scan (scripts/watch_scan.py)."""
    try:
        return jsonify(watch=json.loads(watch.REPORT_PATH.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError):
        return jsonify(watch=None)


@app.post("/api/lease")
def lease_route():
    """Other farm income: extract annual payment + review date from a SYNTHETIC lease (text or PDF)."""
    if (limited := rate_limited()):
        return limited
    if request.files.get("file"):
        f = request.files["file"]
        raw = f.read(LEASE_MAX_BYTES + 1)
        if len(raw) > LEASE_MAX_BYTES:
            return bad_request("File too large (max 2 MB).", 413)
        name = (f.filename or "").lower()
        if name.endswith(".pdf") or raw[:4] == b"%PDF":
            try:
                text = lease.pdf_to_text(raw)
            except Exception:  # pdfplumber raises many types on bad files
                return bad_request("Couldn't read that PDF.")
        else:
            text = raw.decode("utf-8", "replace")
    elif body().get("sample"):
        text = (Path(__file__).parent / "data" / "sample_surface_lease.txt").read_text(encoding="utf-8")
    else:
        text = str(body().get("text", ""))
    try:
        return jsonify(lease=lease.extract(text))
    except ValueError as exc:
        return bad_request(str(exc))


@app.post("/api/ask")
def ask_route():
    """Ask TradeShock agent: what-if questions answered by tool calls to the optimizer."""
    if (limited := rate_limited()):
        return limited
    data = body()
    profile = parse_profile(data)
    crops = zone_crops(profile.soil_zone)
    scenario = parse_scenario(data, crops)
    history = data.get("history") if isinstance(data.get("history"), list) else []
    try:
        result = agent.answer(data.get("question", ""), profile, scenario.price_changes(),
                              crops, history, cases=scenario.cases() if scenario.affected else None)
    except ValueError as exc:
        return bad_request(str(exc))
    result["disclaimer"] = ai.DISCLAIMER
    return jsonify(result)


if __name__ == "__main__":
    app.run(debug=os.getenv("FLASK_DEBUG") == "1", port=int(os.getenv("PORT", "5000")))
