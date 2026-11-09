"""Spectral Canary Works: local ML release and reliability lab."""

import os as _os

# Single-row inference gains nothing from OpenMP threads, and on a shared/oversubscribed machine
# OpenMP spin-waiting makes it dramatically slower. Default to one thread unless the caller chose.
_os.environ.setdefault("OMP_NUM_THREADS", "1")

__version__ = "0.1.0"
