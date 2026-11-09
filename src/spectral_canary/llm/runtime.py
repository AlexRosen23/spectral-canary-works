"""Wire the LLM lane: its store, triage service, controller and backend (shared by the API, demo and tests)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..paths import PROJECT_ROOT, StatePaths, state_paths
from .alerts import gold_sha256, load_gold
from .client import Backend, OllamaBackend
from .controller import LLMController
from .policy import LLMReleasePolicy
from .service import LLMService
from .store import LLMStore

GOLD_PATH = PROJECT_ROOT / "evals" / "triage" / "triage_gold_v1.jsonl"
DB_NAME = "llm-registry.sqlite"


@dataclass
class LLMRuntime:
    paths: StatePaths
    db_path: Path
    store: LLMStore
    service: LLMService
    controller: LLMController
    policy: LLMReleasePolicy
    backend: Any
    report_dir: Path
    gold_sha: str

    def close(self) -> None:
        self.service.close()
        self.store.close()


def default_llm_db(paths: StatePaths) -> Path | None:
    env = os.environ.get("SCW_LLM_DB")
    if env:
        return Path(env)
    latest = paths.runs / "llm-latest" / DB_NAME
    return latest if latest.exists() else None


def build_llm_runtime(
    home: Any = None,
    db_path: Path | None = None,
    policy: LLMReleasePolicy | None = None,
    backend: Backend | None = None,
    report_dir: Path | None = None,
    clock: Any = None,
    log: Any = None,
) -> LLMRuntime:
    paths = state_paths(home).ensure()
    db = Path(db_path) if db_path else (default_llm_db(paths) or paths.home / DB_NAME)
    store = LLMStore(db, clock=clock)
    store.run_id = os.environ.get("SCW_RUN_ID") or None
    policy = policy or LLMReleasePolicy.load()
    backend = backend or OllamaBackend()
    cases = load_gold(GOLD_PATH)
    test = [c for c in cases if c["split"] == "test"]
    dev = [c for c in cases if c["split"] == "dev"]
    sha = gold_sha256(cases)
    service = LLMService(store, paths, policy, backend)
    rdir = report_dir or db.parent / "reports"
    controller = LLMController(
        store, service, paths, policy, test, dev, sha, db.parent / "evals", postmortem_dir=rdir, log=log
    )
    return LLMRuntime(paths, db, store, service, controller, policy, backend, rdir, sha)
