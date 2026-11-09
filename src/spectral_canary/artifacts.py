"""Artifact persistence, integrity and safe loading.

- Format: ``skops`` (no arbitrary pickle execution). Loading refuses any type outside ``TRUSTED_TYPES``.
- Integrity: SHA-256 of the artifact bytes is stored in the manifest and the immutable registry row and is
  re-checked on every load. A file whose bytes changed can never be loaded, so it can never serve.
- Authenticity: the manifest is signed with HMAC-SHA256 using a local key (``var/secrets``). This proves the
  manifest was written by this installation, not by a third party; it is not PKI code signing.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import tempfile
from pathlib import Path
from typing import Any

import skops.io as sio

from .paths import StatePaths

# Every type our two model families need. Anything else in an artifact is rejected before load.
TRUSTED_TYPES: frozenset[str] = frozenset(
    {
        "sklearn.pipeline.Pipeline",
        "sklearn.compose._column_transformer.ColumnTransformer",
        "sklearn.compose._column_transformer._RemainderColsList",
        "sklearn.preprocessing._encoders.OneHotEncoder",
        "sklearn.preprocessing._data.StandardScaler",
        "sklearn.preprocessing._function_transformer.FunctionTransformer",
        "sklearn.linear_model._logistic.LogisticRegression",
        "sklearn.calibration.CalibratedClassifierCV",
        "sklearn.calibration._CalibratedClassifier",
        "sklearn.isotonic.IsotonicRegression",
        "sklearn.ensemble._hist_gradient_boosting.gradient_boosting.HistGradientBoostingClassifier",
        "sklearn.ensemble._hist_gradient_boosting.binning._BinMapper",
        "sklearn.ensemble._hist_gradient_boosting.predictor.TreePredictor",
        "sklearn._loss.loss.HalfBinomialLoss",
        "sklearn._loss.link.LogitLink",
        "sklearn._loss.link.Interval",
        "sklearn._loss._loss.CyHalfBinomialLoss",
        "sklearn.utils._bunch.Bunch",
        "numpy.log1p",
        "numpy.dtype",
        "numpy.float64",
        "numpy.int64",
        "builtins.slice",
        "collections.OrderedDict",
        # HistGradientBoosting stores its input validator as functools.partial(check_array, ...). skops still
        # resolves and vets the wrapped callable, so trusting `partial` here only admits this sklearn function.
        "functools.partial",
        "sklearn.utils.validation.check_array",
    }
)


class ArtifactError(RuntimeError):
    """Raised when an artifact fails integrity, signature or type checks."""


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def canonical_json(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str).encode()


def signing_key(paths: StatePaths) -> bytes:
    p = paths.signing_key
    if not p.exists():
        p.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(secrets.token_hex(32))
    return bytes.fromhex(p.read_text().strip())


def sign_manifest(manifest: dict[str, Any], key: bytes) -> str:
    return hmac.new(key, canonical_json(manifest), hashlib.sha256).hexdigest()


def verify_signature(manifest: dict[str, Any], signature: str, key: bytes) -> bool:
    return hmac.compare_digest(sign_manifest(manifest, key), signature)


def save_model(model: Any, dest_dir: Path) -> tuple[Path, str, int]:
    """Serialize atomically (temp file + rename), make read-only, return (path, sha256, bytes)."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    final = dest_dir / "model.skops"
    if final.exists():
        raise ArtifactError(f"{final} already exists; artifacts are immutable")
    with tempfile.NamedTemporaryFile(dir=dest_dir, suffix=".tmp", delete=False) as tmp:
        tmp_path = Path(tmp.name)
    try:
        sio.dump(model, tmp_path)
        os.replace(tmp_path, final)
    finally:
        tmp_path.unlink(missing_ok=True)
    final.chmod(0o444)
    return final, sha256_file(final), final.stat().st_size


def untrusted_types(path: Path) -> list[str]:
    return [t for t in sio.get_untrusted_types(file=path) if t not in TRUSTED_TYPES]


def load_verified(path: Path, expected_sha256: str) -> Any:
    """Load only if bytes match the registered hash and every type is on the allowlist."""
    path = Path(path)
    if not path.exists():
        raise ArtifactError(f"artifact missing: {path}")
    actual = sha256_file(path)
    if actual != expected_sha256:
        raise ArtifactError(f"artifact_hash_mismatch: expected {expected_sha256[:12]}…, got {actual[:12]}…")
    try:
        bad = untrusted_types(path)
    except Exception as exc:  # corrupted zip, truncated file, …
        raise ArtifactError(f"artifact_unreadable: {exc}") from exc
    if bad:
        raise ArtifactError(f"artifact_untrusted_types: {bad}")
    try:
        trusted = list(sio.get_untrusted_types(file=path))
        return sio.load(path, trusted=trusted)
    except Exception as exc:
        raise ArtifactError(f"artifact_load_failed: {exc}") from exc
