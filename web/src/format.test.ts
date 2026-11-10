import { describe as d, expect, it } from "vitest";
import type { EventRow } from "./api";
import { describe, pct, reachedSteps, trackStates } from "./format";

const ev = (kind: string, payload: Record<string, unknown> = {}): EventRow => ({
  seq: 1, ts: 0, run_id: null, kind, actor: "t", simulated: false, release_id: "rel-0001", payload, hash: "", prev_hash: "",
});

d("release track", () => {
  it("marks a rolled-back canary as failed at promotion", () => {
    const reached = reachedSteps([ev("release.created"), ev("release.gate_evaluated", { passed: true }), ev("release.staged"),
      ev("release.shadow_started"), ev("release.canary_started")]);
    const s = trackStates({ state: "rolled_back" }, reached);
    expect(s.canary).toBe("done");
    expect(s.promoted).toBe("failed");
  });
  it("marks a blocked release as failed at validation", () => {
    const s = trackStates({ state: "blocked" }, reachedSteps([ev("release.created"), ev("release.gate_evaluated", { passed: false })]));
    expect(s.registered).toBe("done");
    expect(s.validated).toBe("failed");
    expect(s.canary).toBe("pending");
  });
  it("keeps a paused canary current, not done", () => {
    const s = trackStates({ state: "paused" }, reachedSteps([ev("release.created"), ev("release.gate_evaluated", { passed: true }),
      ev("release.staged"), ev("release.shadow_started"), ev("release.canary_started")]));
    expect(s.canary).toBe("current");
    expect(s.promoted).toBe("pending");
  });
  it("marks the current step", () => {
    const s = trackStates({ state: "canary" }, reachedSteps([ev("release.created"), ev("release.gate_evaluated", { passed: true }),
      ev("release.staged"), ev("release.shadow_started"), ev("release.canary_started")]));
    expect(s.canary).toBe("current");
    expect(s.shadow).toBe("done");
  });
});

d("formatting", () => {
  it("never invents a value for missing data", () => {
    expect(pct(null)).toBe("—");
    expect(pct(0.1234)).toBe("12.3%");
  });
  it("describes a saturation pause", () => {
    expect(describe(ev("release.paused", { reason: "inconclusive: gateway saturated" }))).toContain("gateway saturated");
  });
  it("describes a rollback event", () => {
    expect(describe(ev("rollback.executed", { trigger: "guardrail_breach", serving_after: "v2-hgb" }))).toContain("v2-hgb");
  });
});
