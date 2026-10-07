"""One-time: Crop Planning Guide PDF -> data/crop_budgets_2026.json

The official guide is downloaded manually into data/raw/ (we never fetch copies).

Usage
  # 1. Look at what pdfplumber sees (pages, zone headings, table previews)
  python scripts/extract_guide.py inspect data/raw/<guide>.pdf

  # 2. Try automatic extraction (writes JSON only if every zone parses)
  python scripts/extract_guide.py extract data/raw/<guide>.pdf

  # 3. Fallback: hand-enter numbers from the PDF into a CSV, then convert
  python scripts/extract_guide.py template            # writes data/budgets_manual.csv
  python scripts/extract_guide.py from-csv data/budgets_manual.csv

Every row keeps source_page so each number is traceable to the guide.
"""
from __future__ import annotations

import csv
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from models import CropBudget  # noqa: E402

OUT = ROOT / "data" / "crop_budgets_2026.json"
MANUAL_CSV = ROOT / "data" / "budgets_manual.csv"
FIELDS = ["crop", "crop_group", "soil_zone", "target_yield", "unit", "price",
          "variable_cost_per_acre", "total_cost_per_acre", "source_page"]

# Crop name patterns -> (our name, group). Extend after running `inspect`.
CROP_PATTERNS = [
    (r"\bcanola\b", "Canola", "oilseed"),
    (r"\bdurum\b", "Durum", "cereal"),
    (r"\b(cwrs|hard red spring|spring wheat)\b", "CWRS Wheat", "cereal"),
    (r"\bbarley\b", "Barley", "cereal"),
    (r"\boats?\b", "Oats", "cereal"),
    (r"\b(yellow )?(field )?peas?\b", "Yellow Peas", "pulse"),
    (r"\b(red )?lentils?\b", "Red Lentils", "pulse"),
    (r"\bflax", "Flax", "oilseed"),
]
ROW_PATTERNS = {
    "target_yield": r"(target|estimated|expected)?\s*yield",
    "price": r"(estimated|expected|forecast)?\s*price",
    "variable_cost_per_acre": r"total\s+variable\s+(expenses|costs)",
    "total_cost_per_acre": r"total\s+(expenses|costs)\b(?!.*variable)",
}
ZONE_PATTERNS = [("Dark Brown", r"dark\s+brown"), ("Black", r"\bblack\b"), ("Brown", r"\bbrown\b")]


def _num(cell) -> float | None:
    if cell is None:
        return None
    m = re.search(r"-?\d[\d,]*\.?\d*", str(cell).replace("$", ""))
    return float(m.group(0).replace(",", "")) if m else None


def _zone(text: str) -> str | None:
    head = text.lower()[:600]  # zone heading is near the top of a budget page
    for zone, pat in ZONE_PATTERNS:
        if re.search(pat + r"\s+soil\s+zone", head):
            return zone
    return None


def _crop(header: str):
    h = (header or "").lower()
    for pat, name, group in CROP_PATTERNS:
        if re.search(pat, h):
            return name, group
    return None


def inspect(pdf_path: str) -> None:
    import pdfplumber
    with pdfplumber.open(pdf_path) as pdf:
        print(f"{len(pdf.pages)} pages")
        for i, page in enumerate(pdf.pages, start=1):
            text = page.extract_text() or ""
            tables = page.extract_tables()
            zone = _zone(text)
            if not tables and not zone:
                continue
            first = text.strip().splitlines()[:2]
            print(f"\n--- page {i}  zone={zone}  tables={len(tables)}  | {' / '.join(first)[:100]}")
            for t in tables[:2]:
                for row in t[:6]:
                    print("    ", [str(c)[:18] if c else "" for c in row][:9])


