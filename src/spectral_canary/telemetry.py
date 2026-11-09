"""Prometheus metrics and structured, redacted logging.

Label cardinality is bounded on purpose: ``model_version`` is limited to registered versions, ``arm`` to
champion/candidate/rejected, and nothing is labeled per request, user, card or trace id.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from typing import Any

import psutil
from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, generate_latest

REGISTRY = CollectorRegistry(auto_describe=True)
_LAT_BUCKETS = (0.001, 0.002, 0.004, 0.008, 0.016, 0.032, 0.064, 0.128, 0.256, 0.512, 1.0, 2.0)

REQUESTS = Counter(
    "scw_requests_total",
    "Prediction requests by arm, serving model version and status",
    ["arm", "model_version", "status"],
    registry=REGISTRY,
)
REQUEST_LATENCY = Histogram(
    "scw_request_latency_seconds",
    "End-to-end handler latency of successful predictions",
    ["arm", "model_version"],
    buckets=_LAT_BUCKETS,
    registry=REGISTRY,
)
MODEL_INFERENCE = Histogram(
    "scw_model_inference_seconds",
    "Model call latency (includes injected faults)",
    ["model_version"],
    buckets=_LAT_BUCKETS,
    registry=REGISTRY,
)
SCHEMA_ERRORS = Counter("scw_schema_errors_total", "Requests rejected by schema validation", registry=REGISTRY)
FALLBACKS = Counter(
    "scw_candidate_fallbacks_total",
    "Canary requests answered by the champion instead",
    ["reason"],
    registry=REGISTRY,
)
SHADOW = Counter("scw_shadow_requests_total", "Shadow scoring outcomes", ["status"], registry=REGISTRY)
MODEL_LOAD_FAILURES = Counter(
    "scw_model_load_failures_total",
    "Artifact verification/load failures",
    ["model_version"],
    registry=REGISTRY,
)
TELEMETRY_DROPPED = Counter(
    "scw_telemetry_dropped_total", "Request rows dropped by the bounded writer", registry=REGISTRY
)
GUARDRAIL_EVALS = Counter(
    "scw_guardrail_evaluations_total",
    "Canary guardrail evaluations by verdict",
    ["verdict"],
    registry=REGISTRY,
)
RELEASE_DECISIONS = Counter(
    "scw_release_decisions_total", "Release controller decisions", ["decision"], registry=REGISTRY
)
ROLLBACKS = Counter("scw_rollbacks_total", "Rollbacks executed", ["trigger"], registry=REGISTRY)
DEPLOYMENT_GENERATION = Gauge("scw_deployment_generation", "Deployment pointer generation", registry=REGISTRY)
CANARY_FRACTION = Gauge("scw_canary_fraction", "Current canary traffic fraction", registry=REGISTRY)
PROCESS_RSS = Gauge("scw_process_resident_memory_bytes", "Resident memory of the API process", registry=REGISTRY)
PROCESS_CPU = Gauge("scw_process_cpu_seconds_total", "CPU seconds used by the API process", registry=REGISTRY)
EVENT_STREAM_DROPPED = Counter(
    "scw_event_stream_dropped_total", "SSE events dropped for slow clients", registry=REGISTRY
)

_PROC = psutil.Process(os.getpid())


def render_metrics() -> bytes:
    with _PROC.oneshot():
        PROCESS_RSS.set(_PROC.memory_info().rss)
        t = _PROC.cpu_times()
        PROCESS_CPU.set(t.user + t.system)
    return generate_latest(REGISTRY)


REDACT_KEYS = {"features", "authorization", "token", "x-operator-token"}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        out: dict[str, Any] = {
            "ts": round(time.time(), 3),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        extra = getattr(record, "fields", None)
        if isinstance(extra, dict):
            out.update({k: ("[redacted]" if k.lower() in REDACT_KEYS else v) for k, v in extra.items()})
        if record.exc_info:
            out["exc"] = self.formatException(record.exc_info)
        return json.dumps(out, default=str)


def configure_logging(level: str = "INFO") -> None:
    h = logging.StreamHandler(sys.stderr)
    h.setFormatter(JsonFormatter())
    root = logging.getLogger("scw")
    root.handlers[:] = [h]
    root.setLevel(level)
    root.propagate = False
