"""Fast CLI checks (no server, no training)."""

from __future__ import annotations

from spectral_canary.cli import main


def test_demo_refuses_a_scale_that_would_starve_the_shadow_phase(capsys) -> None:
    """`scw demo --scale 0.3` used to crash mid-story with a 409 traceback; it now refuses up front with the reason."""
    assert main(["demo", "--scale", "0.3"]) == 2
    err = capsys.readouterr().err
    assert "too small" in err and "400" in err
