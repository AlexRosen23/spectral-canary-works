"""LLM backends for the triage lane.

- ``OllamaBackend`` calls a local Ollama server (``POST /api/chat``, non-streaming) with the bundle's system
  prompt, sampling params and a JSON-schema ``format`` (Ollama's structured outputs). Token counts come from
  Ollama's ``prompt_eval_count`` / ``eval_count``; latency is wall-clock around the HTTP call.
- ``ScriptedBackend`` is a **test double** (deterministic Python functions standing in for models) so unit
  tests and chaos replay run without Ollama. Its outputs are never reported as model results.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

from .graders import OUTPUT_SCHEMA

DEFAULT_OLLAMA = os.environ.get("SCW_OLLAMA_URL", "http://127.0.0.1:11434")


@dataclass
class CallResult:
    text: str | None
    ok: bool
    latency_ms: float
    input_tokens: int = 0
    output_tokens: int = 0
    model: str = ""
    error: str | None = None
    error_type: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)


class Backend(Protocol):
    provider: str

    def chat(self, bundle: dict[str, Any], alert: dict[str, Any], seed: int, timeout_s: float) -> CallResult: ...

    def model_digest(self, model: str) -> str | None: ...


def call_seed(*parts: Any) -> int:
    """Deterministic per-call sampling seed (so a replay of the same request samples the same way)."""
    return int(hashlib.sha256(":".join(map(str, parts)).encode()).hexdigest()[:8], 16) % 2_000_000_000


def ordered_schema(order: list[str] | None) -> dict[str, Any]:
    """The output contract with its keys in the bundle's generation order. Key order changes what the model
    writes first (e.g. rationale before the decision), never which keys or values are valid."""
    if not order:
        return OUTPUT_SCHEMA
    props = OUTPUT_SCHEMA["properties"]
    return {**OUTPUT_SCHEMA, "properties": {k: props[k] for k in order}, "required": list(order)}


def render_facts(fields: dict[str, Any]) -> str:
    """Normalized key facts from the alerting system's structured fields (used by templates with ``{facts}``)."""

    def h(v: Any) -> str:
        return "none" if v is None else f"{float(v):.1f}"

    return "\n".join(
        [
            f"- amount_usd: {float(fields['amount']):.2f}",
            f"- card_present: {'yes' if fields['channel'] == 'card_present' else 'no'}",
            f"- device_first_seen_hours_before_purchase: {h(fields['device_age_hours'])}",
            f"- password_reset_hours_before_purchase: {h(fields['password_reset_hours_ago'])}",
            f"- ship_to_added_hours_before_purchase: {h(fields['ship_to_added_hours_ago'])}",
            f"- authorizations_under_2usd_past_hour: {int(fields['small_auths_1h'])}",
            f"- declines_past_hour: {int(fields['declines_1h'])}",
            f"- prior_chargebacks: {int(fields['prior_chargebacks'])}",
        ]
    )


def render_user(bundle: dict[str, Any], alert: dict[str, Any]) -> str:
    t = str(bundle["user_template"]).replace("{summary}", str(alert["summary"]))
    if "{facts}" in t:
        t = t.replace("{facts}", render_facts(alert["fields"]))
    return t


class OllamaBackend:
    provider = "ollama"

    def __init__(self, base_url: str = DEFAULT_OLLAMA):
        self.base_url = base_url.rstrip("/")
        self.http = httpx.Client(base_url=self.base_url, timeout=None)
        u = httpx.URL(self.base_url)
        self.server_address = u.host
        self.server_port = u.port or 11434

    def chat(self, bundle: dict[str, Any], alert: dict[str, Any], seed: int, timeout_s: float) -> CallResult:
        """Sends the rendered user template: the summary, plus a key-facts block only if the template asks."""
        body = {
            "model": bundle["model"],
            "stream": False,
            "format": ordered_schema(bundle.get("output_key_order")),
            "keep_alive": "30m",
            "options": {**bundle.get("params", {}), "seed": seed},
            "messages": [
                {"role": "system", "content": bundle["system_prompt"]},
                {"role": "user", "content": render_user(bundle, alert)},
            ],
        }
        t0 = time.perf_counter()
        try:
            r = self.http.post("/api/chat", json=body, timeout=timeout_s)
            ms = (time.perf_counter() - t0) * 1000
            if r.status_code != 200:
                return CallResult(
                    None,
                    False,
                    ms,
                    model=bundle["model"],
                    error=f"http {r.status_code}: {r.text[:200]}",
                    error_type=f"http_{r.status_code}",
                )
            j = r.json()
            return CallResult(
                j.get("message", {}).get("content"),
                True,
                ms,
                int(j.get("prompt_eval_count") or 0),
                int(j.get("eval_count") or 0),
                str(j.get("model") or bundle["model"]),
                meta={
                    "load_ms": round((j.get("load_duration") or 0) / 1e6, 1),
                    "server_total_ms": round((j.get("total_duration") or 0) / 1e6, 1),
                },
            )
        except httpx.TimeoutException:
            return CallResult(
                None,
                False,
                (time.perf_counter() - t0) * 1000,
                model=bundle["model"],
                error=f"timeout after {timeout_s:.1f}s",
                error_type="timeout",
            )
        except httpx.HTTPError as exc:
            return CallResult(
                None,
                False,
                (time.perf_counter() - t0) * 1000,
                model=bundle["model"],
                error=f"{type(exc).__name__}: {exc}",
                error_type=type(exc).__name__,
            )

    def model_digest(self, model: str) -> str | None:
        try:
            tags = self.http.get("/api/tags", timeout=10).json().get("models", [])
        except (httpx.HTTPError, json.JSONDecodeError):
            return None
        for m in tags:
            if m.get("name") == model or m.get("model") == model:
                return str(m.get("digest"))
        return None

    def available(self) -> bool:
        try:
            return self.http.get("/api/version", timeout=3).status_code == 200
        except httpx.HTTPError:
            return False


Responder = Callable[[dict[str, Any], dict[str, Any], int], dict[str, Any] | str]


class ScriptedBackend:
    """TEST DOUBLE. ``responders[bundle_version](bundle, alert_fields, seed)`` returns a reply object (or raw text).

    Latency and token counts are synthetic (``latency_ms`` / ``tokens`` per version) so cost/latency guardrails
    can be exercised deterministically. Faults: ``fail_versions`` makes calls error.
    """

    provider = "scripted-test-double"

    def __init__(
        self,
        responders: dict[str, Responder],
        latency_ms: dict[str, float] | None = None,
        tokens: dict[str, tuple[int, int]] | None = None,
    ):
        self.responders = responders
        self.latency_ms = latency_ms or {}
        self.tokens = tokens or {}
        self.fail_versions: set[str] = set()
        self.calls = 0

    def chat(self, bundle: dict[str, Any], alert: dict[str, Any], seed: int, timeout_s: float) -> CallResult:
        self.calls += 1
        v = bundle["version"]
        ms = float(self.latency_ms.get(v, 5.0))
        if v in self.fail_versions:
            return CallResult(None, False, ms, model=bundle["model"], error="scripted failure", error_type="scripted")
        out = self.responders[v](bundle, alert.get("fields") or {}, seed)
        text = out if isinstance(out, str) else json.dumps(out)
        ti, to = self.tokens.get(v, (150, 40))
        return CallResult(text, True, ms, ti, to, bundle["model"])

    def model_digest(self, model: str) -> str | None:
        return f"scripted:{model}"
