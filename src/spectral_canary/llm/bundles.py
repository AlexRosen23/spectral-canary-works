"""Prompt bundles: the releasable unit of the LLM lane = prompt text + model (+ pinned weights digest) + params.

A bundle is declared in ``configs/llm/bundles/<version>.toml`` and points at a prompt file. Registration
freezes it into one canonical JSON document (``contract: prompt_bundle_v1``) that embeds the full prompt text,
the output schema in generation order, the sampling params and the Ollama digest of the model weights, writes
it read-only under ``<state>/llm/bundles/`` and records it through the same immutable ``model_versions`` table,
SHA-256 and HMAC signature as an ML artifact (``Store.insert_model``). Changing one character of the prompt,
one param, or the weights behind a model tag produces a different hash, so it is a different release.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .. import artifacts as art
from ..paths import CONFIG_DIR, StatePaths
from ..store import Store
from .client import ordered_schema

BUNDLE_DIR = CONFIG_DIR / "llm" / "bundles"
CONTRACT = "prompt_bundle_v1"
MODEL_NAME = "fraud-alert-triage"


class BundleError(RuntimeError):
    pass


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def compile_bundle(toml_path: Path, model_digest: str | None) -> dict[str, Any]:
    spec = tomllib.loads(Path(toml_path).read_text())
    prompt = (Path(toml_path).parent.parent / spec["prompt_file"]).read_text()
    schema = ordered_schema(spec.get("output_key_order"))
    return {
        "contract": CONTRACT,
        "model_name": MODEL_NAME,
        "version": spec["version"],
        "prompt_version": spec["prompt_version"],
        "provider": spec.get("provider", "ollama"),
        "model": spec["model"],
        "model_digest": model_digest,
        "system_prompt": prompt,
        "system_prompt_sha256": _sha(prompt),
        "user_template": spec["user_template"],
        "output_key_order": spec.get("output_key_order"),
        "output_schema": schema,
        "output_schema_sha256": _sha(json.dumps(schema, sort_keys=False)),
        "params": dict(spec.get("params", {})),
        "seed_policy": "seed = sha256(request_id or case_id:try) mod 2e9, so replays sample identically",
        "owner": spec.get("owner", "fraud-ops"),
        "change_summary": spec.get("change_summary", ""),
        "deliberately_regressed": bool(spec.get("deliberately_regressed", False)),
        "source": f"configs/llm/bundles/{Path(toml_path).name}",
    }


def bundle_dir(paths: StatePaths) -> Path:
    return paths.home / "llm" / "bundles"


def register_bundle(store: Store, paths: StatePaths, bundle: dict[str, Any], actor: str) -> dict[str, Any]:
    if bundle.get("contract") != CONTRACT:
        raise BundleError(f"unsupported contract {bundle.get('contract')!r}")
    if not bundle.get("model_digest"):
        raise BundleError(f"{bundle['version']}: model {bundle['model']!r} not available (no weights digest)")
    if store.get_model(bundle["version"]):
        raise BundleError(f"version {bundle['version']} already registered (versions are immutable)")
    d = bundle_dir(paths)
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"{bundle['version']}.json"
    data = art.canonical_json(bundle)
    if f.exists():
        os.chmod(f, 0o644)
    f.write_bytes(data)
    os.chmod(f, 0o444)
    sha = hashlib.sha256(data).hexdigest()
    rec = {
        "version": bundle["version"],
        "model_name": MODEL_NAME,
        "artifact_path": str(f.resolve()),
        "artifact_sha256": sha,
        "manifest": bundle,
        "manifest_sha256": sha,
        "signature": art.sign_manifest(bundle, art.signing_key(paths)),
    }
    store.insert_model(rec, actor=actor)
    return rec


def register_all_bundles(
    store: Store, paths: StatePaths, backend: Any, actor: str = "registry-bot", only: list[str] | None = None
) -> list[str]:
    done = []
    for t in sorted(BUNDLE_DIR.glob("*.toml")):
        spec = tomllib.loads(t.read_text())
        if only and spec["version"] not in only:
            continue
        if store.get_model(spec["version"]) is None:
            b = compile_bundle(t, backend.model_digest(spec["model"]))
            register_bundle(store, paths, b, actor)
            done.append(spec["version"])
    return done


@dataclass(frozen=True)
class LoadedBundle:
    version: str
    bundle: dict[str, Any]
    sha256: str

    @property
    def model(self) -> str:
        return str(self.bundle["model"])


class BundlePool:
    """Same contract as ``serving.ModelPool``: verified load (signature + bytes hash), cache, reverify, evict."""

    def __init__(self, store: Store, paths: StatePaths):
        self.store = store
        self.paths = paths
        self._b: dict[str, LoadedBundle] = {}
        self._lock = threading.Lock()
        self.load_failures: dict[str, str] = {}

    def get(self, version: str) -> LoadedBundle:
        with self._lock:
            b = self._b.get(version)
        if b is not None:
            return b
        rec = self.store.get_model(version)
        if rec is None:
            raise art.ArtifactError(f"unknown_version: {version}")
        if not art.verify_signature(rec["manifest"], rec["signature"], art.signing_key(self.paths)):
            raise art.ArtifactError("manifest_signature_invalid")
        try:
            data = Path(rec["artifact_path"]).read_bytes()
        except FileNotFoundError as exc:
            self.load_failures[version] = "artifact_missing"
            raise art.ArtifactError("artifact_missing") from exc
        if hashlib.sha256(data).hexdigest() != rec["artifact_sha256"]:
            self.load_failures[version] = "artifact_hash_mismatch"
            raise art.ArtifactError("artifact_hash_mismatch")
        b = LoadedBundle(version, json.loads(data), rec["artifact_sha256"])
        with self._lock:
            self._b.setdefault(version, b)
            self.load_failures.pop(version, None)
            return self._b[version]

    def reverify(self, version: str) -> None:
        rec = self.store.get_model(version)
        if rec is None:
            raise art.ArtifactError(f"unknown_version: {version}")
        try:
            actual = art.sha256_file(Path(rec["artifact_path"]))
        except FileNotFoundError as exc:
            raise art.ArtifactError("artifact_missing") from exc
        if actual != rec["artifact_sha256"]:
            raise art.ArtifactError("artifact_hash_mismatch")

    def evict(self, version: str) -> None:
        with self._lock:
            self._b.pop(version, None)

    def loaded_versions(self) -> list[str]:
        with self._lock:
            return sorted(self._b)
