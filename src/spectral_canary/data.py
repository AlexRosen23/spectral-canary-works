"""Reproducible synthetic transaction-risk dataset with a documented ground-truth mechanism.

Why synthetic: no license or privacy constraints, and because the label mechanism is known, the
simulated production traffic can deliver *delayed ground-truth labels* to the canary monitor, which a
static public benchmark cannot do. Every row is produced by ``generate`` from a seed; the exact bytes
are hashed into ``manifests/dataset_manifest.json`` so any drift in the generator is detected.

Label definition: ``is_fraud = 1`` means the transaction was later confirmed as a fraudulent chargeback.
It is drawn as Bernoulli(sigmoid(z)) where ``z`` is the risk function in ``_risk_logit`` (documented in
docs/DATA_CARD.md). Nothing here represents real customers or real payments.
"""

from __future__ import annotations

import csv
import hashlib
import io
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from .schema import FEATURE_NAMES, LABEL, MERCHANT_CATEGORIES, decode_row

GENERATOR_VERSION = "txn-risk-gen-1.0"

# Per-category generation parameters: (baseline share, P(card present), median amount USD).
_CATEGORY_PARAMS: dict[str, tuple[float, float, float]] = {
    "grocery": (0.22, 0.85, 45.0),
    "fuel": (0.12, 0.90, 40.0),
    "restaurants": (0.18, 0.85, 35.0),
    "online_retail": (0.18, 0.02, 70.0),
    "electronics": (0.08, 0.40, 220.0),
    "travel": (0.07, 0.30, 350.0),
    "gaming": (0.08, 0.02, 25.0),
    "gift_cards": (0.07, 0.35, 80.0),
}
_CATEGORY_RISK = {
    "grocery": -0.5,
    "fuel": -0.4,
    "restaurants": -0.2,
    "online_retail": 0.2,
    "electronics": 0.5,
    "travel": 0.1,
    "gaming": 0.4,
    "gift_cards": 1.0,
}


@dataclass(frozen=True)
class Scenario:
    """A population the generator can draw from. ``baseline`` is the training population."""

    name: str
    category_shares: dict[str, float] | None = None
    amount_log_shift: float = 0.0
    card_present_scale: float = 1.0
    device_trust_beta: tuple[float, float] = (5.0, 2.0)
    description: str = ""


SCENARIOS: dict[str, Scenario] = {
    "baseline": Scenario("baseline", description="Training-time population."),
    "high_value_online_surge": Scenario(
        "high_value_online_surge",
        category_shares={
            "grocery": 0.08,
            "fuel": 0.04,
            "restaurants": 0.06,
            "online_retail": 0.30,
            "electronics": 0.22,
            "travel": 0.06,
            "gaming": 0.10,
            "gift_cards": 0.14,
        },
        amount_log_shift=1.4,
        card_present_scale=0.35,
        device_trust_beta=(3.0, 2.5),
        description=(
            "SIMULATED distribution shift: holiday-style surge of high-value card-not-present online "
            "purchases on lower-trust devices."
        ),
    ),
}


def _sigmoid(z: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-z))


def _risk_logit(cols: dict[str, np.ndarray]) -> np.ndarray:
    amount = cols["amount"]
    cnp = 1.0 - cols["card_present"]
    night = (cols["hour_of_day"] < 5).astype(float)
    cat = cols["merchant_category"].astype(int)
    cat_risk = np.array([_CATEGORY_RISK[c] for c in MERCHANT_CATEGORIES])[cat]
    gift = (cat == MERCHANT_CATEGORIES.index("gift_cards")).astype(float)
    z = (
        -4.6
        + 1.1 * (np.log(amount) - np.log(80.0))
        + 1.3 * cnp
        + 1.3 * cols["country_mismatch"]
        + 1.6 * (0.6 - cols["device_trust_score"])
        + 1.8 * (cols["device_trust_score"] < 0.3)
        + 0.8 * night
        + 0.5 * np.log1p(cols["txn_count_24h"])
        - 0.7 * np.log1p(cols["account_age_days"] / 365.0)
        + 0.4 * np.log1p(cols["distance_from_home_km"] / 50.0)
        + cat_risk
        # Non-linear interactions a linear model cannot represent exactly.
        + 1.8 * cnp * (amount > 400.0)
        + 1.0 * night * gift
        + 1.2 * cols["country_mismatch"] * cnp
        + 0.9 * (cols["txn_count_24h"] >= 8) * (cols["account_age_days"] < 120)
    )
    return z


