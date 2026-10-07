"""Pydantic schemas for budgets, farm profiles and AI responses.

Every LLM response is validated here. Invalid output is repaired where safe
(clamping, alias mapping, truncation) and rejected otherwise.
"""
from __future__ import annotations

import json
import re
from typing import Literal, Optional

from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

SOIL_ZONES = ("Brown", "Dark Brown", "Black")
CROP_GROUPS = ("cereal", "oilseed", "pulse", "other")

PRICE_CHANGE_MIN = -60.0
PRICE_CHANGE_MAX = 60.0
MAX_HEADLINE_CHARS = 600


# ---------------------------------------------------------------- budgets
class CropBudget(BaseModel):
    crop: str
    crop_group: Literal["cereal", "oilseed", "pulse", "other"]
    soil_zone: Literal["Brown", "Dark Brown", "Black"]
    target_yield: float = Field(gt=0)
    unit: str
    price: float = Field(gt=0)
    variable_cost_per_acre: float = Field(ge=0)
    total_cost_per_acre: float = Field(ge=0)
    source_page: Optional[int] = None
    verified: bool = False

    @model_validator(mode="after")
    def total_covers_variable(self):
        if self.total_cost_per_acre < self.variable_cost_per_acre:
            raise ValueError(f"{self.crop}/{self.soil_zone}: total cost < variable cost")
        return self


# ---------------------------------------------------------------- farm profile
class RotationLimits(BaseModel):
    """Default rotation limits. These are ASSUMPTIONS, editable in the UI."""
    canola_max_pct: float = Field(33, ge=0, le=100)
    pulses_max_pct: float = Field(33, ge=0, le=100)
    single_crop_max_pct: float = Field(50, ge=1, le=100)
    cereals_min_pct: float = Field(20, ge=0, le=100)


class FarmProfile(BaseModel):
    soil_zone: Literal["Brown", "Dark Brown", "Black"] = "Dark Brown"
    total_acres: float = Field(2000, gt=0, le=200_000)
    crops_allowed: Optional[list[str]] = None  # None = every crop in the zone
    limits: RotationLimits = RotationLimits()


# ---------------------------------------------------------------- AI: scenario
# Maps common names the LLM (or a human) may use onto our budget crop names.
CROP_ALIASES = {
    "canola": "Canola", "canola seed": "Canola", "canola meal": "Canola",
    "canola oil": "Canola", "rapeseed": "Canola",
    "wheat": "CWRS Wheat", "spring wheat": "CWRS Wheat", "cwrs": "CWRS Wheat",
    "cwrs wheat": "CWRS Wheat", "hard red spring wheat": "CWRS Wheat",
    "durum": "Durum", "durum wheat": "Durum",
    "barley": "Barley", "feed barley": "Barley", "malt barley": "Barley",
    "oats": "Oats", "oat": "Oats",
    "peas": "Yellow Peas", "pea": "Yellow Peas", "yellow peas": "Yellow Peas",
    "field peas": "Yellow Peas", "dry peas": "Yellow Peas",
    "lentils": "Red Lentils", "lentil": "Red Lentils", "red lentils": "Red Lentils",
    "flax": "Flax", "flaxseed": "Flax",
}


def normalize_crop(name: str, allowed: list[str]) -> Optional[str]:
    """Return the matching budget crop name, or None if unknown."""
    key = name.strip().lower()
    for crop in allowed:
        if crop.lower() == key:
            return crop
    mapped = CROP_ALIASES.get(key)
    return mapped if mapped in allowed else None


def _clamp_pct(v) -> float:
    v = float(v)
    if v != v:  # NaN
        raise ValueError("price change is NaN")
    return max(PRICE_CHANGE_MIN, min(PRICE_CHANGE_MAX, v))


# When the model gives no range, widen the base estimate by confidence.
# ASSUMPTION (labelled in the UI): low confidence -> +/-100% of the estimate, etc.
RANGE_WIDTH_BY_CONFIDENCE = {"high": 0.25, "medium": 0.5, "low": 1.0}


