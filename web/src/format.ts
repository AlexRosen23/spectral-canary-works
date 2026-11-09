// Pure formatting/derivation helpers (unit-tested in format.test.ts).
import type { EventRow, Release } from "./api";

export const TRACK = ["registered", "validated", "staging", "shadow", "canary", "promoted"] as const;

export type StepState = "done" | "current" | "pending" | "failed" | "skipped";

/** Map a release's state to per-step states on the release track. */
export function trackStates(rel: Pick<Release, "state">, reached: Set<string>): Record<string, StepState> {
  const out: Record<string, StepState> = {};
  const terminalFail = ["blocked", "rolled_back", "reverted_stuck", "cancelled"].includes(rel.state);
  let lastReached = -1;
  TRACK.forEach((s, i) => {
    if (reached.has(s)) lastReached = i;
  });
  TRACK.forEach((s, i) => {
    if (rel.state === s) out[s] = s === "promoted" ? "done" : "current";
    else if (i < lastReached || (i === lastReached && rel.state !== s)) out[s] = "done";
    else out[s] = "pending";
  });
  if (terminalFail) {
    const failAt = rel.state === "blocked" ? "validated" : TRACK[Math.min(lastReached + 1, TRACK.length - 1)];
    out[failAt] = "failed";
  }
  return out;
}

/** Which track steps a release reached, derived from its recorded events. */
export function reachedSteps(events: EventRow[]): Set<string> {
  const r = new Set<string>();
  for (const e of events) {
    if (e.kind === "release.created") r.add("registered");
    if (e.kind === "release.gate_evaluated" && e.payload.passed) r.add("validated");
    if (e.kind === "release.staged") r.add("staging");
    if (e.kind === "release.shadow_started") r.add("shadow");
    if (e.kind === "release.canary_started") r.add("canary");
    if (e.kind === "release.promoted") r.add("promoted");
  }
  return r;
}

export const pct = (v: number | null | undefined, digits = 1) =>
  v === null || v === undefined || Number.isNaN(v) ? "—" : `${(v * 100).toFixed(digits)}%`;

export const num = (v: number | null | undefined, digits = 3) =>
  v === null || v === undefined || Number.isNaN(v) ? "—" : v.toFixed(digits);

export const ms = (v: number | null | undefined) =>
  v === null || v === undefined ? "—" : v < 10 ? `${v.toFixed(2)} ms` : `${v.toFixed(1)} ms`;

export const short = (h: string | null | undefined, n = 12) => (h ? `${h.slice(0, n)}…` : "—");

export const clock = (ts: number) =>
  new Date(ts * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });

export const dateTime = (ts: number) => new Date(ts * 1000).toLocaleString();

export type Tone = "good" | "warning" | "critical" | "neutral" | "info";

export function stateTone(state: string): Tone {
  if (state === "promoted") return "good";
  if (["blocked", "rolled_back", "reverted_stuck"].includes(state)) return "critical";
  if (state === "paused") return "warning";
  if (["canary", "shadow", "staging"].includes(state)) return "info";
  return "neutral";
}

export function verdictTone(v: string | undefined): Tone {
  if (v === "healthy") return "good";
  if (v === "breach") return "critical";
  if (v === "insufficient_evidence") return "warning";
  return "neutral";
}

/** Human summary for an event row in the incident timeline. */
export function describe(e: EventRow): string {
  const p = e.payload;
  switch (e.kind) {
    case "model.registered":
      return `registered ${p.version} (sha256 ${short(p.artifact_sha256, 10)})`;
    case "deployment.bootstrapped":
      return `bootstrapped champion ${p.champion}`;
    case "release.created":
      return `release opened: ${p.candidate} vs ${p.champion}`;
    case "release.gate_evaluated":
      return p.passed ? "offline gate passed" : `offline gate BLOCKED — ${(p.blocking_reasons as string[]).join("; ")}`;
    case "release.staged":
      return `candidate ${p.loaded} loaded & smoke-tested`;
    case "release.shadow_started":
      return `shadow: ${p.candidate} scores mirrored traffic, champion answers`;
    case "release.shadow_evaluated":
      return `shadow evidence: ${p.requests} req, JS ${num(p.score_js_divergence)}, disagreement ${pct(p.decision_disagreement)}`;
    case "release.canary_started":
      return `canary ${pct(p.fraction, 0)} of request keys → ${p.candidate}`;
    case "guardrail.evaluated":
      return `window ${p.window_index}: ${p.verdict}${p.breaches?.length ? ` — ${p.breaches[0]}` : ""}`;
    case "drift.alert":
      return `feature drift: max PSI ${num(p.max_psi)} (alert only)`;
    case "release.promoted":
      return `promoted ${p.new_champion} (approved by ${p.approved_by})`;
    case "rollback.executed":
      return `ROLLBACK (${p.trigger}): traffic → ${p.serving_after}`;
    case "rollback.verified":
    case "promotion.verified":
      return `verified: ${p.served_by_expected}/${p.requests} real predictions served by ${p.expected}`;
    case "rollback.duplicate_ignored":
      return "duplicate rollback request ignored (idempotent)";
    case "postmortem.written":
      return "postmortem generated from event log";
    case "chaos.fault_injected":
      return `SIMULATED fault on ${p.version}: ${p.note || ""}`;
    default:
      return e.kind;
  }
}
