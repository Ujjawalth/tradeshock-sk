"""One-time: Crop Planning Guide PDF -> data/crop_budgets_2026.json

The official guide is downloaded manually into data/raw/ (we never fetch copies).

The 2026 guide has one page per crop, with soil zones as columns
(Brown / Dark Brown / Black). We parse the page TEXT lines, e.g.
    Target Yield (bu./acre) (A) 35.3 43.0 50.0
    Estimated Farm Gate Price ($/bu.) (B) 13.20 13.20 13.20
and keep a row only if it passes the guide's own arithmetic:
    yield x price = gross revenue (C)        (within rounding)
    gross revenue - total variable (D) = return over variable expenses (C-D)

Usage
  python scripts/extract_guide.py inspect data/raw/<guide>.pdf      # page titles + key lines
  python scripts/extract_guide.py extract data/raw/<guide>.pdf      # writes data/crop_budgets_2026.json
  # Fallback if a future guide breaks the parser: hand-enter into a CSV, then convert
  python scripts/extract_guide.py template
  python scripts/extract_guide.py from-csv data/budgets_manual.csv

source_page = PDF page number (1-based) in the downloaded file.
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
ZONES = ("Brown", "Dark Brown", "Black")

# Guide page title -> (our crop name, group). Our names match the risk models' series.
GUIDE_CROPS = {
    "Canola": ("Canola", "oilseed"),
    "Hard Red Spring Wheat": ("CWRS Wheat", "cereal"),
    "Durum Wheat": ("Durum", "cereal"),
    "Feed Barley": ("Barley", "cereal"),
    "Oats": ("Oats", "cereal"),
    "Flax": ("Flax", "oilseed"),
    "Edible Yellow Peas": ("Yellow Peas", "pulse"),
    "Red Lentils": ("Red Lentils", "pulse"),
}

NUM = r"-?\d[\d,]*\.\d+"  # guide values always have decimals
LEADING_NUMS = re.compile(rf"^\s*((?:{NUM}\s+)*{NUM})")  # stop at the side-column text
LINES = {
    "target_yield": re.compile(r"Target Yield \((bu|lbs?)\.?/ac(?:re|\.)?\) \(A\)\s+(.*)"),
    "price": re.compile(r"Estimated Farm Gate Price \(\$/(bu|lbs?)\.?\) \(B\)\s+(.*)"),
    "gross": re.compile(r"Estimated Gross Revenue \(\$/ac\.\) \(AxB\)=\(C\)\s+(.*)"),
    "variable": re.compile(r"Total Variable Expenses \(D\)\s+(.*)"),
    "total": re.compile(r"Total Expenses \(D\+E\+F\)=\(G\)\s+(.*)"),
    "rov": re.compile(r"Return Over Variable Expenses \(C-D\)\s+(.*)"),
}


def _nums(s: str) -> list[float]:
    """Only the leading run of numbers; text from the page's side column can follow."""
    m = LEADING_NUMS.match(s)
    return [float(x.replace(",", "")) for x in re.findall(NUM, m.group(1))] if m else []


def _title(text: str) -> str | None:
    first = (text.strip().splitlines() or [""])[0]
    m = re.match(r"2026\s+(.+?)\**\s*$", first)
    return m.group(1).strip() if m else None


def _zones(page, n_values: int) -> list[str]:
    """Zone columns from the header table ('My Farm', 'Brown', ...); else assume all three."""
    for t in page.extract_tables():
        if t and t[0] and (t[0][0] or "").strip() == "My Farm":
            zones = [re.sub(r"\W+$", "", (h or "").replace("\n", " ")).strip() for h in t[0][1:]]
            zones = [z for z in zones if z in ZONES]
            if len(zones) == n_values:
                return zones
    if n_values == 3:
        return list(ZONES)
    raise ValueError(f"can't tell which soil zones the {n_values} columns are")


