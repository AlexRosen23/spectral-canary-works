"""The compiled single-row scorer must be bit-identical to sklearn, or not be used at all."""

from __future__ import annotations

import json

import numpy as np
import pytest

from spectral_canary import fastscore
from spectral_canary.data import load_split_csv
from spectral_canary.paths import MANIFEST_DIR
from spectral_canary.schema import FEATURE_NAMES, MERCHANT_CATEGORIES
from spectral_canary.train import prediction_hash

HGB_VERSIONS = ("v2-hgb", "v3-hgb-fresh", "v4-hgb-segfilter")


def _bits(a: np.ndarray) -> np.ndarray:
    return np.ascontiguousarray(a, dtype=np.float64).view(np.uint64)


@pytest.mark.parametrize("version", HGB_VERSIONS)
def test_serving_path_is_bit_identical_on_frozen_test_split(rt, version: str) -> None:
    """Serve every row of the frozen test split one at a time through the gateway's model pool and compare
    with plain sklearn predict_proba on the whole split (for these models it equals the single-row call)."""
    m = rt.service.pool.get(version)
    assert m.scoring_path == "compiled" and m.fast is not None
    X_te, _, _ = load_split_csv(rt.paths.data / "test.csv")

    reference = m.estimator.predict_proba(X_te)[:, 1]  # plain sklearn, whole split at once
    served = np.array([m.score(X_te[i : i + 1])[0] for i in range(len(X_te))])

    mismatched = np.flatnonzero(_bits(served) != _bits(reference))
    assert mismatched.size == 0, f"{mismatched.size} rows differ, first {mismatched[:5].tolist()}"
    # sklearn's own single-row call agrees too (what the gateway ran before this change)
    for i in range(0, len(X_te), 97):
        assert _bits(m.estimator.predict_proba(X_te[i : i + 1])[:, 1])[0] == _bits(served[i : i + 1])[0]
    committed = json.loads((MANIFEST_DIR / "models" / f"{version}.json").read_text())
    assert prediction_hash(served) == committed["test_prediction_sha256"]


def test_gateway_response_matches_sklearn(scw_home, tmp_path) -> None:
    """End to end through InferenceService.predict with v2-hgb as champion: the served score is sklearn's."""
    from spectral_canary.runtime import build_runtime, register_all

    rt = build_runtime(scw_home, db_path=tmp_path / "registry.sqlite", report_dir=tmp_path / "reports")
    try:
        register_all(rt)
        rt.controller.bootstrap_champion("v2-hgb", actor="test")
        _assert_gateway_scores(rt)
    finally:
        rt.close()


def _assert_gateway_scores(rt) -> None:
    X_te, _, _ = load_split_csv(rt.paths.data / "test.csv")
    m = rt.service.pool.get(rt.store.deployment().champion_version)
    assert m.version == "v2-hgb" and m.scoring_path == "compiled"
    for i in (0, 1, 2, 500, 7999):
        row = X_te[i]
        features = {}
        for j, name in enumerate(FEATURE_NAMES):
            v = float(row[j])
            if name == "merchant_category":
                features[name] = MERCHANT_CATEGORIES[int(v)]
            elif name in ("card_present", "country_mismatch"):
                features[name] = bool(v)
            elif name in ("hour_of_day", "account_age_days", "txn_count_24h"):
                features[name] = int(v)
            else:
                features[name] = v
        out = rt.service.predict({"features": features, "request_id": f"fastscore-{i}"})
        assert out.http_status == 200
        assert out.body["model_version"] == "v2-hgb"
        assert out.body["score"] == round(float(m.estimator.predict_proba(row[None, :])[0, 1]), 6)


def test_logreg_serving_path_is_bit_identical_to_sklearn_single_row(rt) -> None:
    """v1: every test row matches sklearn's single-row predict_proba (the call the gateway made before).

    sklearn's own batch call can differ from its single-row call in the last bit for a linear model (BLAS sums
    (8000, 16) @ (16, 1) and (1, 16) @ (16, 1) in different orders), so the batch output is compared only through
    the committed 6-decimal prediction hash, which is what the reproducibility gate checks.
    """
    m = rt.service.pool.get("v1-logreg")
    assert m.scoring_path == "compiled" and m.fast is not None
    assert rt.controller.health()["scoring"]["v1-logreg"] == "compiled"
    X_te, _, _ = load_split_csv(rt.paths.data / "test.csv")
    served = np.array([m.score(X_te[i : i + 1])[0] for i in range(len(X_te))])
    single = np.array([m.estimator.predict_proba(X_te[i : i + 1])[0, 1] for i in range(len(X_te))])
    mismatched = np.flatnonzero(_bits(served) != _bits(single))
    assert mismatched.size == 0, f"{mismatched.size} rows differ, first {mismatched[:5].tolist()}"
    committed = json.loads((MANIFEST_DIR / "models" / "v1-logreg.json").read_text())
    assert prediction_hash(served) == committed["test_prediction_sha256"]


def test_unsupported_estimator_keeps_the_sklearn_path() -> None:
    from sklearn.dummy import DummyClassifier

    est = DummyClassifier(strategy="prior").fit(np.zeros((4, 9)), [0, 1, 0, 1])
    scorer, why = fastscore.build(est)
    assert scorer is None and why.startswith("unsupported")


@pytest.mark.parametrize("version", ("v1-logreg", "v2-hgb"))
def test_scorer_that_differs_by_one_ulp_is_refused(rt, monkeypatch, version: str) -> None:
    est = rt.service.pool.get(version).estimator
    real = fastscore._compile

    def off_by_one_ulp(e):
        s = real(e)
        return lambda row: float(np.nextafter(s(row), 2.0))

    monkeypatch.setattr(fastscore, "_compile", off_by_one_ulp)
    scorer, why = fastscore.build(est)
    assert scorer is None and why.startswith("self_check_failed")


def test_kill_switch(rt, monkeypatch) -> None:
    monkeypatch.setenv("SCW_FAST_SCORING", "0")
    scorer, why = fastscore.build(rt.service.pool.get("v2-hgb").estimator)
    assert scorer is None and why.startswith("disabled")


def test_probe_rows_cover_the_schema() -> None:
    X = fastscore.probe_rows()
    j = FEATURE_NAMES.index("merchant_category")
    assert set(X[:, j].astype(int)) == set(range(len(MERCHANT_CATEGORIES)))
    assert np.isfinite(X).all()
