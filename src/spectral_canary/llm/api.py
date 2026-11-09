"""HTTP routes for the LLM triage lane, mounted under ``/v1/llm`` on the same gateway, with the same bearer-token
roles as the ML lane. Read routes are open on localhost; release-changing routes need an operator token."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from ..auth import Operator
from ..controller import ReleaseError
from ..registry import verify_registry
from .alerts import REASON_CODES
from .monitor import input_drift
from .runtime import LLMRuntime


class TriageRequest(BaseModel):
    request_id: str | None = Field(None, max_length=128)
    request_key: str | None = Field(None, max_length=128, description="Stable routing key, e.g. customer id")
    alert: dict[str, Any]
    context: dict[str, Any] = Field(default_factory=dict)


class TriageFeedback(BaseModel):
    request_id: str = Field(max_length=128)
    reason_code: str
    escalate: bool
    simulated: bool = False


class ProposeBundle(BaseModel):
    candidate_version: str


class Reason(BaseModel):
    reason: str = Field("manual rollback", max_length=500)


def llm_router(
    get_rt: Callable[[], LLMRuntime | None], operator: Any, need: Callable[[Operator, str], None]
) -> APIRouter:
    r = APIRouter(prefix="/v1/llm", tags=["llm-triage"])

    def R() -> LLMRuntime:
        rt = get_rt()
        if rt is None:
            raise HTTPException(404, "LLM lane not initialised: run `make llm-demo` (or set SCW_LLM_DB)")
        return rt

    def call(fn: Any, *a: Any) -> Any:
        try:
            return fn(*a)
        except ReleaseError as exc:
            raise HTTPException(exc.status, str(exc)) from exc

    @r.post("/triage")
    def triage(req: TriageRequest, x_trace_id: str | None = Header(None)) -> JSONResponse:
        out = R().service.predict(req.model_dump(), trace_id=x_trace_id)
        return JSONResponse(out.body, status_code=out.http_status, headers=out.headers)

    @r.post("/feedback")
    def feedback(fb: TriageFeedback) -> dict[str, Any]:
        if fb.reason_code not in REASON_CODES:
            raise HTTPException(422, f"reason_code must be one of {REASON_CODES}")
        ok = R().store.insert_llm_feedback(fb.request_id, fb.reason_code, fb.escalate, fb.simulated)
        return {"accepted": ok, "duplicate": not ok}

    @r.get("/overview")
    def overview() -> dict[str, Any]:
        rt = R()
        rels = rt.store.list_releases()
        dep = rt.store.deployment()
        return {
            "lane": "llm",
            "deployment": dep.as_dict(),
            "bundles": len(rt.store.list_models()),
            "releases": len(rels),
            "active_release": rt.controller.active_release(),
            "latest_release": rels[-1] if rels else None,
            "event_chain": rt.store.verify_event_chain(),
            "db": rt.db_path.parent.name + "/" + rt.db_path.name,
            "policy_sha256": rt.policy.sha256,
            "gold_sha256": rt.gold_sha,
            "backend": getattr(rt.backend, "provider", "unknown"),
        }

    @r.get("/registry")
    def registry() -> list[dict[str, Any]]:
        out = []
        for m in R().store.list_models():
            b = m["manifest"]
            out.append(
                {
                    "version": m["version"],
                    "prompt_version": b["prompt_version"],
                    "model": b["model"],
                    "model_digest": (b.get("model_digest") or "")[:16],
                    "params": b["params"],
                    "bundle_sha256": m["artifact_sha256"],
                    "system_prompt_sha256": b["system_prompt_sha256"],
                    "change_summary": b["change_summary"],
                    "deliberately_regressed": b["deliberately_regressed"],
                    "registered_at": m["created_at"],
                    "signature": m["signature"][:16] + "…",
                }
            )
        return out

    @r.get("/registry/verify")
    def verify() -> dict[str, Any]:
        rt = R()
        return {"bundles": verify_registry(rt.store, rt.paths), "event_chain": rt.store.verify_event_chain()}

    @r.get("/registry/{version}")
    def bundle(version: str) -> dict[str, Any]:
        m = R().store.get_model(version)
        if not m:
            raise HTTPException(404, "unknown bundle")
        return m

    @r.get("/releases")
    def releases() -> list[dict[str, Any]]:
        return R().store.list_releases()

    @r.get("/releases/{release_id}")
    def release(release_id: str) -> dict[str, Any]:
        rel = R().store.get_release(release_id)
        if not rel:
            raise HTTPException(404, "unknown release")
        return rel

    @r.get("/releases/{release_id}/shadow")
    def shadow(release_id: str) -> dict[str, Any]:
        return call(R().controller.shadow_report, release_id)

    @r.post("/releases")
    def propose(req: ProposeBundle, op: Operator = Depends(operator)) -> dict[str, Any]:
        need(op, "operator")
        return call(R().controller.propose, req.candidate_version, op.id)

    for action, fn_name in (
        ("stage", "to_staging"),
        ("shadow", "to_shadow"),
        ("canary", "to_canary"),
        ("evaluate", "evaluate"),
    ):

        def make(fn_name: str = fn_name) -> Any:
            def endpoint(release_id: str, op: Operator = Depends(operator)) -> dict[str, Any]:
                need(op, "operator")
                return call(getattr(R().controller, fn_name), release_id, op.id)

            return endpoint

        r.add_api_route(f"/releases/{{release_id}}/{action}", make(), methods=["POST"])

    @r.post("/releases/{release_id}/approve")
    def approve(release_id: str, op: Operator = Depends(operator)) -> dict[str, Any]:
        return call(R().controller.approve, release_id, op.id, op.role)

    @r.post("/releases/{release_id}/pause")
    def pause(release_id: str, req: Reason, op: Operator = Depends(operator)) -> dict[str, Any]:
        need(op, "operator")
        return call(R().controller.pause, release_id, op.id, req.reason)

    @r.post("/releases/{release_id}/rollback")
    def rollback(release_id: str, req: Reason, op: Operator = Depends(operator)) -> dict[str, Any]:
        need(op, "operator")
        return call(R().controller.rollback, release_id, op.id, "manual", req.reason)

    @r.get("/events")
    def events(after: int = 0, limit: int = Query(500, le=5000), release_id: str | None = None) -> list[dict]:
        return R().store.events(after=after, limit=limit, release_id=release_id)

    @r.get("/requests/recent")
    def recent(limit: int = Query(50, le=500)) -> list[dict[str, Any]]:
        R().service.writer.flush()
        return R().store.recent_requests(limit)

    @r.get("/drift")
    def drift(last: int = Query(300, ge=20, le=20_000)) -> dict[str, Any]:
        rt = R()
        rt.service.writer.flush()
        rows = [x for x in rt.store.requests_since(0)[-last:] if x["status"] == "ok"]
        return input_drift(rows, rt.policy.canary.drift_psi_major)

    return r
