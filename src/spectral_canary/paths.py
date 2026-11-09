"""Filesystem layout. Everything generated lives under one state directory (default ``var/``).

Set ``SCW_HOME`` to point a run (or a test) at an isolated state directory.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = PROJECT_ROOT / "configs"
MANIFEST_DIR = PROJECT_ROOT / "manifests"


@dataclass(frozen=True)
class StatePaths:
    home: Path

    @property
    def data(self) -> Path:
        return self.home / "data"

    @property
    def artifacts(self) -> Path:
        return self.home / "artifacts"

    @property
    def registry_db(self) -> Path:
        return self.home / "registry.sqlite"

    @property
    def runs(self) -> Path:
        return self.home / "runs"

    @property
    def signing_key(self) -> Path:
        return self.home / "secrets" / "manifest-signing.key"

    @property
    def operators(self) -> Path:
        return self.home / "secrets" / "operators.local.json"

    @property
    def reports(self) -> Path:
        return self.home / "reports"

    def ensure(self) -> StatePaths:
        for p in (self.data, self.artifacts, self.runs, self.reports, self.signing_key.parent):
            p.mkdir(parents=True, exist_ok=True)
        return self


def state_paths(home: str | os.PathLike[str] | None = None) -> StatePaths:
    if home is None:
        home = os.environ.get("SCW_HOME") or (PROJECT_ROOT / "var")
    return StatePaths(Path(home).resolve())
