"""Refresh data/demo_cache.json with LIVE model output for the 3 presets.

Run once (with ANTHROPIC_API_KEY in .env) before the demo:
    python scripts/build_demo_cache.py

- Replaces each preset's seed scenario with the model's scenario.
- Caches the explanation for the default farm (Dark Brown, 2000 ac) per preset,
  keyed by the hash of the optimizer facts, so the demo works with Wi-Fi off.
"""
from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

import ai  # noqa: E402
from app import CACHE_PATH, facts_key, zone_crops  # noqa: E402
from models import FarmProfile, repair_scenario  # noqa: E402
from optimizer import compare  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
SK_TZ = timezone(timedelta(hours=-6))
DEMO_PROFILES = [FarmProfile(soil_zone=z, total_acres=2000) for z in ("Dark Brown", "Brown", "Black")]


def main() -> None:
    if not ai.available():
        raise SystemExit("Set ANTHROPIC_API_KEY in .env first.")
    cache = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    explanations = {}
    for preset in cache["presets"]:
        crops = zone_crops("Dark Brown")
        print(f"\n== {preset['id']}")
        sc = ai.headline_to_scenario(preset["headline"], crops)
        preset["scenario"] = sc.model_dump(exclude={"dropped_crops"})
        print(json.dumps(preset["scenario"], indent=1))
        for prof in DEMO_PROFILES:
            sc_zone = repair_scenario(preset["scenario"], zone_crops(prof.soil_zone))
            cmp = compare(prof, sc_zone.price_changes())
            text, source = ai.explain_plan(cmp, sc_zone)
            if source == "ai":
                explanations[facts_key(ai.build_facts(cmp, sc_zone))] = text
            print(f"-- {prof.soil_zone}: explanation source={source}")
    cache["explanations"] = explanations
    cache["meta"]["generated_by"] = f"{ai._model()} via scripts/build_demo_cache.py"
    cache["meta"]["prompt_version"] = f"{ai.SCENARIO_PROMPT_VERSION}/{ai.EXPLAIN_PROMPT_VERSION}"
    cache["meta"]["generated_at"] = datetime.now(SK_TZ).strftime("%Y-%m-%d %H:%M SK")
    CACHE_PATH.write_text(json.dumps(cache, indent=1, ensure_ascii=False), encoding="utf-8")
    print(f"\nWrote {CACHE_PATH}")


if __name__ == "__main__":
    main()
