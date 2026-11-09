"""Train model versions from TOML configs into signed, hash-verified artifacts.

Discipline:
- fit on ``train`` only (calibration uses CV folds inside ``train``);
- choose the decision threshold on ``val`` only;
- report on the frozen ``test`` split, which no fitting or selection step reads.
"""

from __future__ import annotations

import hashlib
import json
import platform
import subprocess
import sys
import time
import tomllib
from pathlib import Path
from typing import Any

import numpy as np
import sklearn
from sklearn.calibration import CalibratedClassifierCV
from sklearn.compose import ColumnTransformer
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import FunctionTransformer, OneHotEncoder, StandardScaler

from . import artifacts as art
from .data import load_split_csv
from .metrics import choose_threshold, full_report, percentiles
from .paths import CONFIG_DIR, PROJECT_ROOT, StatePaths
from .schema import FEATURE_NAMES, LABEL, MERCHANT_CATEGORIES, SCHEMA_VERSION, schema_dict, schema_hash

CONTRACT = "model_artifact_v1"


def load_config(path: Path) -> dict[str, Any]:
    with Path(path).open("rb") as fh:
        cfg = tomllib.load(fh)
    cfg["_path"] = str(Path(path).resolve().relative_to(PROJECT_ROOT))
    cfg["_sha256"] = hashlib.sha256(Path(path).read_bytes()).hexdigest()
    return cfg


def model_configs() -> list[Path]:
    return sorted((CONFIG_DIR / "models").glob("*.toml"))


def _idx(*names: str) -> list[int]:
    return [FEATURE_NAMES.index(n) for n in names]


def build_estimator(cfg: dict[str, Any]) -> Any:
    p = cfg.get("params", {})
    if cfg["family"] == "logreg":
        ct = ColumnTransformer(
            [
                (
                    "cat",
                    OneHotEncoder(categories=[list(range(len(MERCHANT_CATEGORIES)))]),
                    _idx("merchant_category"),
                ),
                (
                    "log",
                    Pipeline([("log1p", FunctionTransformer(np.log1p)), ("scale", StandardScaler())]),
                    _idx("amount", "account_age_days", "txn_count_24h", "distance_from_home_km"),
                ),
                (
                    "num",
                    StandardScaler(),
                    _idx("hour_of_day", "device_trust_score", "card_present", "country_mismatch"),
                ),
            ]
        )
        return Pipeline(
            [
                ("features", ct),
                ("model", LogisticRegression(C=p.get("C", 1.0), max_iter=p.get("max_iter", 2000))),
            ]
        )
    if cfg["family"] == "hgb_calibrated":
        cat_mask = [n == "merchant_category" for n in FEATURE_NAMES]
        hgb = HistGradientBoostingClassifier(
            max_iter=p.get("max_iter", 200),
            learning_rate=p.get("learning_rate", 0.08),
            max_leaf_nodes=p.get("max_leaf_nodes", 31),
            l2_regularization=p.get("l2_regularization", 1.0),
            categorical_features=cat_mask,
            random_state=cfg["seed"],
        )
        return CalibratedClassifierCV(hgb, method=p.get("calibration", "isotonic"), cv=p.get("calibration_cv", 3))
    raise ValueError(f"unknown model family {cfg['family']!r}")


def apply_data_change(cfg: dict[str, Any], X: np.ndarray, y: np.ndarray) -> tuple[np.ndarray, np.ndarray, dict]:
    """Apply a config-declared training-data change and report exactly what it touched."""
    dc = cfg.get("data_change")
    if not dc:
        return X, y, {"kind": "none"}
    cp = X[:, FEATURE_NAMES.index("card_present")] == (1.0 if dc["card_present"] else 0.0)
    seg = cp & (X[:, FEATURE_NAMES.index("amount")] > dc["amount_gt"])
    if dc["kind"] == "label_snapshot_immature":
        y2 = y.copy()
        flipped = int((y2[seg] == 1).sum())
        y2[seg] = 0
        return X, y2, {**dc, "rows_in_segment": int(seg.sum()), "positives_relabeled_0": flipped}
    if dc["kind"] == "row_filter":
        return (
            X[~seg],
            y[~seg],
            {**dc, "rows_dropped": int(seg.sum()), "positives_dropped": int(y[seg].sum())},
        )
    raise ValueError(f"unknown data_change kind {dc['kind']!r}")