def parse_page(page, pno: int) -> tuple[str | None, list[dict], list[str]]:
    """Returns (guide title, rows, problems) for one crop page."""
    text = page.extract_text() or ""
    title = _title(text)
    if title not in GUIDE_CROPS:
        return title, [], []
    crop, group = GUIDE_CROPS[title]
    found: dict[str, tuple[str, list[float]]] = {}
    for line in text.splitlines():
        for key, rx in LINES.items():
            m = rx.search(line)
            if m and key not in found:
                unit = m.group(1) if key in ("target_yield", "price") else ""
                found[key] = (unit, _nums(m.groups()[-1]))
    missing = [k for k in LINES if k not in found]
    if missing:
        return title, [], [f"p{pno} {title}: missing lines {missing}"]
    n = len(found["target_yield"][1])
    zones = _zones(page, n)
    unit = "lb" if found["target_yield"][0].startswith("lb") else "bu"
    rows, problems = [], []
    for i, zone in enumerate(zones):
        y, p = found["target_yield"][1][i], found["price"][1][i]
        gross, var = found["gross"][1][i], found["variable"][1][i]
        total, rov = found["total"][1][i], found["rov"][1][i]
        # The guide's own arithmetic; tolerance covers price rounded to cents.
        if abs(y * p - gross) > max(1.0, 0.01 * gross) or abs(gross - var - rov) > 0.05:
            problems.append(f"p{pno} {title} {zone}: failed check (y*p={y * p:.2f} vs C={gross}; "
                            f"C-D={gross - var:.2f} vs {rov})")
            continue
        rows.append({
            "crop": crop, "crop_group": group, "soil_zone": zone, "target_yield": y, "unit": unit,
            # Use C/A so yield x price reproduces the guide's gross revenue exactly
            "price": round(gross / y, 4), "price_shown_in_guide": p,
            "variable_cost_per_acre": var, "total_cost_per_acre": total,
            "guide_gross_revenue": gross, "guide_return_over_variable": rov,
            "guide_name": title, "source_page": pno,
        })
    return title, rows, problems


def extract(pdf_path: str) -> tuple[list[dict], list[str]]:
    import pdfplumber
    rows, problems, seen = [], [], set()
    with pdfplumber.open(pdf_path) as pdf:
        for pno, page in enumerate(pdf.pages, start=1):
            title, r, p = parse_page(page, pno)
            rows += r
            problems += p
            if r:
                seen.add(title)
    for title in GUIDE_CROPS:
        if title not in seen:
            problems.append(f"crop page not found: {title}")
    return rows, problems


def inspect(pdf_path: str) -> None:
    import pdfplumber
    with pdfplumber.open(pdf_path) as pdf:
        print(f"{len(pdf.pages)} pages")
        for pno, page in enumerate(pdf.pages, start=1):
            text = page.extract_text() or ""
            title = _title(text)
            if not title:
                continue
            mark = "*" if title in GUIDE_CROPS else " "
            print(f"{mark} p{pno:<3} {title}")
            if title in GUIDE_CROPS:
                for line in text.splitlines():
                    if any(rx.search(line) for rx in LINES.values()):
                        print(f"        {line.strip()[:110]}")


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
    extra_keys = ("price_shown_in_guide", "guide_gross_revenue", "guide_return_over_variable", "guide_name")
    validated = []
    for r in rows:
        b = CropBudget(**{k: v for k, v in r.items() if k not in extra_keys}, verified=True).model_dump()
        b.update({k: r[k] for k in extra_keys if k in r})  # keep the guide's check values for traceability
        validated.append(b)
    for r in validated:
        if not r["source_page"]:
            raise SystemExit(f"{r['crop']}/{r['soil_zone']} has no source_page")
    zones = {r["soil_zone"] for r in validated}
    if zones != set(ZONES):
        raise SystemExit(f"Need all three soil zones, got {sorted(zones)}; not writing.")
    doc = {
        "meta": {
            "title": "Crop budgets by soil zone",
            "source": source,
            "status": "VERIFIED",
            "note": "From the Saskatchewan Ministry of Agriculture 2026 Crop Planning Guide. source_page = PDF page "
                    "number. Each row passed the guide's own checks (yield x price = gross revenue; gross - "
                    "variable = return over variable expenses). price = gross revenue / target yield.",
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
        for zone in ZONES:
            for crop, group in GUIDE_CROPS.values():
                w.writerow([crop, group, zone, "", "lb" if "Lentil" in crop else "bu", "", "", "", ""])
    print(f"Fill in {MANUAL_CSV} from the guide (one row per crop per zone), then run from-csv.")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    cmd = sys.argv[1]
    if cmd == "inspect":
        inspect(sys.argv[2])
    elif cmd == "extract":
        rows, problems = extract(sys.argv[2])
        for p in problems:
            print("PROBLEM:", p)
        if problems and "--force" not in sys.argv:
            raise SystemExit("Not writing because of the problems above (add --force to write the rest).")
        name = Path(sys.argv[2]).name
        write(rows, f"Saskatchewan Ministry of Agriculture, Crop Planning Guide 2026 ({name}), "
                    "extracted with pdfplumber and checked against the guide's own totals")
    elif cmd == "template":
        template()
    elif cmd == "from-csv":
        write(from_csv(sys.argv[2]), "Saskatchewan Ministry of Agriculture, Crop Planning Guide 2026, "
                                     "hand-entered from the PDF")
    else:
        raise SystemExit(__doc__)
