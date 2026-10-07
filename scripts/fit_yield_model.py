"""Fit the yield-risk model from public Statistics Canada data.

Source: Statistics Canada Table 32-10-0359-01, "Estimated areas, yield,
production, average farm price and total farm value of principal field crops"
(annual, Saskatchewan). Open Government Licence - Canada.
Download (done once, kept out of git):
    https://www150.statcan.gc.ca/n1/tbl/csv/32100359-eng.zip -> data/raw/statcan_32100359/

What we fit (no invented numbers):
- Provincial average yield per crop per year, 1991-2025 (CWRS series starts 1991;
  the current year is skipped because its estimate may be preliminary).
- A linear trend per crop (yields rise with technology); each year's factor is
  actual / trend. Factors are kept together per year, so a drought year hits all
  crops at once (cross-crop correlation preserved).
Caveat: provincial averages swing less than a single farm's yields, so this
understates farm-level yield risk.

Run:  python scripts/fit_yield_model.py
Out:  data/yield_model.json (small, committed)
"""
from __future__ import annotations

import csv
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "data" / "raw" / "statcan_32100359" / "32100359.csv"
OUT = ROOT / "data" / "yield_model.json"
FIRST_YEAR, LAST_YEAR = 1991, 2025

# Our budget crop -> (StatCan crop name, yield unit row)
SERIES = {
    "Canola": ("Canola (rapeseed)", "Average yield (bushels per acre)"),
    "CWRS Wheat": ("Wheat, Canada Western Red Spring (CWRS)", "Average yield (bushels per acre)"),
    "Durum": ("Wheat, durum", "Average yield (bushels per acre)"),
    "Barley": ("Barley", "Average yield (bushels per acre)"),
    "Oats": ("Oats", "Average yield (bushels per acre)"),
    "Yellow Peas": ("Peas, dry", "Average yield (bushels per acre)"),
    "Red Lentils": ("Lentils", "Average yield (pounds per acre)"),
    "Flax": ("Flaxseed", "Average yield (bushels per acre)"),
}
SERIES_NOTES = {
    "Yellow Peas": "StatCan series covers all dry peas.",
    "Red Lentils": "StatCan series covers all lentils.",
}


def main() -> None:
    if not SRC.exists():
        raise SystemExit(f"Missing {SRC}. Download the StatCan table first (see docstring).")
    wanted = {v: k for k, v in SERIES.items()}
    data: dict[str, dict[int, float]] = {k: {} for k in SERIES}
    with open(SRC, encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            if r["GEO"] != "Saskatchewan" or not r["VALUE"]:
                continue
            key = (r["Type of crop"], r["Harvest disposition"])
            if key in wanted:
                y = int(r["REF_DATE"][:4])
                if FIRST_YEAR <= y <= LAST_YEAR:
                    data[wanted[key]][y] = float(r["VALUE"])

    years = sorted(set.intersection(*(set(v) for v in data.values())))
    factors, stats, trend_info = {}, {}, {}
    for crop, series in data.items():
        x = np.array(years, dtype=float)
        y = np.array([series[t] for t in years])
        slope, intercept = np.polyfit(x, y, 1)
        trend = slope * x + intercept
        f = y / trend
        f = f / f.mean()  # mean factor 1 -> expected yield = guide target yield
        factors[crop] = f
        stats[crop] = {
            "sd_pct": round(100 * float(f.std(ddof=1)), 1),
            "worst_pct": round(100 * (float(f.min()) - 1), 1),
            "worst_year": int(years[int(f.argmin())]),
            "best_pct": round(100 * (float(f.max()) - 1), 1),
        }
        trend_info[crop] = {"slope_per_year": round(float(slope), 3), "unit": SERIES[crop][1]}

    crops = list(SERIES)
    corr = np.corrcoef(np.array([factors[c] for c in crops]))
    model = {
        "meta": {
            "source": "Statistics Canada. Table 32-10-0359-01 Estimated areas, yield, production, average farm price "
                      "and total farm value of principal field crops (Saskatchewan, annual). "
                      "Open Government Licence - Canada.",
            "url": "https://www150.statcan.gc.ca/t1/tbl1/en/tv.action?pid=3210035901",
            "method": "Provincial average yield / linear trend, per year, de-meaned (centred on guide target yields).",
            "window": f"{years[0]}-{years[-1]}",
            "n_years": len(years),
            "note": "Provincial averages understate single-farm yield swings. Crop insurance is not modelled.",
            "series": {c: SERIES[c][0] for c in crops},
            "series_notes": SERIES_NOTES,
            "fitted_at": datetime.now(timezone(timedelta(hours=-6))).strftime("%Y-%m-%d %H:%M SK"),
        },
        "trend": trend_info,
        "stats": stats,
        "correlation": {a: {b: round(float(corr[i, j]), 2) for j, b in enumerate(crops)} for i, a in enumerate(crops)},
        "years": [{"year": t, "factor": {c: round(float(factors[c][i]), 4) for c in crops}}
                  for i, t in enumerate(years)],
    }
    OUT.write_text(json.dumps(model, indent=1), encoding="utf-8")
    print(f"Wrote {OUT}: {len(years)} years {years[0]}-{years[-1]}")
    for c in crops:
        s = stats[c]
        print(f"  {c:<12} sd {s['sd_pct']:>5}%  worst {s['worst_pct']:>6}% ({s['worst_year']})  best {s['best_pct']:>5}%")


if __name__ == "__main__":
    main()