class AffectedCrop(BaseModel):
    crop: str
    price_change_pct: float
    confidence: Literal["low", "medium", "high"] = "low"
    reasoning: str = ""
    low_pct: Optional[float] = None    # bear case (worse for the farm)
    high_pct: Optional[float] = None   # bull case (better for the farm)
    range_assumed: bool = False

    @field_validator("price_change_pct", mode="before")
    @classmethod
    def clamp_pct(cls, v):
        return _clamp_pct(v)

    @field_validator("low_pct", "high_pct", mode="before")
    @classmethod
    def clamp_range(cls, v):
        if v is None or v == "":
            return None
        try:
            return _clamp_pct(v)
        except (TypeError, ValueError):
            return None

    @model_validator(mode="after")
    def order_range(self):
        base = self.price_change_pct
        if self.low_pct is None or self.high_pct is None:
            k = RANGE_WIDTH_BY_CONFIDENCE.get(self.confidence, 1.0)
            spread = abs(base) * k
            self.low_pct = _clamp_pct(base - spread)
            self.high_pct = _clamp_pct(base + spread)
            self.range_assumed = True
        # Repair: low <= base <= high
        self.low_pct = min(self.low_pct, base)
        self.high_pct = max(self.high_pct, base)
        return self

    @field_validator("confidence", mode="before")
    @classmethod
    def norm_conf(cls, v):
        v = str(v).strip().lower()
        return v if v in ("low", "medium", "high") else "low"

    @field_validator("reasoning", mode="before")
    @classmethod
    def trim_reason(cls, v):
        return str(v or "")[:400]


class Scenario(BaseModel):
    affected: list[AffectedCrop] = []
    duration_months: int = 12
    assumptions: list[str] = []
    is_trade_related: bool = True
    dropped_crops: list[str] = []  # crops the LLM named that we have no budget for

    @field_validator("duration_months", mode="before")
    @classmethod
    def clamp_duration(cls, v):
        try:
            v = int(round(float(v)))
        except (TypeError, ValueError):
            v = 12
        return max(0, min(60, v))

    @field_validator("assumptions", mode="before")
    @classmethod
    def trim_assumptions(cls, v):
        return [str(a)[:300] for a in (v or [])][:8]

    def price_changes(self) -> dict[str, float]:
        return {a.crop: a.price_change_pct for a in self.affected}

    def cases(self) -> dict[str, dict[str, float]]:
        """Bear / base / bull price changes for stress testing."""
        return {
            "bear": {a.crop: a.low_pct for a in self.affected},
            "base": self.price_changes(),
            "bull": {a.crop: a.high_pct for a in self.affected},
        }


def _extract_json(text: str) -> dict:
    """Parse JSON, tolerating code fences or prose around one object."""
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            raise ValueError("no JSON object found in model output")
        return json.loads(match.group(0))


def repair_scenario(raw, allowed_crops: list[str]) -> Scenario:
    """Validate an LLM scenario (dict or JSON text) and repair what is safe.

    - unknown crops are dropped (listed in dropped_crops)
    - aliases map to budget crops ("canola meal" -> Canola)
    - duplicates keep the first entry
    - price changes are clamped to -60..+60
    Raises ValueError if the output can't be salvaged.
    """
    data = _extract_json(raw) if isinstance(raw, str) else dict(raw)
    if not isinstance(data, dict):
        raise ValueError("scenario must be a JSON object")

    affected, dropped, seen = [], [], set()
    for item in data.get("affected") or []:
        if not isinstance(item, dict) or "crop" not in item:
            continue
        crop = normalize_crop(str(item["crop"]), allowed_crops)
        if crop is None:
            dropped.append(str(item["crop"])[:60])
            continue
        if crop in seen:
            continue
        try:
            affected.append(AffectedCrop(**{**item, "crop": crop}))
            seen.add(crop)
        except (ValidationError, ValueError, TypeError):
            dropped.append(str(item["crop"])[:60])

    try:
        return Scenario(
            affected=affected,
            duration_months=data.get("duration_months", 12),
            assumptions=data.get("assumptions") or [],
            is_trade_related=bool(data.get("is_trade_related", True)),
            dropped_crops=dropped,
        )
    except ValidationError as exc:
        raise ValueError(f"invalid scenario: {exc}") from exc


def clean_headline(text: str) -> str:
    """Strip control characters and cap length. The headline is untrusted data."""
    text = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", " ", str(text or ""))
    return re.sub(r"\s+", " ", text).strip()[:MAX_HEADLINE_CHARS]