def git_info() -> dict[str, Any]:
    def run(*args: str) -> str:
        try:
            return subprocess.run(
                ["git", *args], cwd=PROJECT_ROOT, capture_output=True, text=True, timeout=10
            ).stdout.strip()
        except Exception:
            return ""

    commit = run("rev-parse", "HEAD")
    dirty = bool(run("status", "--porcelain", "--", "src", "configs"))
    return {"commit": commit or None, "dirty": dirty if commit else None}


def measure_latency(model: Any, X: np.ndarray, n: int = 300) -> dict[str, Any]:
    """Single-row predict_proba latency in-process (what one API request pays for the model call)."""
    rows = X[:n]
    model.predict_proba(rows[:1])  # warm-up
    times = []
    for i in range(len(rows)):
        t0 = time.perf_counter()
        model.predict_proba(rows[i : i + 1])
        times.append((time.perf_counter() - t0) * 1000)
    return {
        "unit": "ms",
        "method": f"{len(rows)} sequential single-row predict_proba calls, in-process",
        **percentiles(times),
    }


def prediction_hash(p: np.ndarray) -> str:
    return hashlib.sha256(np.round(p, 6).astype("<f8").tobytes()).hexdigest()


def train_one(cfg: dict[str, Any], paths: StatePaths, dataset_manifest: dict[str, Any]) -> dict[str, Any]:
    from .serving import limit_native_threads

    limit_native_threads()
    X_tr, y_tr, _ = load_split_csv(paths.data / "train.csv")
    X_va, y_va, _ = load_split_csv(paths.data / "val.csv")
    X_te, y_te, _ = load_split_csv(paths.data / "test.csv")
    X_fit, y_fit, change = apply_data_change(cfg, X_tr, y_tr)
    est = build_estimator(cfg)
    t0 = time.perf_counter()
    est.fit(X_fit, y_fit)
    fit_s = time.perf_counter() - t0
    threshold = choose_threshold(y_va, est.predict_proba(X_va)[:, 1])
    p_te = est.predict_proba(X_te)[:, 1]
    test_report = full_report(y_te, p_te, threshold, X_te, seed=cfg["seed"])
    val_report = full_report(y_va, est.predict_proba(X_va)[:, 1], threshold, None, seed=cfg["seed"])

    version_dir = paths.artifacts / cfg["version"]
    if (version_dir / "model.skops").exists():
        raise art.ArtifactError(f"{cfg['version']} already trained at {version_dir}; versions are immutable")
    model_path, sha, size = art.save_model(est, version_dir)
    untrusted = art.untrusted_types(model_path)
    latency = measure_latency(est, X_te)
    manifest = {
        "contract": CONTRACT,
        "model_name": cfg["model_name"],
        "version": cfg["version"],
        "family": cfg["family"],
        "owner": cfg["owner"],
        "intended_use": cfg["intended_use"],
        "change_summary": cfg["change_summary"],
        "deliberately_degraded": bool(cfg.get("deliberately_degraded", False)),
        "artifact": {
            "format": "skops",
            "file": "model.skops",
            "sha256": sha,
            "bytes": size,
            "untrusted_types_outside_allowlist": untrusted,
        },
        "input_schema": {**schema_dict(), "schema_sha256": schema_hash()},
        "output_schema": {
            "score": {"type": "number", "minimum": 0, "maximum": 1, "meaning": f"P({LABEL}=1)"},
            "decision": {"type": "boolean", "meaning": "score >= threshold (route to manual review)"},
        },
        "threshold": {"value": round(threshold, 6), "selected_on": "val", "rule": "max F1"},
        "training": {
            "config_path": cfg["_path"],
            "config_sha256": cfg["_sha256"],
            "params": cfg.get("params", {}),
            "seed": cfg["seed"],
            "data_change": change,
            "fit_seconds": round(fit_s, 3),
            "train_rows_used": len(y_fit),
            "train_positives_used": int(y_fit.sum()),
        },
        "dataset": {
            "name": dataset_manifest["dataset"],
            "dataset_hash": dataset_manifest["dataset_hash"],
            "generator_version": dataset_manifest["generator_version"],
            "splits": {k: v["sha256"] for k, v in dataset_manifest["files"].items()},
        },
        "source": git_info(),
        "environment": {
            "python": platform.python_version(),
            "sklearn": sklearn.__version__,
            "numpy": np.__version__,
            "platform": platform.platform(),
            "machine": platform.machine(),
        },
        "metrics": {
            "test": test_report,
            "val": {k: val_report[k] for k in ("pr_auc", "f1", "recall", "precision")},
        },
        "latency_single_row": latency,
        "test_prediction_sha256": prediction_hash(p_te),
        "schema_version": SCHEMA_VERSION,
    }
    (version_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True))
    (version_dir / "MODEL_CARD.md").write_text(model_card(manifest))
    return manifest


