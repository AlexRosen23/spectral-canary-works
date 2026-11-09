"""Validate every trained manifest (and the committed example) against contracts/model_artifact_v1.

Also checks schema compatibility: every manifest's input schema hash must equal the gateway's.
Exit code 1 on any violation. Run via `make contract-check`.
"""

from __future__ import annotations

import json
import sys

from jsonschema import Draft202012Validator

from spectral_canary.paths import PROJECT_ROOT, state_paths
from spectral_canary.schema import schema_hash

schema = json.loads((PROJECT_ROOT / "contracts" / "model_artifact_v1.schema.json").read_text())
v = Draft202012Validator(schema)
targets = sorted(state_paths().artifacts.glob("*/manifest.json")) + sorted(
    (PROJECT_ROOT / "contracts" / "examples").glob("*.json")
)
bad = 0
for p in targets:
    m = json.loads(p.read_text())
    errs = sorted(v.iter_errors(m), key=lambda e: list(e.path))
    compat = m.get("input_schema", {}).get("schema_sha256") == schema_hash()
    status = "ok" if not errs and compat else "FAIL"
    print(f"{status:4}  {p.relative_to(PROJECT_ROOT)}")
    for e in errs[:5]:
        print(f"      {'/'.join(map(str, e.path))}: {e.message}")
    if not compat:
        print("      input schema hash differs from the gateway's (incompatible schema)")
    bad += status != "ok"
if not targets:
    print("no manifests found; run `make train-fixtures`")
    sys.exit(1)
print(f"{len(targets) - bad}/{len(targets)} manifests satisfy model_artifact_v1 and the gateway schema")
sys.exit(1 if bad else 0)
