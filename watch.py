"""Trade Watch: automated headline monitoring.

1. Pull official Government of Canada news feeds (Atom, public API).
2. Keyword pre-filter (cheap) -> Claude triage with the same injection-safe
   scenario prompt used for pasted headlines (feed text is UNTRUSTED).
3. Every trade-related item is stress-tested against the farm automatically
   and ranked by $ impact on the plan.

Scenarios are cached per headline (data/watch_cache.json) so repeated scans
don't repeat AI calls. Offline, a clearly HYPOTHETICAL sample feed is used.
Run on a schedule with scripts/watch_scan.py (cron / Task Scheduler / Render cron).
"""
from __future__ import annotations

import hashlib
import html
import json
import logging
import re
import threading
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import ai
from models import FarmProfile, Scenario, clean_headline, repair_scenario
from optimizer import PlanError, budgets_for_zone, compare

log = logging.getLogger("tradeshock.watch")

DATA = Path(__file__).parent / "data"
CACHE_PATH = DATA / "watch_cache.json"
SAMPLE_PATH = DATA / "watch_sample.json"
REPORT_PATH = DATA / "watch_report.json"
FARM_PATH = DATA / "farm_profile.json"
SK_TZ = timezone(timedelta(hours=-6))

# Official feeds only (Government of Canada news API, Atom format).
FEEDS = [
    {"name": "Agriculture and Agri-Food Canada",
     "url": "https://api.io.canada.ca/io-server/gc/news/en/v2?dept=agricultureagrifood"
            "&sort=publishedDate&orderBy=desc&pick=25&format=atom"},
    {"name": "Department of Finance Canada",
     "url": "https://api.io.canada.ca/io-server/gc/news/en/v2?dept=departmentfinance"
            "&sort=publishedDate&orderBy=desc&pick=25&format=atom"},
]
ALLOWED_LINK_HOSTS = ("https://www.canada.ca/", "https://canada.ca/", "https://agriculture.canada.ca/")

KEYWORDS = re.compile(
    r"\b(tariffs?|dut(y|ies)|surtax\w*|trade|exports?|imports?|market access|anti-dumping|"
    r"canola|peas?|pulses?|lentils?|wheat|durum|oats?|barley|flax\w*|grains?|oilseeds?|"
    r"china|india|united states|u\.s\.|european union)\b", re.I)
MAX_AI_ITEMS = 6
_cache_lock = threading.Lock()


# ---------------------------------------------------------------- feeds
def _text(el) -> str:
    return "".join(el.itertext()).strip() if el is not None else ""


def parse_atom(xml_text: str, source: str) -> list[dict]:
    ns = {"a": "http://www.w3.org/2005/Atom"}
    root = ET.fromstring(xml_text)
    items = []
    for e in root.findall("a:entry", ns):
        title = clean_headline(html.unescape(_text(e.find("a:title", ns))))
        link_el = e.find("a:link", ns)
        link = link_el.get("href", "") if link_el is not None else ""
        if not link.startswith(ALLOWED_LINK_HOSTS):
            link = ""  # never render links to unexpected hosts
        summary = re.sub(r"<[^>]+>", " ", html.unescape(_text(e.find("a:summary", ns)) or _text(e.find("a:content", ns))))
        summary = re.sub(r"\s+", " ", summary).strip()[:400]
        if title:
            items.append({"title": title, "link": link, "published": _text(e.find("a:updated", ns))[:10],
                          "source": source, "summary": summary})
    return items


