"""Scheduled Trade Watch scan (the automation).

Scans official Government of Canada feeds, triages new items with Claude,
stress-tests each trade-related one against the saved farm, and writes
data/watch_report.json (shown in the app via /api/watch/latest).

Run by hand:
    python scripts/watch_scan.py                 # live feeds
    python scripts/watch_scan.py --sample        # HYPOTHETICAL sample feed

Schedule (pick one):
  Windows Task Scheduler:
    schtasks /Create /SC HOURLY /TN TradeShockWatch /TR "\"C:\\TradeShock SK\\venv\\Scripts\\python.exe\" \"C:\\TradeShock SK\\scripts\\watch_scan.py\""
  cron (Linux/macOS):
    0 * * * *  cd /path/to/tradeshock-sk && venv/bin/python scripts/watch_scan.py
  Render: add a Cron Job service with command `python scripts/watch_scan.py`.

Farm profile: data/farm_profile.json if present, else Dark Brown, 2,000 acres.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

import watch  # noqa: E402
from models import FarmProfile  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")


def money(v: float) -> str:
    return f"{'-' if v < 0 else '+'}${abs(v):,.0f}"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--sample", action="store_true", help="use the HYPOTHETICAL sample feed")
    args = ap.parse_args()

    if watch.FARM_PATH.exists():
        profile = FarmProfile(**json.loads(watch.FARM_PATH.read_text(encoding="utf-8")))
    else:
        profile = FarmProfile()
    report = watch.scan(profile, "sample" if args.sample else "live")
    report["profile"] = profile.model_dump()
    watch.REPORT_PATH.write_text(json.dumps(report, indent=1, ensure_ascii=False), encoding="utf-8")

    print(f"\nTrade Watch digest - {report['scanned_at']} ({report['source']})")
    print(f"Farm: {profile.soil_zone}, {profile.total_acres:,.0f} ac | triage: {report['counts']}")
    if report["errors"]:
        print("Feed errors:", "; ".join(report["errors"]))
    if not report["alerts"]:
        print("No trade-related items that affect your crops right now.")
    for a in report["alerts"]:
        imp = a.get("impact") or {}
        print(f"- {a['title']}")
        if imp:
            print(f"    plan return {money(imp['change_vs_baseline'])} vs baseline; "
                  f"re-planning worth {money(imp['value_of_replanning'])}")
    print(f"\nReport: {watch.REPORT_PATH}")


if __name__ == "__main__":
    main()
