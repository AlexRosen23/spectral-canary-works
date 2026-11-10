"""Single-row scorer for the calibrated HistGradientBoosting models, bit-identical to sklearn.

Why: one gateway request scores one row. For a ``CalibratedClassifierCV`` of three HGB fits, sklearn's
``predict_proba`` on one row runs input validation, the HGB-internal ``ColumnTransformer`` (with its joblib
dispatch), ``make_known_categories_bitsets`` and an OpenMP thread query three times, then 40-60 Cython tree
calls per fit. That wrapper work is ~95% of the ~2.5-4.5 ms per request; the trees themselves are tiny. (v1, a logistic
regression, pays ~1 ms for the same reason: its own ``ColumnTransformer``.)

What this does: at model load it reads the fitted trees out of the verified estimator (no retraining, no new
artifact) and scores a row with the same arithmetic sklearn performs, in the same order:

1. HGB preprocessor: the categorical column goes through the fitted ``OrdinalEncoder`` mapping (unknown ->
   NaN), the numeric columns pass through as float64, in the preprocessor's output column order.
2. Each tree is walked with the rules of ``_predict_one_from_raw_data`` (sklearn
   ``ensemble/_hist_gradient_boosting/_predictor.pyx``): NaN -> missing branch; categorical -> left if the
   category is in the node's left bitset, right if it is a known category, else the missing branch; numeric ->
   left iff ``value <= threshold``. Categorical decisions are precomputed per category code from those same
   bitsets.
3. Raw score = ``0.0 + baseline``, then ``+= leaf value`` tree by tree in fit order (float64, sequential, as
   ``_raw_predict`` accumulates into its ``raw_predictions`` array).
4. Isotonic calibration calls the fitted calibrator's own ``np.clip`` + ``f_`` (scipy ``interp1d``), so the
   interpolation formula is the library's, not a copy. Values in (1, 1 + 1e-5] snap to 1.0 as sklearn does.
5. Fold mean: ``0.0 + p_1 + p_2 + p_3`` in fold order, then ``/ 3``, as ``CalibratedClassifierCV`` does.

v1 (logistic regression) gets the same treatment: see ``_compile_logreg``.

Safety: ``build`` returns ``None`` (the caller keeps plain sklearn) for any estimator whose structure is not
exactly the one above, and it refuses a compiled scorer that does not reproduce sklearn's single-row
``predict_proba`` bit for bit on a deterministic probe set. ``tests/test_fastscore.py`` additionally checks
every row of the frozen test split. ``SCW_FAST_SCORING=0`` turns the fast path off.
"""

from __future__ import annotations

import math
import os
from collections.abc import Callable, Sequence
from typing import Any

import numpy as np

from .schema import FEATURES

# Node kinds in the flattened trees.
_LEAF, _NUM, _CAT = 0, 1, 2

Scorer = Callable[[Sequence[float]], float]


class Unsupported(ValueError):
    """The estimator is not one this module reproduces exactly; serve it through sklearn."""


def enabled() -> bool:
    return os.environ.get("SCW_FAST_SCORING", "1").strip().lower() not in ("0", "false", "no", "off")


