// Typed client for the gateway. Every number the UI renders comes from these endpoints, which read
// recorded requests, events and manifests. Nothing here generates or interpolates data.

// Same origin by default: the gateway serves the built UI on its own port (5120), and the Vite dev server (5121)
// proxies the API paths to it. VITE_API_URL overrides (the Docker web image sets it).
export const API = (import.meta.env.VITE_API_URL as string | undefined) ?? "";

export type Deployment = {
  champion_version: string | null;
  candidate_version: string | null;
  mode: string;
  canary_fraction: number;
  release_id: string | null;
  last_known_good: string | null;
  generation: number;
  updated_at: number;
};

export type Overview = {
  deployment: Deployment;
  health: { ready: boolean; problems: string[]; champion: string | null };
  models: number;
  releases: number;
  active_release: Release | null;
  latest_release: Release | null;
  event_chain: { ok: boolean; checked: number; head?: string; broken_at_seq?: number };
  db: string;
  policy_sha256: string;
};

export type Check = { name: string; passed: boolean; observed: unknown; limit: unknown; reason: string; skipped: boolean };

export type Release = {
  release_id: string;
  candidate_version: string;
  champion_version: string | null;
  state: string;
  reason: string | null;
  created_at: number;
  updated_at: number;
  created_by: string | null;
  windows_evaluated: number;
  healthy_windows: number;
  gate_report: { passed: boolean; blocking_reasons: string[]; checks: Check[] } | null;
  shadow_summary: ShadowSummary | null;
  last_evaluation: Evaluation | null;
  approvals: { actor: string; role: string; ts: number }[];
};

export type ShadowSummary = {
  requests: number;
  error_rate: number;
  score_js_divergence: number;
  decision_disagreement: number;
  champion_flag_rate: number | null;
  candidate_flag_rate: number | null;
  passed: boolean;
  reasons: string[];
};

export type Pctl = { p50: number | null; p95: number | null; p99: number | null; mean: number | null; n: number };

export type ArmStats = {
  requests: number;
  served_by_arm_model: number;
  model_failures: number;
  error_rate: number;
  flagged: number;
  flag_rate: number | null;
  flag_rate_ci95: [number, number];
  latency_ms: Pctl;
  flag_rate_ratio_vs_champion?: number;
};

export type Paired = {
  positives: number;
  candidate_caught: number;
  champion_would_have_caught: number;
  missed_by_candidate_only: number;
  missed_by_champion_only: number;
  candidate_recall: number | null;
  champion_recall: number | null;
  mcnemar_p_one_sided: number;
};

export type Evaluation = {
  window_index: number;
  verdict: "healthy" | "breach" | "insufficient_evidence";
  breaches: string[];
  insufficient: string[];
  healthy_windows: number;
  promotion_eligible: boolean;
  window_requests: number;
  arms: { candidate: ArmStats; champion: ArmStats };
  labeled?: { paired: Paired; candidate: { recall: number | null; recall_ci95: [number, number]; labeled_positives: number }; champion: { recall: number | null; recall_ci95: [number, number]; labeled_positives: number } };
  labeled_cumulative?: Evaluation["labeled"];
  drift_status?: string;
  drift_max_psi?: number;
  drift_features?: Record<string, number>;
};

export type EventRow = {
  seq: number;
  ts: number;
  run_id: string | null;
  kind: string;
  actor: string;
  simulated: boolean;
  release_id: string | null;
  payload: Record<string, any>; // eslint-disable-line @typescript-eslint/no-explicit-any
  hash: string;
  prev_hash: string;
};

export type SeriesPoint = {
  start_seq: number;
  end_seq: number;
  t0: number;
  t1: number;
  requests: number;
  schema_errors: number;
  errors: number;
  fallbacks: number;
  latency_ms: Pctl;
  scenarios: string[];
  simulated_share: number;
  arms: Record<"champion" | "candidate", { requests: number; flag_rate: number | null; model_failures: number; recall_labeled: number | null; labeled_positives: number; versions: string[] }>;
};