def generate(n: int, seed: int, scenario: str = "baseline") -> tuple[np.ndarray, np.ndarray]:
    """Return (X, y): X columns follow FEATURE_NAMES with categories encoded as indices."""
    sc = SCENARIOS[scenario]
    rng = np.random.default_rng(seed)
    shares = sc.category_shares or {c: p[0] for c, p in _CATEGORY_PARAMS.items()}
    probs = np.array([shares[c] for c in MERCHANT_CATEGORIES], dtype=float)
    probs = probs / probs.sum()
    cat = rng.choice(len(MERCHANT_CATEGORIES), size=n, p=probs)
    p_card = np.array([_CATEGORY_PARAMS[c][1] for c in MERCHANT_CATEGORIES])[cat] * sc.card_present_scale
    card_present = (rng.random(n) < p_card).astype(float)
    median_amt = np.array([_CATEGORY_PARAMS[c][2] for c in MERCHANT_CATEGORIES])[cat]
    amount = np.exp(np.log(median_amt) + sc.amount_log_shift + rng.normal(0.0, 0.8, n))
    amount = np.round(np.clip(amount, 0.5, 49_999.0), 2)
    day = rng.random(n) < 0.88
    hour = np.where(day, rng.normal(14.0, 3.5, n), rng.normal(2.0, 1.8, n))
    hour = np.mod(np.round(hour), 24).astype(float)
    account_age = np.clip(np.round(rng.exponential(1500.0, n)), 0, 20_000)
    txn_count = np.clip(rng.poisson(2.5, n) + rng.binomial(1, 0.04, n) * rng.poisson(9.0, n), 0, 500)
    distance = np.round(np.clip(np.exp(rng.normal(np.log(8.0), 1.2, n)), 0.0, 19_999.0), 2)
    a, b = sc.device_trust_beta
    device_trust = np.round(rng.beta(a, b, n), 4)
    travel = cat == MERCHANT_CATEGORIES.index("travel")
    country_mismatch = (rng.random(n) < np.where(travel, 0.30, 0.04)).astype(float)
    cols = {
        "amount": amount,
        "merchant_category": cat.astype(float),
        "hour_of_day": hour,
        "account_age_days": account_age,
        "txn_count_24h": txn_count.astype(float),
        "distance_from_home_km": distance,
        "device_trust_score": device_trust,
        "card_present": card_present,
        "country_mismatch": country_mismatch,
    }
    y = (rng.random(n) < _sigmoid(_risk_logit(cols))).astype(np.int64)
    X = np.column_stack([cols[name] for name in FEATURE_NAMES])
    return X, y


@dataclass(frozen=True)
class DatasetConfig:
    n_rows: int = 40_000
    seed: int = 20261108
    train_frac: float = 0.6
    val_frac: float = 0.2


@dataclass
class Splits:
    X_train: np.ndarray
    y_train: np.ndarray
    X_val: np.ndarray
    y_val: np.ndarray
    X_test: np.ndarray
    y_test: np.ndarray
    row_ids_train: np.ndarray
    row_ids_val: np.ndarray
    row_ids_test: np.ndarray


def split(X: np.ndarray, y: np.ndarray, cfg: DatasetConfig) -> Splits:
    """Deterministic shuffled split by row id (no row can appear in two splits)."""
    rng = np.random.default_rng(cfg.seed + 1)
    idx = rng.permutation(len(y))
    n_train = int(len(y) * cfg.train_frac)
    n_val = int(len(y) * cfg.val_frac)
    tr, va, te = idx[:n_train], idx[n_train : n_train + n_val], idx[n_train + n_val :]
    return Splits(X[tr], y[tr], X[va], y[va], X[te], y[te], tr, va, te)


def to_csv_bytes(X: np.ndarray, y: np.ndarray, row_ids: np.ndarray) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(["row_id", *FEATURE_NAMES, LABEL])
    for rid, row, label in zip(row_ids, X, y, strict=True):
        rec = decode_row(row)
        w.writerow([int(rid), *[rec[n] for n in FEATURE_NAMES], int(label)])
    return buf.getvalue().encode()


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def build_dataset(out_dir: Path, cfg: DatasetConfig | None = None) -> dict[str, Any]:
    """Generate, split and write the dataset; return its manifest."""
    cfg = cfg or DatasetConfig()
    X, y = generate(cfg.n_rows, cfg.seed)
    s = split(X, y, cfg)
    out_dir.mkdir(parents=True, exist_ok=True)
    files: dict[str, Any] = {}
    for name, (Xs, ys, ids) in {
        "train": (s.X_train, s.y_train, s.row_ids_train),
        "val": (s.X_val, s.y_val, s.row_ids_val),
        "test": (s.X_test, s.y_test, s.row_ids_test),
    }.items():
        b = to_csv_bytes(Xs, ys, ids)
        (out_dir / f"{name}.csv").write_bytes(b)
        files[name] = {
            "file": f"{name}.csv",
            "rows": len(ys),
            "positives": int(ys.sum()),
            "positive_rate": round(float(ys.mean()), 5),
            "sha256": sha256_bytes(b),
        }
    manifest = {
        "dataset": "spectral-txn-risk",
        "generator_version": GENERATOR_VERSION,
        "kind": "synthetic (reproducible, seeded); no real people or payments",
        "license": "MIT (generated by this repository's code)",
        "label": LABEL,
        "label_definition": "1 = transaction later confirmed as fraudulent chargeback (synthetic mechanism)",
        "seed": cfg.seed,
        "n_rows": cfg.n_rows,
        "split": {
            "method": "seeded permutation by row id",
            "train": cfg.train_frac,
            "val": cfg.val_frac,
            "test": round(1 - cfg.train_frac - cfg.val_frac, 4),
            "permutation_seed": cfg.seed + 1,
        },
        "files": files,
    }
    manifest["dataset_hash"] = sha256_bytes("".join(files[k]["sha256"] for k in ("train", "val", "test")).encode())
    return manifest


def load_split_csv(path: Path) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Load a split written by build_dataset back into (X, y, row_ids)."""
    from .schema import encode_records

    with path.open() as fh:
        rows = list(csv.DictReader(fh))
    recs: list[dict[str, Any]] = []
    for r in rows:
        rec: dict[str, Any] = {}
        for name in FEATURE_NAMES:
            v = r[name]
            if name == "merchant_category":
                rec[name] = v
            elif name in ("card_present", "country_mismatch"):
                rec[name] = v == "True"
            elif name in ("hour_of_day", "account_age_days", "txn_count_24h"):
                rec[name] = int(v)
            else:
                rec[name] = float(v)
        recs.append(rec)
    X = encode_records(recs)
    y = np.array([int(r[LABEL]) for r in rows], dtype=np.int64)
    ids = np.array([int(r["row_id"]) for r in rows], dtype=np.int64)
    return X, y, ids
