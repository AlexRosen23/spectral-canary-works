import { useEffect, useState } from "react";
import { API } from "./api";
import { ms, num, pct, short, stateTone } from "./format";

// LLM triage lane: prompt bundles (prompt + model + params) released through the same controller, audit chain and
// rollback path as the ML lane. Everything here is read from /v1/llm/*, which reads the recorded run.

type LlmCheck = { name: string; passed: boolean; observed: unknown; limit: unknown; reason: string; blocking?: boolean };
type Rate = { rate: number | null; n: number; ci95: [number, number] };
type SuiteSummary = {
  cases: number; k: number; calls: number;
  schema_valid: Rate; reason_accuracy: Rate; correct_pass1: Rate; correct_pass_k: Rate;
  escalation_recall: Rate; false_escalation_rate: Rate;
  segments: Record<string, Rate & { cases: number }>;
  latency_ms: { p50: number | null; p95: number | null };
  tokens: { input_mean: number; output_mean: number };
  cost_usd_per_1k_calls: number;
};
type Arm = { calls: number; failures: number; latency_ms: { p95: number | null }; input_tokens_mean: number | null; output_tokens_mean: number | null; cost_usd_per_1k_calls: number | null; escalation_rate: number | null };
type LlmRelease = {
  release_id: string; candidate_version: string; champion_version: string | null; state: string; reason: string | null;
  windows_evaluated: number; healthy_windows: number; updated_at: number;
  gate_report: { passed: boolean; blocking_reasons: string[]; checks: LlmCheck[]; candidate_summary: SuiteSummary | null; champion_summary: SuiteSummary | null } | null;
  shadow_summary: { requests: number; error_rate: number; escalate_agreement: number | null; passed: boolean } | null;
  last_evaluation: { window_index: number; verdict: string; breaches: string[]; arms: { candidate: Arm; champion: Arm }; drift?: { status: string; max_psi: number | null } } | null;
};
type Bundle = { version: string; prompt_version: string; model: string; model_digest: string; params: Record<string, number>; bundle_sha256: string; change_summary: string; deliberately_regressed: boolean };
type LlmOverview = {
  deployment: { champion_version: string | null; candidate_version: string | null; mode: string; generation: number; last_known_good: string | null };
  bundles: number; releases: number; event_chain: { ok: boolean; checked: number }; db: string; policy_sha256: string; gold_sha256: string;
};

async function get<T>(path: string): Promise<T> {
  const r = await fetch(`${API}${path}`);
  if (!r.ok) throw new Error(String(r.status));
  return (await r.json()) as T;
}