export type ModelSummary = {
  version: string;
  family: string;
  registered_at: number;
  artifact_sha256: string;
  manifest_sha256: string;
  signature: string;
  owner: string;
  deliberately_degraded: boolean;
  change_summary: string;
  dataset_hash: string;
  config_sha256: string;
  source: { commit: string | null; dirty: boolean | null };
  schema_version: string;
  threshold: number;
  test_metrics: { pr_auc: number; pr_auc_ci95: [number, number]; precision: number; recall: number; f1: number; brier: number; ece: number; roc_auc: number; n: number; positives: number };
  latency_single_row: Pctl;
  bytes: number;
};

export type ModelDetail = {
  version: string;
  manifest: Record<string, any>; // eslint-disable-line @typescript-eslint/no-explicit-any
  model_card_markdown: string | null;
  artifact_sha256: string;
  signature: string;
};

export type DriftReport = {
  n: number;
  status: string;
  max_psi?: number;
  caveat?: string | null;
  features: Record<string, { psi: number; status: string; reference_mean: number; live_mean: number }>;
  thresholds?: { moderate: number; major: number };
};

export type RunSummary = { run_id: string; kind: string; started_at: number; passed: boolean; summary: string };

async function get<T>(path: string): Promise<T> {
  const r = await fetch(`${API}${path}`);
  if (!r.ok) throw new Error(`${path}: HTTP ${r.status}`);
  return (await r.json()) as T;
}

export const api = {
  overview: () => get<Overview>("/v1/overview"),
  models: () => get<ModelSummary[]>("/v1/registry/models"),
  model: (v: string) => get<ModelDetail>(`/v1/registry/models/${encodeURIComponent(v)}`),
  releases: () => get<Release[]>("/v1/releases"),
  events: (after = 0) => get<EventRow[]>(`/v1/events?after=${after}&limit=5000`),
  series: (releaseId?: string, bucket = 250) =>
    get<{ series: SeriesPoint[]; total: number; bucket: number }>(
      `/v1/traffic/series?bucket=${bucket}${releaseId ? `&release_id=${encodeURIComponent(releaseId)}` : ""}`,
    ),
  drift: (releaseId?: string, last = 1500) =>
    get<DriftReport>(`/v1/drift?last=${last}${releaseId ? `&release_id=${encodeURIComponent(releaseId)}` : ""}`),
  runs: () => get<RunSummary[]>("/v1/runs"),
  run: (id: string) => get<Record<string, any>>(`/v1/runs/${encodeURIComponent(id)}`), // eslint-disable-line @typescript-eslint/no-explicit-any
  postmortem: async (runId: string, releaseId: string) => {
    const r = await fetch(`${API}/v1/runs/${encodeURIComponent(runId)}/postmortem?release_id=${encodeURIComponent(releaseId)}`);
    return r.ok ? r.text() : null;
  },
};

/** Subscribe to the server-sent event feed. Returns an unsubscribe function. */
export function subscribeEvents(after: number, onEvent: (e: EventRow) => void, onResync: () => void, onStatus: (s: string) => void): () => void {
  const es = new EventSource(`${API}/v1/events/stream?after=${after}`);
  es.onopen = () => onStatus("live");
  es.onerror = () => onStatus("reconnecting");
  es.addEventListener("resync", () => onResync());
  es.onmessage = (m) => onEvent(JSON.parse(m.data));
  // Named events (kind) arrive as typed events; listen generically via a wildcard shim.
  const handler = (m: MessageEvent) => {
    try {
      onEvent(JSON.parse(m.data));
    } catch {
      /* keepalive */
    }
  };
  for (const k of KNOWN_KINDS) es.addEventListener(k, handler as EventListener);
  return () => es.close();
}

export const KNOWN_KINDS = [
  "model.registered", "deployment.bootstrapped", "release.created", "release.gate_evaluated", "release.staged",
  "release.staging_failed", "release.shadow_started", "release.shadow_evaluated", "release.canary_started",
  "guardrail.evaluated", "drift.alert", "release.promoted", "promotion.verified", "promotion.verification_failed",
  "release.paused", "rollback.executed", "rollback.verified", "rollback.verification_failed", "rollback.duplicate_ignored",
  "postmortem.written", "release.stuck_reverted", "recovery.champion_reverted", "recovery.pointer_reconciled",
  "recovery.no_safe_champion", "chaos.fault_injected", "chaos.faults_cleared", "chaos.artifact_tampered",
];
