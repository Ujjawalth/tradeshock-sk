import io
from datetime import date
from pathlib import Path

import ai
import app as app_module
import lease

SAMPLE = (Path(__file__).resolve().parents[1] / "data" / "sample_surface_lease.txt").read_text(encoding="utf-8")


def test_offline_extract_sample():
    out = lease.extract(SAMPLE, today=date(2026, 10, 6))
    assert out["method"] == "regex" and out["is_lease"] and out["synthetic"]
    assert out["annual_payment"] == 4850.0 and "$4,850.00" in out["payment_quote"]
    assert out["next_review_date"] == "2028-04-15" and out["months_to_review"] == 18
    assert out["lessee"] == "Northwind Sample Energy Corp." and out["acres"] == 4.2


def test_verify_rejects_hallucinated_values():
    raw = {"is_lease": True, "annual_payment": 9999.0, "payment_quote": "annual compensation of $9,999.00",
           "next_review_date": "2027-01-01", "review_quote": "The next compensation review date is April 15, 2028.",
           "lessee": "x", "acres": 4.2}
    out, issues = lease.verify(raw, SAMPLE)
    assert out["annual_payment"] is None and out["next_review_date"] is None
    assert len(issues) == 2


def test_ai_path_with_verified_quotes(monkeypatch):
    good = {"is_lease": True, "annual_payment": 4850, "lessee": "Northwind Sample Energy Corp.", "acres": 4.2,
            "payment_quote": "The Lessee shall pay the Lessor an annual compensation of $4,850.00 per year",
            "next_review_date": "2028-04-15", "review_quote": "The next compensation review date is April 15, 2028."}
    monkeypatch.setattr(ai, "available", lambda: True)
    monkeypatch.setattr(lease, "ai_extract", lambda doc: good)
    out = lease.extract(SAMPLE, today=date(2026, 10, 6))
    assert out["method"] == "ai" and out["annual_payment"] == 4850 and not out["issues"]


def test_ai_hallucination_falls_back_to_regex(monkeypatch):
    bad = {"is_lease": True, "annual_payment": 12000, "payment_quote": "pays $12,000 per year",
           "next_review_date": None, "review_quote": "", "lessee": "", "acres": None}
    monkeypatch.setattr(ai, "available", lambda: True)
    monkeypatch.setattr(lease, "ai_extract", lambda doc: bad)
    out = lease.extract(SAMPLE)
    assert out["method"] == "regex" and out["annual_payment"] == 4850.0


def test_lease_api_sample_text_and_file():
    app_module.ai_limiter.hits.clear()
    c = app_module.app.test_client()
    assert c.post("/api/lease", json={"sample": True}).get_json()["lease"]["annual_payment"] == 4850.0
    assert c.post("/api/lease", json={"text": "hi"}).status_code == 400
    res = c.post("/api/lease", data={"file": (io.BytesIO(SAMPLE.encode()), "lease.txt")},
                 content_type="multipart/form-data")
    assert res.status_code == 200 and res.get_json()["lease"]["next_review_date"] == "2028-04-15"
    res = c.post("/api/lease", data={"file": (io.BytesIO(b"%PDF-broken"), "x.pdf")},
                 content_type="multipart/form-data")
    assert res.status_code == 400
