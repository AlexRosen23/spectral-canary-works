"""Model registration: validate a trained artifact directory and record it immutably."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from . import artifacts as art
from .paths import StatePaths
from .schema import schema_hash
from .store import Store

REQUIRED_MANIFEST_KEYS = (
    "contract",
    "model_name",
    "version",
    "artifact",
    "input_schema",
    "output_schema",
    "threshold",
    "training",
    "dataset",
    "metrics",
)


class RegistrationError(RuntimeError):
    pass


def register_from_dir(store: Store, paths: StatePaths, version_dir: Path, actor: str) -> dict[str, Any]:
    """Validate an artifact directory (manifest + model.skops) and register it.

    Checks: manifest contract keys, schema hash compatibility, artifact hash, skops type allowlist and a
    real load. The registry stores the hash and an HMAC signature of the manifest; neither can be changed
    later (SQLite triggers).
    """
    version_dir = Path(version_dir)
    manifest = json.loads((version_dir / "manifest.json").read_text())
    missing = [k for k in REQUIRED_MANIFEST_KEYS if k not in manifest]
    if missing:
        raise RegistrationError(f"manifest missing keys: {missing}")
    if manifest["contract"] != "model_artifact_v1":
        raise RegistrationError(f"unsupported contract {manifest['contract']!r}")
    if manifest["input_schema"].get("schema_sha256") != schema_hash():
        raise RegistrationError("input schema incompatible with this gateway (schema hash differs)")
    model_path = version_dir / manifest["artifact"]["file"]
    expected = manifest["artifact"]["sha256"]
    art.load_verified(model_path, expected)  # raises ArtifactError on any integrity/type problem
    if store.get_model(manifest["version"]):
        raise RegistrationError(f"version {manifest['version']} already registered (versions are immutable)")
    key = art.signing_key(paths)
    signature = art.sign_manifest(manifest, key)
    rec = {
        "version": manifest["version"],
        "model_name": manifest["model_name"],
        "artifact_path": str(model_path.resolve()),
        "artifact_sha256": expected,
        "manifest": manifest,
        "manifest_sha256": hashlib.sha256(art.canonical_json(manifest)).hexdigest(),
        "signature": signature,
    }
    store.insert_model(rec, actor=actor)
    return rec


def verify_registry(store: Store, paths: StatePaths) -> list[dict[str, Any]]:
    """Re-check every registered artifact's bytes and manifest signature (tamper detection)."""
    key = art.signing_key(paths)
    results = []
    for m in store.list_models():
        problems = []
        try:
            actual = art.sha256_file(Path(m["artifact_path"]))
            if actual != m["artifact_sha256"]:
                problems.append("artifact_hash_mismatch")
        except FileNotFoundError:
            problems.append("artifact_missing")
        if not art.verify_signature(m["manifest"], m["signature"], key):
            problems.append("manifest_signature_invalid")
        results.append(
            {
                "version": m["version"],
                "ok": not problems,
                "problems": problems,
                "artifact_sha256": m["artifact_sha256"],
            }
        )
    return results