export default function LlmLane() {
  const [ov, setOv] = useState<LlmOverview | null>(null);
  const [rels, setRels] = useState<LlmRelease[]>([]);
  const [bundles, setBundles] = useState<Bundle[]>([]);
  const [open, setOpen] = useState<string | null>(null);
  const [missing, setMissing] = useState(false);
  const [pm, setPm] = useState<string | null>(null);

  useEffect(() => {
    Promise.all([get<LlmOverview>("/v1/llm/overview"), get<LlmRelease[]>("/v1/llm/releases"), get<Bundle[]>("/v1/llm/registry")])
      .then(([o, r, b]) => {
        setOv(o);
        setRels(r);
        setBundles(b);
        const rb = [...r].reverse().find((x) => x.state === "rolled_back");
        setOpen(rb?.release_id ?? r[r.length - 1]?.release_id ?? null);
      })
      .catch(() => setMissing(true));
  }, []);

  const runId = ov?.db.split("/")[0];
  const cur = rels.find((r) => r.release_id === open) ?? null;
  useEffect(() => {
    setPm(null);
    if (runId && cur?.state === "rolled_back") {
      fetch(`${API}/v1/runs/${encodeURIComponent(runId)}/postmortem?release_id=${encodeURIComponent(cur.release_id)}`)
        .then((r) => (r.ok ? r.text() : null)).then(setPm).catch(() => setPm(null));
    }
  }, [runId, cur?.release_id, cur?.state]);

  if (missing) {
    return (
      <section className="card" id="llm-lane">
        <h2>LLM triage lane</h2>
        <p className="dim">No LLM lane recorded on this gateway yet. Run <code>make llm-demo</code> (needs a local Ollama with llama3.2:3b and qwen2.5-coder:3b), then reload.</p>
      </section>
    );
  }
  if (!ov) return null;

  return (
    <section className="card llm" id="llm-lane">
      <div className="card-head">
        <h2>LLM triage lane <span className="dim small">fraud-alert triage · releasable unit = prompt + model + params</span></h2>
        <span className="small dim">
          champion <b className="mono">{ov.deployment.champion_version ?? "—"}</b> · mode {ov.deployment.mode} · pointer gen {ov.deployment.generation} ·{" "}
          <span className={ov.event_chain.ok ? "good" : "critical"}>{ov.event_chain.ok ? `audit chain ok · ${ov.event_chain.checked} events` : "audit chain BROKEN"}</span>
        </span>
      </div>
      <p className="small dim">
        Alerts are <b>simulated</b> (seeded generator, rule-derived labels); every model call is a real local Ollama call, graded by code.
        Gold set sha256 <span className="mono">{short(ov.gold_sha256, 10)}</span> · LLM policy sha256 <span className="mono">{short(ov.policy_sha256, 10)}</span> · run <span className="mono">{runId}</span>
      </p>

      <table className="data compact llm-releases">
        <thead>
          <tr><th>release</th><th>candidate</th><th>state</th><th>pass@1</th><th>pass^3</th><th>escalation recall</th><th>takeover segment</th><th>p95</th><th>$ / 1k calls</th><th>why</th></tr>
        </thead>
        <tbody>
          {rels.map((r) => {
            const s = r.gate_report?.candidate_summary;
            const why = r.state === "blocked" ? r.gate_report?.blocking_reasons[0] : r.reason;
            return (
              <tr key={r.release_id} className={open === r.release_id ? "on" : ""} onClick={() => setOpen(r.release_id)} style={{ cursor: "pointer" }}>
                <td className="mono">{r.release_id}</td>
                <td className="mono">{r.candidate_version}</td>
                <td><span className={`pill ${stateTone(r.state)}`}>{r.state.replace("_", " ")}</span></td>
                <td>{s ? pct(s.correct_pass1.rate) : "—"}</td>
                <td>{s ? pct(s.correct_pass_k.rate) : "—"}</td>
                <td>{s ? pct(s.escalation_recall.rate) : "—"}</td>
                <td>{s ? pct(s.segments.account_takeover?.rate) : "—"}</td>
                <td>{s ? ms(s.latency_ms.p95) : "—"}</td>
                <td>{s ? num(s.cost_usd_per_1k_calls, 4) : "—"}</td>
                <td className="small">{why ? (why.length > 110 ? `${why.slice(0, 110)}…` : why) : ""}</td>
              </tr>
            );
          })}
        </tbody>
      </table>

      {cur && (
        <div className="grid2">
          <div>
            <h3 className="mono">{cur.release_id} · {cur.candidate_version} vs {cur.champion_version}</h3>
            {cur.gate_report && (
              <table className="data compact">
                <thead><tr><th>offline check</th><th>result</th><th>observed</th><th>limit</th></tr></thead>
                <tbody>
                  {cur.gate_report.checks.map((c) => (
                    <tr key={c.name} className={c.passed || c.blocking === false ? "" : "fail"} title={c.reason}>
                      <td className="mono small">{c.name}</td>
                      <td>{c.passed ? <span className="pill good">pass</span> : c.blocking === false ? <span className="pill warning">advisory</span> : <span className="pill critical">fail</span>}</td>
                      <td className="mono small">{String(c.observed)}</td>
                      <td className="mono small">{String(c.limit ?? "—")}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
          <div>
            {cur.shadow_summary && (
              <p className="small">Shadow: {cur.shadow_summary.requests} requests, candidate failure rate {pct(cur.shadow_summary.error_rate)}, escalate agreement {pct(cur.shadow_summary.escalate_agreement)} · {cur.shadow_summary.passed ? "passed" : "refused"}</p>
            )}
            {cur.last_evaluation && (
              <>
                <h3>Canary window {cur.last_evaluation.window_index} <span className={`pill ${cur.last_evaluation.verdict === "breach" ? "critical" : cur.last_evaluation.verdict === "healthy" ? "good" : "neutral"}`}>{cur.last_evaluation.verdict}</span></h3>
                {cur.last_evaluation.breaches.length > 0 && <ul className="breaches">{cur.last_evaluation.breaches.map((b) => <li key={b}>▲ {b}</li>)}</ul>}
                <table className="data compact">
                  <thead><tr><th>window signal</th><th>candidate</th><th>champion arm</th></tr></thead>
                  <tbody>
                    {(["calls", "failures"] as const).map((k) => <tr key={k}><td>{k}</td><td>{cur.last_evaluation!.arms.candidate[k]}</td><td>{cur.last_evaluation!.arms.champion[k]}</td></tr>)}
                    <tr><td>model p95</td><td>{ms(cur.last_evaluation.arms.candidate.latency_ms.p95)}</td><td>{ms(cur.last_evaluation.arms.champion.latency_ms.p95)}</td></tr>
                    <tr><td>output tokens (mean)</td><td>{num(cur.last_evaluation.arms.candidate.output_tokens_mean, 1)}</td><td>{num(cur.last_evaluation.arms.champion.output_tokens_mean, 1)}</td></tr>
                    <tr><td>$ / 1k calls (notional)</td><td>{num(cur.last_evaluation.arms.candidate.cost_usd_per_1k_calls, 4)}</td><td>{num(cur.last_evaluation.arms.champion.cost_usd_per_1k_calls, 4)}</td></tr>
                    <tr><td>escalation rate</td><td>{pct(cur.last_evaluation.arms.candidate.escalation_rate)}</td><td>{pct(cur.last_evaluation.arms.champion.escalation_rate)}</td></tr>
                  </tbody>
                </table>
                {cur.last_evaluation.drift && <p className="small dim">Input drift in window: {cur.last_evaluation.drift.status} (max PSI {num(cur.last_evaluation.drift.max_psi)}).</p>}
              </>
            )}
            {pm && (
              <details className="gate">
                <summary>Generated postmortem ({cur.release_id})</summary>
                <pre className="postmortem small">{pm}</pre>
              </details>
            )}
          </div>
        </div>
      )}

      <details className="gate">
        <summary>Bundle registry: {bundles.length} immutable bundles (sha256 + HMAC, weights digest pinned)</summary>
        <table className="data compact">
          <thead><tr><th>bundle</th><th>prompt</th><th>model</th><th>temperature</th><th>bundle sha256</th><th>change</th></tr></thead>
          <tbody>
            {bundles.map((b) => (
              <tr key={b.version}>
                <td className="mono">{b.version}{b.deliberately_regressed ? " ⚠" : ""}</td>
                <td className="mono">{b.prompt_version}</td>
                <td className="mono">{b.model}</td>
                <td>{String(b.params.temperature)}</td>
                <td className="mono small">{short(b.bundle_sha256, 12)}</td>
                <td className="small">{b.change_summary}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </details>
    </section>
  );
}
