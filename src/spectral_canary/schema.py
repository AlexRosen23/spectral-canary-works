"""Feature schema shared by training, serving and monitoring.

The schema is the contract between callers and every model version. A request that does not match it
is rejected before any model runs (HTTP 422) and counted as a schema failure, so a schema break can
never silently reach a model.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

import numpy as np

MERCHANT_CATEGORIES: tuple[str, ...] = (
    "grocery",
    "fuel",
    "restaurants",
    "online_retail",
    "electronics",
    "travel",
    "gaming",
    "gift_cards",
)


@dataclass(frozen=True)
class Feature:
    name: str
    kind: str  # "float" | "int" | "bool" | "category"
    minimum: float | None = None
    maximum: float | None = None
    categories: tuple[str, ...] = ()
    description: str = ""


FEATURES: tuple[Feature, ...] = (
    Feature("amount", "float", 0.01, 50_000.0, description="Transaction amount in USD."),
    Feature("merchant_category", "category", categories=MERCHANT_CATEGORIES, description="Merchant type."),
    Feature("hour_of_day", "int", 0, 23, description="Local hour the transaction was made."),
    Feature("account_age_days", "int", 0, 20_000, description="Days since the account was opened."),
    Feature("txn_count_24h", "int", 0, 500, description="Transactions on the card in the prior 24 hours."),
    Feature("distance_from_home_km", "float", 0.0, 20_000.0, description="Distance from billing address."),
    Feature("device_trust_score", "float", 0.0, 1.0, description="Device reputation score, 1 = trusted."),
    Feature("card_present", "bool", description="Physical card was present."),
    Feature("country_mismatch", "bool", description="Card country differs from merchant country."),
)

FEATURE_NAMES: tuple[str, ...] = tuple(f.name for f in FEATURES)
LABEL = "is_fraud"
SCHEMA_VERSION = "txn-risk-v1"


class SchemaError(ValueError):
    """Raised when a payload does not satisfy the feature schema."""

    def __init__(self, problems: list[str]):
        super().__init__("; ".join(problems))
        self.problems = problems


def schema_dict() -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "label": LABEL,
        "features": [
            {
                "name": f.name,
                "kind": f.kind,
                "minimum": f.minimum,
                "maximum": f.maximum,
                "categories": list(f.categories),
                "description": f.description,
            }
            for f in FEATURES
        ],
    }


def schema_hash() -> str:
    return hashlib.sha256(json.dumps(schema_dict(), sort_keys=True).encode()).hexdigest()


def validate_record(record: dict[str, Any]) -> list[str]:
    """Return a list of human-readable problems (empty when valid)."""
    problems: list[str] = []
    extra = set(record) - set(FEATURE_NAMES)
    if extra:
        problems.append(f"unexpected fields: {sorted(extra)}")
    for f in FEATURES:
        if f.name not in record:
            problems.append(f"missing field: {f.name}")
            continue
        v = record[f.name]
        if f.kind == "category":
            if not isinstance(v, str) or v not in f.categories:
                problems.append(f"{f.name}: unknown category {v!r}")
            continue
        if f.kind == "bool":
            if not isinstance(v, bool):
                problems.append(f"{f.name}: expected bool, got {type(v).__name__}")
            continue
        if isinstance(v, bool) or not isinstance(v, int | float):
            problems.append(f"{f.name}: expected number, got {type(v).__name__}")
            continue
        if f.kind == "int" and not float(v).is_integer():
            problems.append(f"{f.name}: expected integer, got {v!r}")
            continue
        if not np.isfinite(float(v)):
            problems.append(f"{f.name}: not finite")
            continue
        if f.minimum is not None and v < f.minimum:
            problems.append(f"{f.name}: {v} below minimum {f.minimum}")
        if f.maximum is not None and v > f.maximum:
            problems.append(f"{f.name}: {v} above maximum {f.maximum}")
    return problems


def encode_records(records: list[dict[str, Any]]) -> np.ndarray:
    """Encode validated records into the numeric matrix every model consumes.

    Column order follows FEATURE_NAMES; categories become their index in MERCHANT_CATEGORIES.
    """
    out = np.empty((len(records), len(FEATURES)), dtype=np.float64)
    cat_index = {c: i for i, c in enumerate(MERCHANT_CATEGORIES)}
    for i, r in enumerate(records):
        for j, f in enumerate(FEATURES):
            v = r[f.name]
            if f.kind == "category":
                out[i, j] = cat_index[v]
            elif f.kind == "bool":
                out[i, j] = 1.0 if v else 0.0
            else:
                out[i, j] = float(v)
    return out


def decode_row(row: np.ndarray) -> dict[str, Any]:
    """Inverse of encode_records for one row (used to build realistic JSON payloads)."""
    rec: dict[str, Any] = {}
    for j, f in enumerate(FEATURES):
        v = row[j]
        if f.kind == "category":
            rec[f.name] = MERCHANT_CATEGORIES[int(v)]
        elif f.kind == "bool":
            rec[f.name] = bool(v >= 0.5)
        elif f.kind == "int":
            rec[f.name] = int(v)
        else:
            rec[f.name] = round(float(v), 4)
    return rec
