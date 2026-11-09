"""Evaluation statistics with honest uncertainty.

Positive rate is ~6%, so accuracy and ROC-AUC flatter every model. The primary metrics are PR-AUC
(average precision) and precision/recall/F1 at the validation-chosen threshold, plus Brier score and
expected calibration error because downstream consumers read the score as a probability.
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np
from sklearn.metrics import average_precision_score, brier_score_loss, precision_recall_curve, roc_auc_score


def wilson_interval(successes: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 1.0)
    p = successes / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


def newcombe_diff_interval(s1: int, n1: int, s2: int, n2: int, z: float = 1.96) -> tuple[float, float]:
    """95% CI for p1 - p2 (independent proportions), Newcombe hybrid score method."""
    if n1 == 0 or n2 == 0:
        return (-1.0, 1.0)
    p1, p2 = s1 / n1, s2 / n2
    l1, u1 = wilson_interval(s1, n1, z)
    l2, u2 = wilson_interval(s2, n2, z)
    d = p1 - p2
    lo = d - math.sqrt((p1 - l1) ** 2 + (u2 - p2) ** 2)
    hi = d + math.sqrt((u1 - p1) ** 2 + (p2 - l2) ** 2)
    return (max(-1.0, lo), min(1.0, hi))


def choose_threshold(y_val: np.ndarray, p_val: np.ndarray) -> float:
    """Threshold maximizing F1 on the validation split only (never the test split)."""
    precision, recall, thresholds = precision_recall_curve(y_val, p_val)
    f1 = 2 * precision * recall / np.maximum(precision + recall, 1e-12)
    return float(thresholds[int(np.argmax(f1[:-1]))])


def expected_calibration_error(y: np.ndarray, p: np.ndarray, bins: int = 10) -> float:
    edges = np.linspace(0.0, 1.0, bins + 1)
    idx = np.clip(np.digitize(p, edges[1:-1]), 0, bins - 1)
    ece = 0.0
    for b in range(bins):
        m = idx == b
        if m.any():
            ece += m.mean() * abs(p[m].mean() - y[m].mean())
    return float(ece)


def threshold_metrics(y: np.ndarray, decision: np.ndarray) -> dict[str, Any]:
    tp = int(((decision == 1) & (y == 1)).sum())
    fp = int(((decision == 1) & (y == 0)).sum())
    fn = int(((decision == 0) & (y == 1)).sum())
    tn = int(((decision == 0) & (y == 0)).sum())
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "tn": tn,
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "f1": round(f1, 4),
        "precision_ci95": [round(v, 4) for v in wilson_interval(tp, tp + fp)],
        "recall_ci95": [round(v, 4) for v in wilson_interval(tp, tp + fn)],
        "flag_rate": round(float(decision.mean()) if len(decision) else 0.0, 4),
    }


def bootstrap_ci(y: np.ndarray, p: np.ndarray, fn: Any, n_boot: int = 200, seed: int = 0) -> list[float]:
    rng = np.random.default_rng(seed)
    vals = []
    n = len(y)
    for _ in range(n_boot):
        i = rng.integers(0, n, n)
        if y[i].sum() == 0:
            continue
        vals.append(fn(y[i], p[i]))
    return [round(float(np.quantile(vals, 0.025)), 4), round(float(np.quantile(vals, 0.975)), 4)]


def full_report(
    y: np.ndarray, p: np.ndarray, threshold: float, X: np.ndarray | None = None, seed: int = 0
) -> dict[str, Any]:
    decision = (p >= threshold).astype(int)
    rep: dict[str, Any] = {
        "n": len(y),
        "positives": int(y.sum()),
        "positive_rate": round(float(y.mean()), 4),
        "pr_auc": round(float(average_precision_score(y, p)), 4),
        "pr_auc_ci95": bootstrap_ci(y, p, average_precision_score, seed=seed),
        "roc_auc": round(float(roc_auc_score(y, p)), 4),
        "brier": round(float(brier_score_loss(y, p)), 5),
        "ece": round(expected_calibration_error(y, p), 4),
        "threshold": round(threshold, 6),
        **threshold_metrics(y, decision),
    }
    if X is not None:
        from .schema import FEATURE_NAMES, MERCHANT_CATEGORIES

        slices: dict[str, Any] = {}
        cp = X[:, FEATURE_NAMES.index("card_present")]
        for name, mask in (("card_present", cp == 1), ("card_not_present", cp == 0)):
            slices[name] = _slice(y, p, decision, mask)
        cat = X[:, FEATURE_NAMES.index("merchant_category")].astype(int)
        for i, c in enumerate(MERCHANT_CATEGORIES):
            slices[f"merchant={c}"] = _slice(y, p, decision, cat == i)
        rep["slices"] = slices
    return rep


def _slice(y: np.ndarray, p: np.ndarray, decision: np.ndarray, mask: np.ndarray) -> dict[str, Any]:
    ys, ps, ds = y[mask], p[mask], decision[mask]
    out: dict[str, Any] = {"n": int(mask.sum()), "positives": int(ys.sum())}
    out.update(threshold_metrics(ys, ds))
    out["pr_auc"] = round(float(average_precision_score(ys, ps)), 4) if 0 < ys.sum() < len(ys) else None
    return out


def psi(expected: np.ndarray, actual: np.ndarray, bins: int = 10) -> float:
    """Population Stability Index with quantile bins from the reference sample.

    Rule of thumb used in docs/DRIFT_AND_LIMITATIONS.md: <0.1 stable, 0.1–0.25 moderate, >0.25 major.
    """
    if len(actual) == 0 or len(expected) == 0:
        return 0.0
    qs = np.unique(np.quantile(expected, np.linspace(0, 1, bins + 1)[1:-1]))
    e = np.bincount(np.searchsorted(qs, expected, side="right"), minlength=len(qs) + 1) / len(expected)
    a = np.bincount(np.searchsorted(qs, actual, side="right"), minlength=len(qs) + 1) / len(actual)
    e = np.clip(e, 1e-4, None)
    a = np.clip(a, 1e-4, None)
    return float(np.sum((a - e) * np.log(a / e)))


def categorical_psi(expected: np.ndarray, actual: np.ndarray, k: int) -> float:
    e = np.clip(np.bincount(expected.astype(int), minlength=k) / max(len(expected), 1), 1e-4, None)
    a = np.clip(np.bincount(actual.astype(int), minlength=k) / max(len(actual), 1), 1e-4, None)
    return float(np.sum((a - e) * np.log(a / e)))


def js_divergence(p_scores: np.ndarray, q_scores: np.ndarray, bins: int = 20) -> float:
    """Jensen–Shannon divergence (base 2, in [0, 1]) between two score histograms on [0, 1]."""
    if len(p_scores) == 0 or len(q_scores) == 0:
        return 0.0
    edges = np.linspace(0, 1, bins + 1)
    p = np.histogram(p_scores, edges)[0].astype(float) + 1e-9
    q = np.histogram(q_scores, edges)[0].astype(float) + 1e-9
    p /= p.sum()
    q /= q.sum()
    m = 0.5 * (p + q)
    return float(0.5 * np.sum(p * np.log2(p / m)) + 0.5 * np.sum(q * np.log2(q / m)))


def percentiles(values: list[float] | np.ndarray) -> dict[str, float | None]:
    if len(values) == 0:
        return {"p50": None, "p95": None, "p99": None, "mean": None, "n": 0}
    a = np.asarray(values, dtype=float)
    return {
        "p50": round(float(np.percentile(a, 50)), 3),
        "p95": round(float(np.percentile(a, 95)), 3),
        "p99": round(float(np.percentile(a, 99)), 3),
        "mean": round(float(a.mean()), 3),
        "n": len(a),
    }


def mcnemar_exact_p(b: int, c: int) -> float:
    """One-sided exact McNemar (sign) test p-value for H1: b > c.

    b = discordant pairs favouring the reference (champion caught, candidate missed),
    c = discordant pairs favouring the candidate.
    """
    n = b + c
    if n == 0:
        return 1.0
    return float(sum(math.comb(n, k) for k in range(b, n + 1)) / 2**n)
