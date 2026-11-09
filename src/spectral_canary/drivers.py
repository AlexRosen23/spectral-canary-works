"""Two ways to drive the system with the same scenario code.

- ``InProcessDriver`` calls the runtime directly (deterministic; used by chaos replay and tests).
- ``HttpDriver`` sends real HTTP requests to a running gateway (used by ``make demo`` and the HTTP e2e test).
"""

from __future__ import annotations

from typing import Any

import httpx

from .controller import ReleaseError
from .runtime import Runtime
from .serving import Fault


class DriverError(RuntimeError):
    def __init__(self, status: int, detail: str):
        super().__init__(f"{status}: {detail}")
        self.status = status
        self.detail = detail


class InProcessDriver:
    kind = "in_process"

    def __init__(self, rt: Runtime, operators: dict[str, str] | None = None):
        self.rt = rt
        self.c = rt.controller

    def predict(self, payload: dict[str, Any]) -> tuple[int, dict[str, Any], dict[str, str]]:
        out = self.rt.service.predict(payload)
        return out.http_status, out.body, out.headers

    def feedback(self, fb: dict[str, Any]) -> None:
        self.rt.store.insert_feedback(fb["request_id"], fb["label"], fb.get("simulated", False))

    def _wrap(self, fn: Any, *a: Any) -> Any:
        try:
            return fn(*a)
        except ReleaseError as exc:
            raise DriverError(exc.status, str(exc)) from exc

    def propose(self, version: str, actor: str = "oncall-operator") -> dict[str, Any]:
        return self._wrap(self.c.propose, version, actor)

    def stage(self, rid: str) -> dict[str, Any]:
        return self._wrap(self.c.to_staging, rid, "oncall-operator")

    def shadow(self, rid: str) -> dict[str, Any]:
        return self._wrap(self.c.to_shadow, rid, "oncall-operator")

    def canary(self, rid: str) -> dict[str, Any]:
        return self._wrap(self.c.to_canary, rid, "oncall-operator")

    def approve(self, rid: str, actor: str = "release-manager", role: str = "release_manager") -> dict[str, Any]:
        return self._wrap(self.c.approve, rid, actor, role)

    def rollback(self, rid: str, reason: str = "manual rollback") -> dict[str, Any]:
        return self._wrap(self.c.rollback, rid, "oncall-operator", "manual", reason)

    def release(self, rid: str) -> dict[str, Any]:
        return self.rt.store.get_release(rid) or {}

    def deployment(self) -> dict[str, Any]:
        return self.rt.store.deployment().as_dict()

    def set_fault(self, version: str, latency_ms: float = 0.0, error_rate: float = 0.0, note: str = "") -> None:
        self.rt.faults.set(version, Fault(latency_ms, error_rate, 0, note))
        self.rt.store.append_event(
            "chaos.fault_injected",
            {"version": version, "latency_ms": latency_ms, "error_rate": error_rate, "note": note},
            actor="chaos-replay",
            simulated=True,
        )

    def clear_faults(self) -> None:
        self.rt.faults.clear()
        self.rt.store.append_event("chaos.faults_cleared", {}, actor="chaos-replay", simulated=True)

    def flush(self) -> None:
        self.rt.service.writer.flush()


class HttpDriver:
    kind = "http"

    def __init__(self, base_url: str, operators: dict[str, str], timeout: float = 30.0):
        self.http = httpx.Client(base_url=base_url, timeout=timeout)
        self.tokens = operators

    def _auth(self, actor: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.tokens[actor]}"}

    def _post(self, path: str, actor: str = "oncall-operator", json: Any = None) -> dict[str, Any]:
        r = self.http.post(path, headers=self._auth(actor), json=json or {})
        if r.status_code >= 400:
            raise DriverError(r.status_code, r.json().get("detail", r.text))
        return r.json()

    def predict(self, payload: dict[str, Any]) -> tuple[int, dict[str, Any], dict[str, str]]:
        r = self.http.post("/v1/predict", json=payload)
        return r.status_code, r.json(), dict(r.headers)

    def feedback(self, fb: dict[str, Any]) -> None:
        self.http.post("/v1/feedback", json=fb).raise_for_status()

    def propose(self, version: str, actor: str = "oncall-operator") -> dict[str, Any]:
        return self._post("/v1/releases", actor, {"candidate_version": version})

    def stage(self, rid: str) -> dict[str, Any]:
        return self._post(f"/v1/releases/{rid}/stage")

    def shadow(self, rid: str) -> dict[str, Any]:
        return self._post(f"/v1/releases/{rid}/shadow")

    def canary(self, rid: str) -> dict[str, Any]:
        return self._post(f"/v1/releases/{rid}/canary")

    def approve(self, rid: str, actor: str = "release-manager", role: str = "release_manager") -> dict[str, Any]:
        return self._post(f"/v1/releases/{rid}/approve", actor)

    def rollback(self, rid: str, reason: str = "manual rollback") -> dict[str, Any]:
        return self._post(f"/v1/releases/{rid}/rollback", json={"reason": reason})

    def release(self, rid: str) -> dict[str, Any]:
        return self.http.get(f"/v1/releases/{rid}").json()

    def deployment(self) -> dict[str, Any]:
        return self.http.get("/v1/deployment").json()

    def set_fault(self, version: str, latency_ms: float = 0.0, error_rate: float = 0.0, note: str = "") -> None:
        self._post(
            "/v1/chaos/faults",
            json={"version": version, "latency_ms": latency_ms, "error_rate": error_rate, "note": note},
        )

    def clear_faults(self) -> None:
        r = self.http.delete("/v1/chaos/faults", headers=self._auth("oncall-operator"))
        r.raise_for_status()

    def flush(self) -> None:
        pass