def _bit(bitsets: np.ndarray, row: int, value: int) -> int:
    # sklearn/utils/_bitset.pyx in_bitset_2d_memoryview: (bitset[row, val // 32] >> (val % 32)) & 1
    return (int(bitsets[row, value // 32]) >> (value % 32)) & 1


def _compile_hgb(hgb: Any) -> tuple[list[int], list[dict[float, float]], float, list[list[tuple]]]:
    if type(hgb).__name__ != "HistGradientBoostingClassifier":
        raise Unsupported(f"base estimator {type(hgb).__name__}")
    if hgb.n_trees_per_iteration_ != 1 or list(hgb.classes_) != [0, 1]:
        raise Unsupported("not a binary HGB")
    pre = hgb._preprocessor
    if pre is None:
        raise Unsupported("HGB without categorical preprocessor")
    if getattr(pre, "sparse_output_", False):
        raise Unsupported("sparse preprocessor output")
    parts = [t for t in pre.transformers_ if not (t[0] == "remainder" and not np.any(t[2]))]
    if [t[0] for t in parts] != ["encoder", "numerical"]:
        raise Unsupported(f"preprocessor steps {[t[0] for t in parts]}")
    (_, enc, enc_cols), (_, num, num_cols) = parts
    if type(enc).__name__ != "OrdinalEncoder" or type(num).__name__ != "FunctionTransformer":
        raise Unsupported("unexpected preprocessor transformers")
    if enc.handle_unknown != "use_encoded_value" or not (
        isinstance(enc.unknown_value, float) and math.isnan(enc.unknown_value)
    ):
        raise Unsupported("encoder unknown handling")
    enc_idx = [int(i) for i in np.flatnonzero(np.asarray(enc_cols))]
    num_idx = [int(i) for i in np.flatnonzero(np.asarray(num_cols))]
    sl = pre.output_indices_
    if (sl["encoder"].start, sl["encoder"].stop, sl["numerical"].start, sl["numerical"].stop) != (
        0,
        len(enc_idx),
        len(enc_idx),
        len(enc_idx) + len(num_idx),
    ):
        raise Unsupported("preprocessor output layout")
    order = enc_idx + num_idx  # transformed column j is raw column order[j]
    if len(order) != hgb.n_features_in_ or sorted(order) != list(range(hgb.n_features_in_)):
        raise Unsupported("preprocessor does not cover every feature exactly once")
    cat_maps = [{float(v): float(code) for code, v in enumerate(cats)} for cats in enc.categories_]
    if any(len(cats) > 256 for cats in enc.categories_):
        raise Unsupported("more than 256 categories")
    n_codes = [len(cats) for cats in enc.categories_]

    is_cat = np.asarray(hgb._is_categorical_remapped, dtype=bool)
    known_bitsets, f_idx_map = hgb._bin_mapper.make_known_categories_bitsets()
    baseline = hgb._baseline_prediction
    if baseline.shape != (1, 1) or baseline.dtype != np.float64:
        raise Unsupported("baseline shape/dtype")

    trees: list[list[tuple]] = []
    for iteration in hgb._predictors:
        if len(iteration) != 1:
            raise Unsupported("multiple trees per iteration")
        pred = iteration[0]
        nodes = pred.nodes
        if nodes["value"].dtype != np.float64 or nodes["num_threshold"].dtype != np.float64:
            raise Unsupported("tree dtypes")
        flat: list[tuple] = []
        for i in range(len(nodes)):
            nd = nodes[i]
            if nd["is_leaf"]:
                flat.append((_LEAF, float(nd["value"])))
                continue
            f = int(nd["feature_idx"])
            left, right = int(nd["left"]), int(nd["right"])
            missing = left if nd["missing_go_to_left"] else right
            if nd["is_categorical"]:
                if not (f < len(enc_idx) and is_cat[f]):
                    raise Unsupported("categorical split on a non-encoded column")
                bidx, kidx = int(nd["bitset_idx"]), int(f_idx_map[f])
                table = tuple(
                    left
                    if _bit(pred.raw_left_cat_bitsets, bidx, c)
                    else (right if _bit(known_bitsets, kidx, c) else missing)
                    for c in range(n_codes[f])
                )
                flat.append((_CAT, f, table, missing))
            else:
                if f < len(enc_idx) or is_cat[f]:
                    raise Unsupported("numeric split on a categorical column")
                flat.append((_NUM, f, float(nd["num_threshold"]), left, right, missing))
        trees.append(flat)
    return order, cat_maps, float(baseline[0, 0]), trees


def _compile_logreg(est: Any) -> Scorer:
    """v1: Pipeline(ColumnTransformer[one-hot, log1p+scale, scale]) -> LogisticRegression.

    The per-column work is the same numpy operations, on arrays of the same (1, k) shapes, that sklearn's
    transformers run on one row; the dot product is the same ``(1, 16) @ coef_.T`` call (so the same BLAS
    path) and the link is ``scipy.special.expit(out=...)`` as in ``_predict_proba_lr``. Only the wrappers
    (validation, ``ColumnTransformer`` dispatch) are skipped.
    """
    steps = [name for name, _ in est.steps]
    if steps != ["features", "model"]:
        raise Unsupported(f"pipeline steps {steps}")
    ct, lr = est.named_steps["features"], est.named_steps["model"]
    if type(ct).__name__ != "ColumnTransformer" or type(lr).__name__ != "LogisticRegression":
        raise Unsupported("pipeline step types")
    if ct.sparse_output_ or list(lr.classes_) != [0, 1] or lr.coef_.shape[0] != 1:
        raise Unsupported("sparse features or non-binary model")
    parts = [t for t in ct.transformers_ if not (t[0] == "remainder" and not np.any(t[2]))]
    if [t[0] for t in parts] != ["cat", "log", "num"]:
        raise Unsupported(f"feature steps {[t[0] for t in parts]}")
    (_, oh, cat_cols), (_, logpipe, log_cols), (_, numsc, num_cols) = parts
    if type(oh).__name__ != "OneHotEncoder" or len(oh.categories_) != 1 or len(list(cat_cols)) != 1:
        raise Unsupported("one-hot step")
    if oh.drop_idx_ is not None or oh.handle_unknown != "error" or oh.dtype is not np.float64:
        raise Unsupported("one-hot options")
    if [n for n, _ in logpipe.steps] != ["log1p", "scale"]:
        raise Unsupported("log step")
    ft, logsc = logpipe.named_steps["log1p"], logpipe.named_steps["scale"]
    if type(ft).__name__ != "FunctionTransformer" or ft.func is not np.log1p or ft.kw_args or ft.validate:
        raise Unsupported("log1p transformer")
    for sc in (logsc, numsc):
        if type(sc).__name__ != "StandardScaler" or not (sc.with_mean and sc.with_std):
            raise Unsupported("scaler options")
    n_cat = len(oh.categories_[0])
    widths = [("cat", n_cat), ("log", len(list(log_cols))), ("num", len(list(num_cols)))]
    start = 0
    for name, w in widths:
        if (ct.output_indices_[name].start, ct.output_indices_[name].stop) != (start, start + w):
            raise Unsupported("feature output layout")
        start += w
    if lr.coef_.shape[1] != start:
        raise Unsupported("coefficient width")

    from scipy.special import expit

    cat_col = int(next(iter(cat_cols)))
    codes = {float(v): i for i, v in enumerate(oh.categories_[0])}
    log_idx = [int(c) for c in log_cols]
    num_idx = [int(c) for c in num_cols]
    log_mean, log_scale = logsc.mean_.astype(np.float64), logsc.scale_.astype(np.float64)
    num_mean, num_scale = numsc.mean_.astype(np.float64), numsc.scale_.astype(np.float64)
    coef_t, intercept = lr.coef_.T, lr.intercept_

    def score(row: Sequence[float]) -> float:
        code = codes.get(float(row[cat_col]))
        if code is None:  # OneHotEncoder(handle_unknown="error") raises too; the schema rejects it earlier
            raise ValueError(f"unknown category {row[cat_col]!r}")
        oh_part = np.zeros((1, n_cat), dtype=np.float64)
        oh_part[0, code] = 1.0
        log_part = np.log1p(np.array([[row[i] for i in log_idx]], dtype=np.float64))
        log_part -= log_mean
        log_part /= log_scale
        num_part = np.array([[row[i] for i in num_idx]], dtype=np.float64)
        num_part -= num_mean
        num_part /= num_scale
        dec = (np.hstack([oh_part, log_part, num_part]) @ coef_t + intercept).reshape(-1)
        expit(dec, out=dec)
        return float(dec[0])

    return score


def _compile(est: Any) -> Scorer:
    if type(est).__name__ == "Pipeline":
        return _compile_logreg(est)
    if type(est).__name__ != "CalibratedClassifierCV":
        raise Unsupported(f"top-level {type(est).__name__}")
    if list(est.classes_) != [0, 1]:
        raise Unsupported("not binary")
    folds = []
    for cc in est.calibrated_classifiers_:
        if cc.method != "isotonic" or len(cc.calibrators) != 1 or list(cc.classes) != [0, 1]:
            raise Unsupported(f"calibration {cc.method!r} with {len(cc.calibrators)} calibrators")
        cal = cc.calibrators[0]
        if type(cal).__name__ != "IsotonicRegression" or cal.out_of_bounds != "clip":
            raise Unsupported("calibrator is not a clipping IsotonicRegression")
        if cal.X_thresholds_.dtype != np.float64:
            raise Unsupported("calibrator dtype")
        folds.append((*_compile_hgb(cc.estimator), cal))
    n_folds = len(folds)
    nan = math.nan

    def score(row: Sequence[float]) -> float:
        total = 0.0
        for order, cat_maps, baseline, trees, cal in folds:
            x = [float(row[c]) for c in order]
            for j, mapping in enumerate(cat_maps):
                x[j] = mapping.get(x[j], nan)
            raw = 0.0 + baseline
            for nodes in trees:
                n = nodes[0]
                while True:
                    kind = n[0]
                    if kind == _NUM:
                        d = x[n[1]]
                        if d <= n[2]:
                            n = nodes[n[3]]
                        elif d != d:  # NaN
                            n = nodes[n[5]]
                        else:
                            n = nodes[n[4]]
                    elif kind == _LEAF:
                        raw += n[1]
                        break
                    else:
                        d = x[n[1]]
                        n = nodes[n[3]] if d != d else nodes[n[2][int(d)]]
            p = float(cal.f_(np.clip(np.array([raw]), cal.X_min_, cal.X_max_))[0])
            if 1.0 < p <= 1.0 + 1e-5:
                p = 1.0
            total += p
        return total / n_folds

    return score


def probe_rows(n: int = 32, seed: int = 20261109) -> np.ndarray:
    """Deterministic rows spanning the schema (every category, range edges, typical values)."""
    rng = np.random.default_rng(seed)
    X = np.empty((n, len(FEATURES)), dtype=np.float64)
    for j, f in enumerate(FEATURES):
        if f.kind == "category":
            X[:, j] = np.arange(n) % len(f.categories)
        elif f.kind == "bool":
            X[:, j] = rng.integers(0, 2, n)
        else:
            lo, hi = float(f.minimum or 0.0), float(f.maximum or 1.0)
            # half log-uniform (realistic small values), half uniform (the long tail), plus both edges
            u = rng.random(n)
            v = np.where(np.arange(n) % 2 == 0, np.expm1(u * np.log1p(hi - lo)) + lo, lo + u * (hi - lo)).clip(lo, hi)
            v[0], v[1] = lo, hi
            X[:, j] = np.round(v) if f.kind == "int" else v
    return X


def build(est: Any, probes: np.ndarray | None = None) -> tuple[Scorer | None, str]:
    """Return ``(scorer, "compiled")`` or ``(None, reason)``; never a scorer that disagrees with sklearn.

    The reference is sklearn's single-row ``predict_proba``, i.e. exactly the call the gateway made before.
    (For the HGB models it equals the batch call too; for v1 it can differ from the batch call in the last bit,
    because BLAS sums a (1, 16) and an (n, 16) product in different orders. That is a property of sklearn.)
    """
    if not enabled():
        return None, "disabled (SCW_FAST_SCORING=0)"
    try:
        scorer = _compile(est)
    except Unsupported as exc:
        return None, f"unsupported: {exc}"
    except Exception as exc:  # an sklearn internals change: fail safe to the library path
        return None, f"compile_failed: {type(exc).__name__}: {exc}"
    X = probe_rows() if probes is None else probes
    ref = np.array([est.predict_proba(X[i : i + 1])[0, 1] for i in range(len(X))], dtype=np.float64)
    got = np.array([scorer(r) for r in X.tolist()], dtype=np.float64)
    bad = np.flatnonzero(ref.view(np.uint64) != got.view(np.uint64))
    if bad.size:
        return None, f"self_check_failed: {bad.size}/{len(X)} probe rows differ from predict_proba"
    return scorer, "compiled"
