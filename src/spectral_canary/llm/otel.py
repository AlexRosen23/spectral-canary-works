"""OpenTelemetry GenAI client metrics for every triage model call, exported on the gateway's ``/metrics``.

Instruments follow the OpenTelemetry GenAI semantic conventions as released in semantic-conventions v1.38.0
(``docs/gen-ai/gen-ai-metrics.md``), whose status is **Development** (names and attributes may still change):

- ``gen_ai.client.token.usage``        Histogram, unit ``{token}``, attribute ``gen_ai.token.type`` = input|output
- ``gen_ai.client.operation.duration`` Histogram, unit ``s``

with ``gen_ai.operation.name`` = ``chat``, ``gen_ai.provider.name`` (``ollama`` is a well-known value),
``gen_ai.request.model``, ``gen_ai.response.model``, ``server.address``/``server.port`` and ``error.type`` on
failures, and the spec's recommended explicit bucket boundaries. Note: on the newer
``open-telemetry/semantic-conventions-genai`` main branch these are being replaced for inference by
``gen_ai.client.inference.duration`` and ``gen_ai.client.inference.operation.{input,output}_tokens``; see
docs/LLM_RELEASE_LANE.md. Two bounded, non-standard attributes are added: ``scw.release.arm`` and
``scw.bundle.version`` (registered versions only), so Grafana can split champion vs candidate.

The SDK's Prometheus reader registers into this project's own ``telemetry.REGISTRY`` (not the global one), so
the existing ``/metrics`` endpoint, Prometheus scrape and Grafana stack pick these up unchanged.
"""

from __future__ import annotations

import threading
from typing import Any

from opentelemetry.exporter.prometheus import PrometheusMetricReader
from opentelemetry.sdk.metrics import Histogram, MeterProvider
from opentelemetry.sdk.metrics.view import ExplicitBucketHistogramAggregation, View
from opentelemetry.sdk.resources import Resource

from .. import telemetry as tm

SEMCONV_SCHEMA_URL = "https://opentelemetry.io/schemas/1.38.0"
TOKEN_BUCKETS = (1, 4, 16, 64, 256, 1024, 4096, 16384, 65536, 262144, 1048576, 4194304, 16777216, 67108864)
DURATION_BUCKETS = (0.01, 0.02, 0.04, 0.08, 0.16, 0.32, 0.64, 1.28, 2.56, 5.12, 10.24, 20.48, 40.96, 81.92)

_lock = threading.Lock()
_inst: dict[str, Any] = {}


def _instruments() -> dict[str, Any]:
    with _lock:
        if _inst:
            return _inst
        reader = PrometheusMetricReader(registry=tm.REGISTRY)
        provider = MeterProvider(
            resource=Resource.create({"service.name": "spectral-canary-gateway"}),
            metric_readers=[reader],
            views=[
                View(
                    instrument_type=Histogram,
                    instrument_name="gen_ai.client.token.usage",
                    aggregation=ExplicitBucketHistogramAggregation(TOKEN_BUCKETS),
                ),
                View(
                    instrument_type=Histogram,
                    instrument_name="gen_ai.client.operation.duration",
                    aggregation=ExplicitBucketHistogramAggregation(DURATION_BUCKETS),
                ),
            ],
        )
        meter = provider.get_meter("spectral_canary.llm", schema_url=SEMCONV_SCHEMA_URL)
        _inst["tokens"] = meter.create_histogram(
            "gen_ai.client.token.usage", unit="{token}", description="Number of input and output tokens used"
        )
        _inst["duration"] = meter.create_histogram(
            "gen_ai.client.operation.duration", unit="s", description="GenAI operation duration"
        )
        _inst["provider"] = provider
        return _inst


def record_call(backend: Any, lb: Any, result: Any, arm: str) -> None:
    inst = _instruments()
    attrs: dict[str, Any] = {
        "gen_ai.operation.name": "chat",
        "gen_ai.provider.name": getattr(backend, "provider", "unknown"),
        "gen_ai.request.model": lb.model,
        "scw.release.arm": arm,
        "scw.bundle.version": lb.version,
    }
    if getattr(backend, "server_address", None):
        attrs["server.address"] = backend.server_address
        attrs["server.port"] = backend.server_port
    if result.ok:
        attrs["gen_ai.response.model"] = result.model or lb.model
        inst["tokens"].record(result.input_tokens, {**attrs, "gen_ai.token.type": "input"})
        inst["tokens"].record(result.output_tokens, {**attrs, "gen_ai.token.type": "output"})
    else:
        attrs["error.type"] = result.error_type or "_OTHER"
    inst["duration"].record(result.latency_ms / 1000.0, attrs)
