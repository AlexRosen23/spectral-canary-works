"""FastAPI gateway: typed predictions, feedback, registry, release control, events, metrics, evidence.

Run: ``make serve`` (uvicorn on 127.0.0.1:5120). Release-changing endpoints need a bearer token from
``var/secrets/operators.local.json``; read endpoints are open on localhost.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import numpy as np
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import contract as ct
from . import telemetry as tm
from .auth import Operator
from .controller import ReleaseError
from .metrics import percentiles
from .monitor import drift_report
from .paths import PROJECT_ROOT
from .registry import verify_registry
from .runtime import Runtime, build_runtime
from .schema import schema_dict
from .serving import Fault

log = logging.getLogger("scw.api")


class PredictRequest(BaseModel):
    request_id: str | None = Field(None, max_length=128)
    request_key: str | None = Field(None, max_length=128, description="Stable routing key, e.g. card id")
    features: dict[str, Any]
    context: dict[str, Any] = Field(default_factory=dict)


class PredictResponse(BaseModel):
    request_id: str
    trace_id: str
    model_version: str
    arm: str
    score: float
    decision: bool
    threshold: float
    model_sha256: str
    schema_version: str
    latency_ms: float
    fallback: str | None
    simulated: bool


class FeedbackRequest(BaseModel):
    request_id: str = Field(max_length=128)
    label: int = Field(ge=0, le=1)
    simulated: bool = False


class ProposeRequest(BaseModel):
    candidate_version: str


class RollbackRequest(BaseModel):
    reason: str = Field("manual rollback", max_length=500)


class FaultRequest(BaseModel):
    version: str
    latency_ms: float = Field(0.0, ge=0, le=5000)
    error_rate: float = Field(0.0, ge=0, le=1)
    seed: int = 0
    note: str = Field("", max_length=200)


def create_app(rt: Runtime | None = None, start_background: bool = True) -> FastAPI:
    state: dict[str, Any] = {"rt": rt}

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if state["rt"] is None:
            state["rt"] = build_runtime()
        runtime: Runtime = state["rt"]
        actions = runtime.controller.recover()
        if actions:
            log.warning("startup recovery", extra={"fields": {"actions": actions}})
        stop = threading.Event()
        if start_background:

            def ticker() -> None:
                while not stop.wait(5.0):
                    try:
                        runtime.controller.tick()
                    except Exception:  # pragma: no cover
                        log.exception("controller tick failed")

            threading.Thread(target=ticker, name="scw-ticker", daemon=True).start()
        yield
        stop.set()
        runtime.service.writer.flush()

    app = FastAPI(
        title="Spectral Canary Works",
        version="0.1.0",
        lifespan=lifespan,
        description="Local ML release & reliability lab. Simulated traffic is labeled as such.",
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://127.0.0.1:5121", "http://localhost:5121"],
        allow_methods=["GET", "POST"],
        allow_headers=["*"],
        expose_headers=["X-Model-Version", "X-Release-Arm", "X-Trace-Id", "X-Fallback"],
    )

    def R() -> Runtime:
        r = state["rt"]
        if r is None:
            raise HTTPException(503, "runtime not ready")
        return r

    def operator(authorization: str | None = Header(None)) -> Operator:
        op = R().operators.authenticate(authorization)
        if op is None:
            raise HTTPException(401, "missing or invalid bearer token (see var/secrets/operators.local.json)")
        return op

    def need(op: Operator, role: str) -> None:
        try:
            R().controller.require_role(op.role, role)
        except ReleaseError as exc:
            raise HTTPException(exc.status, str(exc)) from exc

    def call(fn: Any, *args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except ReleaseError as exc:
            raise HTTPException(exc.status, str(exc)) from exc

    # -- health ---------------------------------------------------------------------------------------------
    @app.get("/healthz")
    def healthz() -> dict[str, Any]:
        return {"status": "alive", "time": time.time()}

    @app.get("/readyz")
    def readyz() -> JSONResponse:
        h = R().controller.health()
        return JSONResponse(h, status_code=200 if h["ready"] else 503)

    @app.get("/metrics")
    def metrics() -> Response:
        return Response(tm.render_metrics(), media_type="text/plain; version=0.0.4")

    # -- inference ------------------------------------------------------------------------------------------
    @app.post(
        "/v1/predict",
        response_model=PredictResponse,
        responses={422: {"description": "schema validation failed"}, 503: {"description": "no model"}},
    )
    def predict(req: PredictRequest, x_trace_id: str | None = Header(None)) -> JSONResponse:
        out = R().service.predict(req.model_dump(), trace_id=x_trace_id)
        log.info(
            "predict",
            extra={
                "fields": {
                    "request_id": out.body.get("request_id"),
                    "status": out.http_status,
                    "model_version": out.body.get("model_version"),
                    "arm": out.body.get("arm"),
                    "trace_id": out.body.get("trace_id"),
                    "latency_ms": out.body.get("latency_ms"),
                }
            },
        )
        return JSONResponse(out.body, status_code=out.http_status, headers=out.headers)

    @app.post("/v1/feedback")
    def feedback(req: FeedbackRequest) -> dict[str, Any]:
        inserted = R().store.insert_feedback(req.request_id, req.label, req.simulated)
        return {"accepted": inserted, "duplicate": not inserted}

    @app.get("/v1/schema")
    def schema() -> dict[str, Any]:
        return schema_dict()

    # -- registry -------------------------------------------------------------------------------------------
    @app.get("/v1/registry/models")
    def models() -> list[dict[str, Any]]:
        out = []
        for m in R().store.list_models():
            man = m["manifest"]
            t = man["metrics"]["test"]
            out.append(
                {
                    "version": m["version"],
                    "model_name": m["model_name"],
                    "family": man["family"],
                    "registered_at": m["created_at"],
                    "artifact_sha256": m["artifact_sha256"],
                    "manifest_sha256": m["manifest_sha256"],
                    "signature": m["signature"][:16] + "…",
                    "owner": man["owner"],
                    "deliberately_degraded": man.get("deliberately_degraded", False),
                    "change_summary": man["change_summary"],
                    "dataset_hash": man["dataset"]["dataset_hash"],
                    "config_sha256": man["training"]["config_sha256"],
                    "source": man["source"],
                    "schema_version": man["schema_version"],
                    "threshold": man["threshold"]["value"],
                    "test_metrics": {
                        k: t[k]
                        for k in (
                            "pr_auc",
                            "pr_auc_ci95",
                            "precision",
                            "recall",
                            "f1",
                            "brier",
                            "ece",
                            "roc_auc",
                            "n",
                            "positives",
                        )
                    },
                    "latency_single_row": man["latency_single_row"],
                    "bytes": man["artifact"]["bytes"],
                }
            )
        return out

    @app.get("/v1/registry/models/{version}")
    def model(version: str) -> dict[str, Any]:
        m = R().store.get_model(version)
        if not m:
            raise HTTPException(404, "unknown version")
        card = Path(m["artifact_path"]).with_name("MODEL_CARD.md")
        return {**m, "model_card_markdown": card.read_text() if card.exists() else None}

    @app.get("/v1/registry/verify")
    def verify() -> dict[str, Any]:
        rt = R()
        return {"models": verify_registry(rt.store, rt.paths), "event_chain": rt.store.verify_event_chain()}

    # -- deployment & releases ------------------------------------------------------------------------------
    @app.get("/v1/deployment")
    def deployment() -> dict[str, Any]:
        rt = R()
        return {
            **rt.store.deployment().as_dict(),
            "loaded_versions": rt.service.pool.loaded_versions(),
            "active_faults": rt.faults.active(),
            "health": rt.controller.health(),
        }

    @app.get("/v1/releases")
    def releases() -> list[dict[str, Any]]:
        return R().store.list_releases()

    @app.get("/v1/releases/{release_id}")
    def release(release_id: str) -> dict[str, Any]:
        r = R().store.get_release(release_id)
        if not r:
            raise HTTPException(404, "unknown release")
        return r

    @app.get("/v1/releases/{release_id}/evaluations")
    def evaluations(release_id: str) -> list[dict[str, Any]]:
        return [e for e in R().store.events(release_id=release_id, limit=100_000) if e["kind"] == "guardrail.evaluated"]

    @app.get("/v1/releases/{release_id}/shadow")
    def shadow(release_id: str) -> dict[str, Any]:
        return call(R().controller.shadow_report, release_id)

    @app.post("/v1/releases")
    def propose(req: ProposeRequest, op: Operator = Depends(operator)) -> dict[str, Any]:
        need(op, "operator")
        return call(R().controller.propose, req.candidate_version, op.id)

    @app.post("/v1/releases/{release_id}/stage")
    def stage(release_id: str, op: Operator = Depends(operator)) -> dict[str, Any]:
        need(op, "operator")
        return call(R().controller.to_staging, release_id, op.id)

    @app.post("/v1/releases/{release_id}/shadow")
    def start_shadow(release_id: str, op: Operator = Depends(operator)) -> dict[str, Any]:
        need(op, "operator")
        return call(R().controller.to_shadow, release_id, op.id)

    @app.post("/v1/releases/{release_id}/canary")
    def start_canary(release_id: str, op: Operator = Depends(operator)) -> dict[str, Any]:
        need(op, "operator")
        return call(R().controller.to_canary, release_id, op.id)

    @app.post("/v1/releases/{release_id}/evaluate")
    def evaluate(release_id: str, op: Operator = Depends(operator)) -> dict[str, Any]:
        need(op, "operator")
        return call(R().controller.evaluate, release_id, op.id)

    @app.post("/v1/releases/{release_id}/approve")
    def approve(release_id: str, op: Operator = Depends(operator)) -> dict[str, Any]:
        return call(R().controller.approve, release_id, op.id, op.role)

    @app.post("/v1/releases/{release_id}/pause")
    def pause(release_id: str, req: RollbackRequest, op: Operator = Depends(operator)) -> dict[str, Any]:
        need(op, "operator")
        return call(R().controller.pause, release_id, op.id, req.reason)

    @app.post("/v1/releases/{release_id}/rollback")
    def rollback(release_id: str, req: RollbackRequest, op: Operator = Depends(operator)) -> dict[str, Any]:
        need(op, "operator")
        return call(R().controller.rollback, release_id, op.id, "manual", req.reason)

    # -- chaos (simulated faults; disabled unless SCW_ENABLE_CHAOS=1) ---------------------------------------
    def chaos_enabled() -> None:
        if os.environ.get("SCW_ENABLE_CHAOS") != "1":
            raise HTTPException(403, "chaos endpoints are disabled (set SCW_ENABLE_CHAOS=1)")

    @app.post("/v1/chaos/faults")
    def set_fault(req: FaultRequest, op: Operator = Depends(operator)) -> dict[str, Any]:
        chaos_enabled()
        need(op, "operator")
        rt = R()
        rt.faults.set(req.version, Fault(req.latency_ms, req.error_rate, req.seed, req.note))
        rt.store.append_event("chaos.fault_injected", req.model_dump(), actor=op.id, simulated=True)
        return {"active_faults": rt.faults.active()}

    @app.delete("/v1/chaos/faults")
    def clear_faults(op: Operator = Depends(operator)) -> dict[str, Any]:
        chaos_enabled()
        need(op, "operator")
        rt = R()
        rt.faults.clear()
        rt.store.append_event("chaos.faults_cleared", {}, actor=op.id, simulated=True)
        return {"active_faults": {}}

    # -- events ---------------------------------------------------------------------------------------------
    @app.get("/v1/events")
    def events(after: int = 0, limit: int = Query(500, le=5000), release_id: str | None = None) -> list[dict]:
        return R().store.events(after=after, limit=limit, release_id=release_id)

    @app.get("/v1/events/stream")
    async def stream(request: Request, after: int = 0) -> StreamingResponse:
        """Server-sent events. Backpressure: at most 200 events per send; if a client falls more than 2000
        events behind it gets one ``resync`` event and must refetch (counted in metrics)."""
        rt = R()

        async def gen() -> AsyncIterator[str]:
            cursor = after
            while True:
                if await request.is_disconnected():
                    break
                batch = rt.store.events(after=cursor, limit=2001)
                if len(batch) > 2000:
                    tm.EVENT_STREAM_DROPPED.inc(len(batch))
                    cursor = batch[-1]["seq"]
                    yield f"event: resync\ndata: {json.dumps({'seq': cursor})}\n\n"
                    continue
                for e in batch[:200]:
                    cursor = e["seq"]
                    yield f"id: {e['seq']}\nevent: {e['kind']}\ndata: {json.dumps(e, default=str)}\n\n"
                if len(batch) <= 200:
                    yield ": keepalive\n\n"
                    await asyncio.sleep(0.5)

        return StreamingResponse(
            gen(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # -- observed telemetry for the control room ------------------------------------------------------------
    @app.get("/v1/requests/recent")
    def recent(limit: int = Query(100, le=1000)) -> list[dict[str, Any]]:
        R().service.writer.flush()
        return R().store.recent_requests(limit)

    @app.get("/v1/traffic/series")
    def series(
        bucket: int = Query(250, ge=10, le=5000), release_id: str | None = None, since_seq: int = 0
    ) -> dict[str, Any]:
        rt = R()
        rt.service.writer.flush()
        rows = rt.store.requests_since(since_seq, release_id)
        rows = [r for r in rows if not (r.get("scenario") or "").endswith("_verification")]
        out = []
        for i in range(0, len(rows), bucket):
            chunk = rows[i : i + bucket]
            arms: dict[str, Any] = {}
            for arm in ("champion", "candidate"):
                a = [r for r in chunk if r["arm"] == arm and r["status"] == "ok"]
                served = [r for r in a if arm == "champion" or r.get("shadow_status") == "ok"]
                labeled_pos = [r for r in served if r.get("label") == 1]
                arms[arm] = {
                    "requests": len(a),
                    "flag_rate": round(float(np.mean([r["decision"] for r in served])), 4) if served else None,
                    "model_failures": sum(
                        1 for r in a if r.get("shadow_status") in ("error", "timeout", "unavailable")
                    ),
                    "recall_labeled": round(float(np.mean([r["decision"] for r in labeled_pos])), 4)
                    if labeled_pos
                    else None,
                    "labeled_positives": len(labeled_pos),
                    "versions": sorted({r["served_version"] for r in a if r.get("served_version")}),
                }
            lat = [r["latency_ms"] for r in chunk if r["status"] == "ok"]
            out.append(
                {
                    "start_seq": chunk[0]["seq"],
                    "end_seq": chunk[-1]["seq"],
                    "t0": chunk[0]["ts"],
                    "t1": chunk[-1]["ts"],
                    "requests": len(chunk),
                    "schema_errors": sum(1 for r in chunk if r["status"] == "schema_error"),
                    "errors": sum(1 for r in chunk if r["status"] == "error"),
                    "latency_ms": percentiles(lat),
                    "arms": arms,
                    "scenarios": sorted({r["scenario"] or "unlabeled" for r in chunk}),
                    "simulated_share": round(float(np.mean([r["simulated"] for r in chunk])), 4),
                    "fallbacks": sum(1 for r in chunk if r.get("fallback")),
                }
            )
        return {"bucket": bucket, "series": out, "total": len(rows)}

    @app.get("/v1/drift")
    def drift(last: int = Query(1500, ge=50, le=50_000), release_id: str | None = None) -> dict[str, Any]:
        rt = R()
        rt.service.writer.flush()
        rows = rt.store.requests_since(0, release_id)[-last:]
        rows = [r for r in rows if r["status"] == "ok"]
        return drift_report(rows, rt.paths.data / "train.csv")

    # -- recorded runs (demo / chaos / load test reports) ---------------------------------------------------
    @app.get("/v1/runs")
    def runs() -> list[dict[str, Any]]:
        out = []
        for d in sorted(R().paths.runs.glob("*/report.json")):
            if d.parent.name == "latest":
                continue
            rep = json.loads(d.read_text())
            out.append(
                {
                    "run_id": rep.get("run_id"),
                    "kind": rep.get("kind"),
                    "started_at": rep.get("started_at"),
                    "passed": rep.get("passed"),
                    "summary": rep.get("summary"),
                }
            )
        return out

    @app.get("/v1/runs/{run_id}")
    def run(run_id: str) -> dict[str, Any]:
        p = R().paths.runs / run_id / "report.json"
        if not p.resolve().is_relative_to(R().paths.runs.resolve()) or not p.exists():
            raise HTTPException(404, "unknown run")
        return json.loads(p.read_text())

    @app.get("/v1/runs/{run_id}/postmortem", response_class=PlainTextResponse)
    def run_postmortem(run_id: str, release_id: str) -> str:
        base = (R().paths.runs / run_id / "reports").resolve()
        p = (base / f"postmortem-{release_id}.md").resolve()
        if not p.is_relative_to(base) or not p.exists():
            raise HTTPException(404, "no postmortem")
        return p.read_text()

    @app.get("/v1/overview")
    def overview() -> dict[str, Any]:
        rt = R()
        rels = rt.store.list_releases()
        return {
            "deployment": rt.store.deployment().as_dict(),
            "health": rt.controller.health(),
            "models": len(rt.store.list_models()),
            "releases": len(rels),
            "active_release": rt.controller.active_release(),
            "latest_release": rels[-1] if rels else None,
            "event_chain": rt.store.verify_event_chain(),
            "db": str(rt.db_path.name),
            "policy_sha256": rt.policy.sha256,
        }

    # -- read-only project contract (dashboards such as the owner's Constellation read these) --------------------
    @app.get("/health")
    def health() -> dict[str, Any]:
        return ct.health_answer(R().controller.health(), R().paths.runs)

    @app.get("/capabilities")
    def capabilities() -> dict[str, Any]:
        return ct.capabilities_answer()

    @app.get("/runs")
    def contract_runs(limit: int = Query(20, ge=1, le=200)) -> dict[str, Any]:
        return ct.runs_answer(R().paths.runs, limit)

    @app.get("/runs/{run_id}")
    def contract_run(run_id: str) -> dict[str, Any]:
        r = ct.run_answer(R().paths.runs, run_id)
        if r is None:
            raise HTTPException(404, "unknown run")
        return r

    # The built control room (web/dist), served same-origin so one process opens the whole app. Mounted last so every
    # API route above wins.
    dist = PROJECT_ROOT / "web" / "dist"
    if (dist / "index.html").exists():
        app.mount("/", StaticFiles(directory=dist, html=True), name="ui")

    return app


app = create_app()
