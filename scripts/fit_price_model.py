"""Fit the price-risk model from public Statistics Canada data.

Source: Statistics Canada Table 32-10-0077-01, "Farm product prices, crops and
livestock" (monthly, Saskatchewan). Open Government Licence - Canada.
Download (done once, kept out of git):
    https://www150.statcan.gc.ca/n1/tbl/csv/32100077-eng.zip  -> data/raw/statcan_32100077/

What we fit (no invented numbers - everything comes from the table):
- For every month t in the common window, the 12-month log price change of
  each crop: ln(P[t+12] / P[t]). Each window keeps all crops together, so
  cross-crop correlation (e.g. canola and flax moving together) is preserved.
- Changes are de-meaned so simulations are centred on the guide's budget
  prices: the guide sets the expected price, history sets the spread.

Run:  python scripts/fit_price_model.py
Out:  data/price_model.json (small, committed)
"""
from __future__ import annotations

import csv
import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "data" / "raw" / "statcan_32100077" / "32100077.csv"
OUT = ROOT / "data" / "price_model.json"
HORIZON = 12  # months: winter planning -> next marketing year

# Our budget crop -> StatCan series (Saskatchewan). Pulses are class-level series.
SERIES = {
    "Canola": "Canola (including rapeseed) [113111]",
    "CWRS Wheat": "Wheat (except durum wheat) [1121111]",
    "Durum": "Durum wheat [112111211]",
    "Barley": "Barley [1151141]",
    "Oats": "Oats [115113111]",
    "Yellow Peas": "Dry peas [114314]",
    "Red Lentils": "Lentils [114312]",
    "Flax": "Flaxseed [115122111]",
}
SERIES_NOTES = {
    "Yellow Peas": "StatCan series covers all dry peas, not yellow peas only.",
    "Red Lentils": "StatCan series covers all lentils, not red lentils only.",
    "CWRS Wheat": "StatCan series is wheat excluding durum (all classes).",
}


def load_series() -> dict[str, dict[str, float]]:
    wanted = {v: k for k, v in SERIES.items()}
    out: dict[str, dict[str, float]] = {k: {} for k in SERIES}
    with open(SRC, encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            if r["GEO"] != "Saskatchewan" or r["Farm products"] not in wanted or not r["VALUE"]:
                continue
            out[wanted[r["Farm products"]]][r["REF_DATE"]] = float(r["VALUE"])
    return out


def month_add(ym: str, n: int) -> str:
    y, m = map(int, ym.split("-"))
    m0 = y * 12 + (m - 1) + n
    return f"{m0 // 12:04d}-{m0 % 12 + 1:02d}"


def pct(v: float) -> float:
    return round(100 * (math.exp(v) - 1), 1)


def main() -> None:
    if not SRC.exists():
        raise SystemExit(f"Missing {SRC}. Download the StatCan table first (see docstring).")
    series = load_series()
    crops = list(SERIES)
    common = sorted(set.intersection(*(set(s) for s in series.values())))
    starts = [t for t in common if month_add(t, HORIZON) in series[crops[0]]
              and all(month_add(t, HORIZON) in series[c] for c in crops)]

    raw = {c: [math.log(series[c][month_add(t, HORIZON)] / series[c][t]) for t in starts] for c in crops}
    means = {c: sum(v) / len(v) for c, v in raw.items()}
    centred = {c: [x - means[c] for x in v] for c, v in raw.items()}

    def std(v):
        m = sum(v) / len(v)
        return math.sqrt(sum((x - m) ** 2 for x in v) / (len(v) - 1))

    def quantile(v, q):
        s = sorted(v)
        i = (len(s) - 1) * q
        lo, hi = int(math.floor(i)), int(math.ceil(i))
        return s[lo] + (s[hi] - s[lo]) * (i - lo)

    def corr(a, b):
        ma, mb = sum(a) / len(a), sum(b) / len(b)
        cov = sum((x - ma) * (y - mb) for x, y in zip(a, b))
        return cov / math.sqrt(sum((x - ma) ** 2 for x in a) * sum((y - mb) ** 2 for y in b))

    stats = {c: {
        "std_12m_pct": round(100 * std(centred[c]), 1),
        "p5_12m_pct": pct(quantile(centred[c], 0.05)),
        "p95_12m_pct": pct(quantile(centred[c], 0.95)),
        "worst_12m_pct": pct(min(centred[c])),
        "best_12m_pct": pct(max(centred[c])),
        "raw_mean_12m_pct": pct(means[c]),
    } for c in crops}

    model = {
        "meta": {
            "source": "Statistics Canada. Table 32-10-0077-01 Farm product prices, crops and livestock "
                      "(Saskatchewan, monthly). Open Government Licence - Canada.",
            "url": "https://www150.statcan.gc.ca/t1/tbl1/en/tv.action?pid=3210007701",
            "method": f"Joint historical {HORIZON}-month log price changes, de-meaned (centred on guide prices).",
            "horizon_months": HORIZON,
            "window": f"{starts[0]} to {month_add(starts[-1], HORIZON)}",
            "n_windows": len(starts),
            "note": "Overlapping windows: scenarios are not independent. Price risk only (no yield risk).",
            "series": SERIES,
            "series_notes": SERIES_NOTES,
            "fitted_at": datetime.now(timezone(timedelta(hours=-6))).strftime("%Y-%m-%d %H:%M SK"),
        },
        "stats": stats,
        "correlation": {a: {b: round(corr(raw[a], raw[b]), 2) for b in crops} for a in crops},
        "windows": [{"start": t, "log_change": {c: round(centred[c][i], 5) for c in crops}}
                    for i, t in enumerate(starts)],
    }
    OUT.write_text(json.dumps(model, indent=1), encoding="utf-8")
    print(f"Wrote {OUT}: {len(starts)} windows, {model['meta']['window']}")
    for c in crops:
        s = stats[c]
        print(f"  {c:<12} sd {s['std_12m_pct']:>5}%   5th pct {s['p5_12m_pct']:>6}%   95th {s['p95_12m_pct']:>6}%")


if __name__ == "__main__":
    main()