def fetch_feed(feed: dict, timeout: float = 10.0) -> list[dict]:
    req = urllib.request.Request(feed["url"], headers={"User-Agent": "TradeShockSK/1.0 (hackathon demo)"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (fixed official URLs)
        return parse_atom(resp.read().decode("utf-8", "replace"), feed["name"])


def fetch_all() -> tuple[list[dict], list[str]]:
    items, errors = [], []
    for feed in FEEDS:
        try:
            items += fetch_feed(feed)
        except Exception as exc:  # network, XML, HTTP - report and keep going
            errors.append(f"{feed['name']}: {type(exc).__name__}")
            log.warning("feed failed %s: %s", feed["name"], exc)
    seen, unique = set(), []
    for it in items:
        if it["title"].lower() not in seen:
            seen.add(it["title"].lower())
            unique.append(it)
    return unique, errors


# ---------------------------------------------------------------- cache
def _key(title: str) -> str:
    return hashlib.sha256(f"{ai.SCENARIO_PROMPT_VERSION}|{title}".encode()).hexdigest()[:20]


def _load_cache() -> dict:
    try:
        return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _save_cache(cache: dict) -> None:
    try:
        CACHE_PATH.write_text(json.dumps(cache, indent=1, ensure_ascii=False), encoding="utf-8")
    except OSError as exc:
        log.warning("could not write watch cache: %s", exc)


# ---------------------------------------------------------------- triage + impact
def triage(items: list[dict], crops: list[str], use_ai: bool) -> list[dict]:
    """Attach a validated scenario (or a reason) to each item."""
    cache = _load_cache()
    todo = []
    for it in items:
        if it.get("scenario") is not None:          # sample feed carries its own scenario
            it["triage"] = "sample"
            continue
        if not KEYWORDS.search(f"{it['title']} {it.get('summary', '')}"):
            it["triage"] = "keyword-skip"
            continue
        cached = cache.get(_key(it["title"]))
        if cached:
            it["scenario"], it["triage"] = cached, "cache"
        elif use_ai:
            todo.append(it)
        else:
            it["triage"] = "needs-ai"

    def run(it):
        try:
            text = it["title"] + (f". {it['summary']}" if it.get("summary") else "")
            sc = ai.headline_to_scenario(text, crops)
            it["scenario"], it["triage"] = sc.model_dump(), "ai"
        except (ai.AIUnavailable, ValueError) as exc:
            it["triage"], it["error"] = "ai-error", str(exc)

    batch = todo[:MAX_AI_ITEMS]
    for it in todo[MAX_AI_ITEMS:]:
        it["triage"] = "deferred"  # picked up by the next scan
    if batch:
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(run, batch))
        with _cache_lock:
            cache = _load_cache()
            for it in batch:
                if it.get("triage") == "ai":
                    cache[_key(it["title"])] = it["scenario"]
            _save_cache(cache)
    return items


def impact(items: list[dict], profile: FarmProfile, crops: list[str]) -> list[dict]:
    for it in items:
        raw = it.get("scenario")
        if not raw:
            it["relevant"] = False
            continue
        sc: Scenario = repair_scenario(raw, crops)
        it["scenario"] = sc.model_dump()
        it["relevant"] = bool(sc.is_trade_related and sc.affected)
        if not it["relevant"]:
            continue
        try:
            s = compare(profile, sc.price_changes())["summary"]
            it["impact"] = {"change_vs_baseline": s["change_vs_baseline"],
                            "change_vs_baseline_pct": s["change_vs_baseline_pct"],
                            "value_of_replanning": s["value_of_replanning"],
                            "mix_changed": s["mix_changed"]}
        except PlanError as exc:
            it["impact"], it["error"] = None, str(exc)
    relevant = [i for i in items if i.get("relevant")]
    others = [i for i in items if not i.get("relevant")]
    relevant.sort(key=lambda i: abs((i.get("impact") or {}).get("change_vs_baseline", 0)), reverse=True)
    return relevant + others


def load_sample() -> list[dict]:
    data = json.loads(SAMPLE_PATH.read_text(encoding="utf-8"))
    return [dict(it) for it in data["items"]]


def scan(profile: FarmProfile, mode: str = "live") -> dict:
    """mode: 'live' (official feeds, falls back to sample) or 'sample'."""
    crops = [b.crop for b in budgets_for_zone(profile.soil_zone)]
    errors, source = [], "sample"
    items = []
    if mode == "live":
        items, errors = fetch_all()
        source = "live" if items else "sample"
    if not items:
        items = load_sample()
    items = triage(items, crops, use_ai=ai.available())
    items = impact(items, profile, crops)
    counts = {}
    for it in items:
        counts[it.get("triage", "?")] = counts.get(it.get("triage", "?"), 0) + 1
    return {
        "source": source,
        "scanned_at": datetime.now(SK_TZ).strftime("%Y-%m-%d %H:%M SK"),
        "feeds": [f["name"] for f in FEEDS] if source == "live" else ["HYPOTHETICAL sample feed"],
        "errors": errors,
        "ai_available": ai.available(),
        "counts": counts,
        "alerts": [i for i in items if i.get("relevant")],
        "other": [{k: i.get(k) for k in ("title", "link", "published", "source", "triage")}
                  for i in items if not i.get("relevant")][:30],
    }