def extract(pdf_path: str) -> list[dict]:
    """Heuristic: crops are columns, budget lines are rows. Verify with `inspect` first."""
    import pdfplumber
    rows: dict[tuple, dict] = {}
    with pdfplumber.open(pdf_path) as pdf:
        for pno, page in enumerate(pdf.pages, start=1):
            zone = _zone(page.extract_text() or "")
            if not zone:
                continue
            for table in page.extract_tables():
                if not table or len(table) < 3:
                    continue
                header = table[0]
                cols = {j: _crop(h) for j, h in enumerate(header) if _crop(h)}
                if not cols:
                    continue
                for line in table[1:]:
                    label = (line[0] or "").lower()
                    for field, pat in ROW_PATTERNS.items():
                        if not re.search(pat, label):
                            continue
                        for j, (crop, group) in cols.items():
                            if j >= len(line):
                                continue
                            val = _num(line[j])
                            if val is None:
                                continue
                            rec = rows.setdefault((crop, zone), {
                                "crop": crop, "crop_group": group, "soil_zone": zone,
                                "unit": "lb" if "lb" in label else "bu", "source_page": pno})
                            rec.setdefault(field, val)
    complete = [r for r in rows.values() if all(f in r for f in FIELDS)]
    missing = [f"{r['crop']}/{r['soil_zone']}" for r in rows.values() if r not in complete]
    if missing:
        print("Incomplete rows (fill these by hand):", ", ".join(missing))
    return complete


def from_csv(csv_path: str) -> list[dict]:
    out = []
    with open(csv_path, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if not r.get("crop"):
                continue
            out.append({
                "crop": r["crop"].strip(), "crop_group": r["crop_group"].strip(),
                "soil_zone": r["soil_zone"].strip(), "unit": r["unit"].strip(),
                "target_yield": float(r["target_yield"]), "price": float(r["price"]),
                "variable_cost_per_acre": float(r["variable_cost_per_acre"]),
                "total_cost_per_acre": float(r["total_cost_per_acre"]),
                "source_page": int(r["source_page"]),
            })
    return out


def write(rows: list[dict], source: str) -> None:
    validated = [CropBudget(**{**r, "verified": True}).model_dump() for r in rows]
    for r in validated:
        if not r["source_page"]:
            raise SystemExit(f"{r['crop']}/{r['soil_zone']} has no source_page")
    zones = {r["soil_zone"] for r in validated}
    if zones != {"Brown", "Dark Brown", "Black"}:
        raise SystemExit(f"Need all three soil zones, got {sorted(zones)}; not writing.")
    doc = {
        "meta": {
            "title": "Crop budgets by soil zone",
            "source": source,
            "status": "VERIFIED",
            "note": "Extracted from the Saskatchewan Ministry of Agriculture 2026 Crop Planning Guide. "
                    "source_page = PDF page number. Spot-check against the guide before the demo.",
            "price_units": "CAD per unit",
            "cost_units": "CAD per acre",
        },
        "rows": validated,
    }
    OUT.write_text(json.dumps(doc, indent=1), encoding="utf-8")
    print(f"Wrote {len(validated)} rows to {OUT}")


def template() -> None:
    with open(MANUAL_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(FIELDS)
        for zone in ("Brown", "Dark Brown", "Black"):
            for _, crop, group in CROP_PATTERNS:
                w.writerow([crop, group, zone, "", "lb" if "Lentil" in crop else "bu", "", "", "", ""])
    print(f"Fill in {MANUAL_CSV} from the guide (one row per crop per zone), then run from-csv.")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    cmd = sys.argv[1]
    if cmd == "inspect":
        inspect(sys.argv[2])
    elif cmd == "extract":
        name = Path(sys.argv[2]).name
        write(extract(sys.argv[2]), f"Saskatchewan Ministry of Agriculture, 2026 Crop Planning Guide ({name}), "
                                    "auto-extracted with pdfplumber")
    elif cmd == "template":
        template()
    elif cmd == "from-csv":
        write(from_csv(sys.argv[2]), "Saskatchewan Ministry of Agriculture, 2026 Crop Planning Guide, "
                                     "hand-entered from the PDF (pdfplumber extraction was unreliable)")
    else:
        raise SystemExit(__doc__)