def model_card(m: dict[str, Any]) -> str:
    t = m["metrics"]["test"]
    lines = [
        f"# Model card: {m['model_name']} {m['version']}",
        "",
        f"- **Family:** {m['family']}  ",
        f"- **Owner:** {m['owner']}  ",
        f"- **Intended use:** {m['intended_use']}  ",
        f"- **Change:** {m['change_summary']}  ",
        f"- **Deliberately degraded (demo):** {'yes' if m['deliberately_degraded'] else 'no'}  ",
        f"- **Artifact:** `{m['artifact']['file']}` sha256 `{m['artifact']['sha256']}` "
        f"({m['artifact']['bytes']} bytes)",
        f"- **Dataset:** {m['dataset']['name']} hash `{m['dataset']['dataset_hash'][:16]}…` (synthetic)",
        f"- **Source commit:** `{m['source']['commit']}` dirty={m['source']['dirty']}",
        "",
        "## Held-out test metrics (frozen split, synthetic data)",
        "",
        "| metric | value |",
        "|---|---|",
        f"| rows / positives | {t['n']} / {t['positives']} ({t['positive_rate']:.2%}) |",
        f"| PR-AUC (95% bootstrap CI) | {t['pr_auc']} ({t['pr_auc_ci95'][0]}–{t['pr_auc_ci95'][1]}) |",
        f"| ROC-AUC | {t['roc_auc']} |",
        f"| threshold (chosen on val, max F1) | {t['threshold']} |",
        f"| precision (95% CI) | {t['precision']} ({t['precision_ci95'][0]}–{t['precision_ci95'][1]}) |",
        f"| recall (95% CI) | {t['recall']} ({t['recall_ci95'][0]}–{t['recall_ci95'][1]}) |",
        f"| F1 | {t['f1']} |",
        f"| Brier / ECE | {t['brier']} / {t['ece']} |",
        f"| single-row latency p50 / p95 (ms) | {m['latency_single_row']['p50']} / {m['latency_single_row']['p95']} |",
        "",
        "## Slices",
        "",
        "| slice | n | positives | recall | precision |",
        "|---|---|---|---|---|",
    ]
    for name, s in t["slices"].items():
        lines.append(f"| {name} | {s['n']} | {s['positives']} | {s['recall']} | {s['precision']} |")
    lines += [
        "",
        "## Limitations",
        "",
        "- Trained and evaluated on a synthetic, seeded dataset; numbers say nothing about real fraud rates.",
        "- The threshold is tuned for F1 on the validation split; a real review team would set it from capacity.",
        "- Under distribution shift the score's calibration is not guaranteed; see docs/DRIFT_AND_LIMITATIONS.md.",
    ]
    if m["training"]["data_change"].get("kind") not in (None, "none"):
        lines.append(f"- Training data change: {json.dumps(m['training']['data_change'])}")
    return "\n".join(lines) + "\n"


def main_train_all(
    paths: StatePaths, dataset_manifest: dict[str, Any], log: Any = print, only: list[Path] | None = None
) -> list[dict]:
    out = []
    for cfg_path in only if only is not None else model_configs():
        cfg = load_config(cfg_path)
        log(f"training {cfg['version']} ({cfg['family']}) …")
        m = train_one(cfg, paths, dataset_manifest)
        t = m["metrics"]["test"]
        log(
            f"  {cfg['version']}: test PR-AUC {t['pr_auc']} F1 {t['f1']} recall {t['recall']} "
            f"precision {t['precision']} sha256 {m['artifact']['sha256'][:12]}"
        )
        out.append(m)
    return out


if __name__ == "__main__":  # pragma: no cover
    sys.exit("use `scw train-fixtures`")
