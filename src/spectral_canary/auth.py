"""Local operator roles for release endpoints. Deliberately simple and honest: this is not enterprise IAM.

Tokens are random, generated per installation into ``var/secrets/operators.local.json`` (gitignored,
mode 0600). The server stores only SHA-256 hashes in memory and compares in constant time.
Roles: viewer < operator (start shadow/canary, pause, rollback, chaos) < release_manager (approve).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
from dataclasses import dataclass
from pathlib import Path

DEFAULT_OPERATORS = (
    ("oncall-operator", "operator"),
    ("release-manager", "release_manager"),
    ("dashboard-viewer", "viewer"),
)


@dataclass(frozen=True)
class Operator:
    id: str
    role: str


def ensure_operators_file(path: Path) -> dict[str, str]:
    """Create the local operators file if missing; return {operator_id: token} (local use only)."""
    if path.exists():
        data = json.loads(path.read_text())
        return {o["id"]: o["token"] for o in data["operators"]}
    path.parent.mkdir(parents=True, exist_ok=True)
    ops = [{"id": i, "role": r, "token": secrets.token_urlsafe(24)} for i, r in DEFAULT_OPERATORS]
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump({"note": "local demo operator tokens; never commit", "operators": ops}, fh, indent=2)
    return {o["id"]: o["token"] for o in ops}


class OperatorDirectory:
    def __init__(self, path: Path):
        data = json.loads(Path(path).read_text())
        self._by_hash = {
            hashlib.sha256(o["token"].encode()).hexdigest(): Operator(o["id"], o["role"]) for o in data["operators"]
        }

    def authenticate(self, authorization: str | None) -> Operator | None:
        if not authorization or not authorization.lower().startswith("bearer "):
            return None
        h = hashlib.sha256(authorization[7:].strip().encode()).hexdigest()
        for known, op in self._by_hash.items():
            if hmac.compare_digest(known, h):
                return op
        return None
