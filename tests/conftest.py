"""Shared fixtures. Tests train real models once per session into an isolated SCW_HOME."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

os.environ.setdefault("OMP_NUM_THREADS", "1")

from spectral_canary.fixtures import fixture_runtime, train_fixtures
from spectral_canary.paths import PROJECT_ROOT, state_paths


@pytest.fixture(scope="session")
def scw_home(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Session state dir with the dataset + all four trained versions.

    Reuses ``var/artifacts`` (copied, never modified) when `make train-fixtures` already ran, so the suite
    does not retrain 4 models; otherwise trains them from scratch.
    """
    home = tmp_path_factory.mktemp("scw-home")
    paths = state_paths(home).ensure()
    src = state_paths(PROJECT_ROOT / "var")
    if (src.artifacts / "v3-hgb-fresh" / "manifest.json").exists() and (src.data / "train.csv").exists():
        shutil.copytree(src.data, paths.data, dirs_exist_ok=True)
        for d in src.artifacts.iterdir():
            if (d / "manifest.json").exists():
                shutil.copytree(d, paths.artifacts / d.name)
    train_fixtures(paths)  # verifies hashes against committed manifests; trains anything missing
    return home


@pytest.fixture()
def rt(scw_home: Path, tmp_path: Path):
    runtime = fixture_runtime(tmp_path / "run", home=scw_home)
    yield runtime
    runtime.close()
