"""Wire store + inference service + controller together (shared by the API, demo, chaos and tests)."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .auth import OperatorDirectory, ensure_operators_file
from .controller import Controller
from .paths import StatePaths, state_paths
from .policy import ReleasePolicy
from .registry import register_from_dir
from .serving import FaultInjector, InferenceService
from .store import Store


@dataclass
class Runtime:
    paths: StatePaths
    db_path: Path
    store: Store
    service: InferenceService
    controller: Controller
    policy: ReleasePolicy
    faults: FaultInjector
    operators: OperatorDirectory
    report_dir: Path

    def close(self) -> None:
        self.service.close()
        self.store.close()


def default_db_path(paths: StatePaths) -> Path:
    env = os.environ.get("SCW_DB")
    if env:
        return Path(env)
    latest = paths.runs / "latest" / "registry.sqlite"
    return latest if latest.exists() else paths.registry_db


def build_runtime(
    home: str | os.PathLike[str] | None = None,
    db_path: Path | None = None,
    policy: ReleasePolicy | None = None,
    clock: Any = None,
    report_dir: Path | None = None,
) -> Runtime:
    paths = state_paths(home).ensure()
    db = Path(db_path) if db_path else default_db_path(paths)
    store = Store(db, clock=clock) if clock else Store(db)
    policy = policy or ReleasePolicy.load()
    faults = FaultInjector()
    service = InferenceService(store, paths, policy, faults)
    rdir = report_dir or db.parent / "reports"
    controller = Controller(store, service, paths, policy, postmortem_dir=rdir)
    ensure_operators_file(paths.operators)
    return Runtime(paths, db, store, service, controller, policy, faults, OperatorDirectory(paths.operators), rdir)


def register_all(rt: Runtime, actor: str = "registry-bot") -> list[str]:
    """Register every trained artifact directory not yet in this registry."""
    done = []
    for d in sorted(p for p in rt.paths.artifacts.iterdir() if (p / "manifest.json").exists()):
        version = json.loads((d / "manifest.json").read_text())["version"]
        if rt.store.get_model(version) is None:
            register_from_dir(rt.store, rt.paths, d, actor=actor)
            done.append(version)
    return done
