"""Dataset determinism, no leakage, schema validation, artifact integrity and reproducible training."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from spectral_canary import artifacts as art
from spectral_canary.data import DatasetConfig, build_dataset, generate, load_split_csv, split
from spectral_canary.paths import MANIFEST_DIR, state_paths
from spectral_canary.schema import FEATURE_NAMES, decode_row, encode_records, validate_record


def test_generator_is_seed_stable() -> None:
    a = generate(500, 42)
    b = generate(500, 42)
    c = generate(500, 43)
    assert np.array_equal(a[0], b[0]) and np.array_equal(a[1], b[1])
    assert not np.array_equal(a[0], c[0])


def test_splits_are_disjoint_and_cover_all_rows() -> None:
    cfg = DatasetConfig(n_rows=2000)
    X, y = generate(cfg.n_rows, cfg.seed)
    s = split(X, y, cfg)
    ids = [set(s.row_ids_train), set(s.row_ids_val), set(s.row_ids_test)]
    assert not (ids[0] & ids[1]) and not (ids[0] & ids[2]) and not (ids[1] & ids[2])
    assert len(ids[0] | ids[1] | ids[2]) == cfg.n_rows


def test_no_duplicate_feature_rows_across_train_and_test(scw_home: Path) -> None:
    p = state_paths(scw_home)
    Xtr, _, _ = load_split_csv(p.data / "train.csv")
    Xte, _, _ = load_split_csv(p.data / "test.csv")
    tr = {r.tobytes() for r in np.round(Xtr, 4)}
    dup = sum(r.tobytes() in tr for r in np.round(Xte, 4))
    assert dup == 0, f"{dup} test rows also appear in train"


def test_dataset_hash_matches_committed_manifest(tmp_path: Path) -> None:
    m = build_dataset(tmp_path)
    committed = json.loads((MANIFEST_DIR / "dataset_manifest.json").read_text())
    assert m["dataset_hash"] == committed["dataset_hash"]
    assert m["files"]["test"]["sha256"] == committed["files"]["test"]["sha256"]


def test_csv_roundtrip_matches_encoding(scw_home: Path) -> None:
    X, _, _ = load_split_csv(state_paths(scw_home).data / "val.csv")
    again = encode_records([decode_row(r) for r in X[:200]])
    assert np.allclose(X[:200], again)


@pytest.mark.parametrize(
    "bad,needle",
    [
        ({"amount": 1.0}, "missing field"),
        ({"merchant_category": "crypto_atm"}, "unknown category"),
        ({"amount": "12"}, "expected number"),
        ({"card_present": 1}, "expected bool"),
        ({"hour_of_day": 24}, "above maximum"),
        ({"hour_of_day": 2.5}, "expected integer"),
        ({"surprise": 1}, "unexpected fields"),
    ],
)
def test_schema_rejects(bad: dict, needle: str) -> None:
    good = {
        "amount": 23.5,
        "merchant_category": "grocery",
        "hour_of_day": 11,
        "account_age_days": 3400,
        "txn_count_24h": 1,
        "distance_from_home_km": 2.1,
        "device_trust_score": 0.93,
        "card_present": True,
        "country_mismatch": False,
    }
    assert validate_record(good) == []
    rec = dict(bad) if needle == "missing field" else {**good, **bad}
    problems = validate_record(rec)
    assert any(needle in p for p in problems), problems


def test_manifests_record_provenance_and_threshold_from_val(scw_home: Path) -> None:
    for d in state_paths(scw_home).artifacts.iterdir():
        m = json.loads((d / "manifest.json").read_text())
        assert m["contract"] == "model_artifact_v1"
        assert m["threshold"]["selected_on"] == "val"
        assert m["artifact"]["untrusted_types_outside_allowlist"] == []
        assert len(m["training"]["config_sha256"]) == 64 and len(m["dataset"]["dataset_hash"]) == 64
        assert set(FEATURE_NAMES) == {f["name"] for f in m["input_schema"]["features"]}


def test_training_is_reproducible(scw_home: Path, tmp_path: Path) -> None:
    """Retraining v1 from the same config/data/seed reproduces the committed prediction hash."""
    from spectral_canary.paths import CONFIG_DIR
    from spectral_canary.train import load_config, train_one

    p = state_paths(tmp_path).ensure()
    import shutil

    shutil.copytree(state_paths(scw_home).data, p.data, dirs_exist_ok=True)
    ds = json.loads((p.data / "dataset_manifest.json").read_text())
    m = train_one(load_config(CONFIG_DIR / "models" / "v1-logreg.toml"), p, ds)
    committed = json.loads((MANIFEST_DIR / "models" / "v1-logreg.json").read_text())
    assert m["test_prediction_sha256"] == committed["test_prediction_sha256"]


def test_tampered_artifact_cannot_load(scw_home: Path, tmp_path: Path) -> None:
    src = state_paths(scw_home).artifacts / "v1-logreg" / "model.skops"
    m = json.loads(src.with_name("manifest.json").read_text())
    f = tmp_path / "model.skops"
    data = bytearray(src.read_bytes())
    data[len(data) // 3] ^= 0x01
    f.write_bytes(bytes(data))
    with pytest.raises(art.ArtifactError, match="artifact_hash_mismatch"):
        art.load_verified(f, m["artifact"]["sha256"])
    # and the untouched copy loads fine
    assert art.load_verified(src, m["artifact"]["sha256"]) is not None


def test_manifest_signature_detects_edits(scw_home: Path) -> None:
    key = art.signing_key(state_paths(scw_home))
    m = {"version": "x", "metrics": {"pr_auc": 0.5}}
    sig = art.sign_manifest(m, key)
    assert art.verify_signature(m, sig, key)
    m["metrics"]["pr_auc"] = 0.9
    assert not art.verify_signature(m, sig, key)
