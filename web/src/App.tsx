import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { API, api, subscribeEvents } from "./api";
import type { Check, DriftReport, EventRow, ModelDetail, ModelSummary, Overview, Release, RunSummary, SeriesPoint } from "./api";
import { BarChart, IntervalPlot, LineChart } from "./charts";
import LlmLane from "./LlmLane";
import type { Marker } from "./charts";
import { TRACK, clock, dateTime, describe, ms, num, pct, reachedSteps, short, stateTone, trackStates, verdictTone } from "./format";

const C_CHAMP = "var(--series-1)";
const C_CAND = "var(--series-2)";

type Load = { overview: Overview | null; models: ModelSummary[]; releases: Release[]; events: EventRow[]; runs: RunSummary[]; error: string | null };

export default function App() {
  const [d, setD] = useState<Load>({ overview: null, models: [], releases: [], events: [], runs: [], error: null });
  const [selected, setSelected] = useState<string | null>(null);
  const [feed, setFeed] = useState("connecting");
  const [drawer, setDrawer] = useState<string | null>(null);
  const lastSeq = useRef(0);

  const refresh = useCallback(async () => {
    try {
      const [overview, models, releases, events, runs] = await Promise.all([api.overview(), api.models(), api.releases(), api.events(0), api.runs()]);
      lastSeq.current = events.length ? events[events.length - 1].seq : 0;
      setD({ overview, models, releases, events, runs, error: null });
    } catch (e) {
      setD((x) => ({ ...x, error: String(e) }));
    }
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  // Live event feed (SSE). New events append; a debounce refetches derived state.
  useEffect(() => {
    let t: number | undefined;
    const unsub = subscribeEvents(lastSeq.current, (e) => {
      if (e.seq <= lastSeq.current) return;
      lastSeq.current = e.seq;
      setD((x) => ({ ...x, events: [...x.events, e].slice(-20000) }));
      window.clearTimeout(t);
      t = window.setTimeout(refresh, 800);
    }, refresh, setFeed);
    return () => {
      unsub();
      window.clearTimeout(t);
    };
  }, [refresh]);

  const releases = d.releases;
  const current = useMemo(() => {
    if (selected) return releases.find((r) => r.release_id === selected) ?? null;
    const rb = [...releases].reverse().find((r) => r.state === "rolled_back");
    return d.overview?.active_release ?? rb ?? releases[releases.length - 1] ?? null;
  }, [selected, releases, d.overview]);

  if (d.error && !d.overview) return <Offline error={d.error} retry={refresh} />;
  if (!d.overview) return <div className="boot">Connecting to the gateway…</div>;

  const ov = d.overview;
  const relEvents = current ? d.events.filter((e) => e.release_id === current.release_id) : [];
  // the served registry is the latest demo run (var/runs/latest); chaos and load-test runs have no postmortem
  const latestRun = [...d.runs].reverse().find((r) => r.kind === "demo");

  return (
    <div className="app">
      <header className="top">
        <div className="brand">
          <span className="logo" aria-hidden>◈</span>
          <div>
            <h1>Spectral Canary Works</h1>
            <p className="sub">ML release &amp; reliability control room · local lab</p>
          </div>
        </div>
        <div className="status-strip">
          <Badge tone="warning" icon="⚑" label="SIMULATED TRAFFIC" title="All request traffic is a seeded simulation. Predictions, routing, guardrails and rollbacks are real executions." />
          <Badge tone={ov.health.ready ? "good" : "critical"} icon={ov.health.ready ? "●" : "▲"} label={ov.health.ready ? "gateway ready" : "not ready"} title={ov.health.problems.join("; ")} />
          <Badge tone={ov.event_chain.ok ? "good" : "critical"} icon={ov.event_chain.ok ? "⛓" : "✕"} label={ov.event_chain.ok ? `audit chain ok · ${ov.event_chain.checked} events` : `chain broken @${ov.event_chain.broken_at_seq}`} />
          <Badge tone={feed === "live" ? "info" : "neutral"} icon="↻" label={`feed ${feed}`} />
          <a className="badge info" href="#llm-lane">LLM lane ↓</a>
        </div>
      </header>

      <section className="pointer card">
        <Stat label="Champion (serving)" value={ov.deployment.champion_version ?? "—"} mono />
        <Stat label="Candidate" value={ov.deployment.candidate_version ?? "none"} mono />
        <Stat label="Mode" value={ov.deployment.mode === "canary" ? `canary ${pct(ov.deployment.canary_fraction, 0)}` : ov.deployment.mode} />
        <Stat label="Last known good" value={ov.deployment.last_known_good ?? "—"} mono />
        <Stat label="Pointer generation" value={String(ov.deployment.generation)} mono />
        <Stat label="Registry" value={`${ov.db} · ${ov.models} models`} />
        {latestRun && <Stat label="Recorded run" value={`${latestRun.run_id} · ${latestRun.passed ? "passed" : "FAILED"}`} />}
      </section>

      {releases.length === 0 ? <Onboarding /> : (
        <>
          <nav className="release-tabs" aria-label="Releases">
            {releases.map((r) => (
              <button key={r.release_id} className={`tab ${current?.release_id === r.release_id ? "on" : ""}`} onClick={() => setSelected(r.release_id)}>
                <span className="mono">{r.release_id}</span> {r.candidate_version}
                <Pill tone={stateTone(r.state)} text={r.state.replace("_", " ")} />
              </button>
            ))}
          </nav>
          {current && <ReleaseView key={current.release_id} rel={current} events={relEvents} models={d.models} runId={latestRun?.run_id} onCard={setDrawer} />}
        </>
      )}

      <Registry models={d.models} champion={ov.deployment.champion_version} onOpen={setDrawer} />
      <LlmLane />
      {drawer && <ReproDrawer version={drawer} onClose={() => setDrawer(null)} />}
      <footer className="foot">
        Every number on this page is read from the gateway's recorded requests, hash-chained events and training manifests ·
        policy sha256 <span className="mono">{short(ov.policy_sha256, 10)}</span> · synthetic dataset; no real customers or payments.
      </footer>
    </div>
  );
}

function ReleaseView({ rel, events, models, runId, onCard }: { rel: Release; events: EventRow[]; models: ModelSummary[]; runId?: string; onCard: (v: string) => void }) {
  const [series, setSeries] = useState<SeriesPoint[]>([]);
  const [drift, setDrift] = useState<DriftReport | null>(null);
  const [cursor, setCursor] = useState<number>(events.length - 1);
  const [playing, setPlaying] = useState(false);
  const [postmortem, setPostmortem] = useState<string | null>(null);

  useEffect(() => {
    api.series(rel.release_id, 250).then((s) => setSeries(s.series)).catch(() => setSeries([]));
    api.drift(rel.release_id, 1500).then(setDrift).catch(() => setDrift(null));
    if (runId && rel.state === "rolled_back") api.postmortem(runId, rel.release_id).then(setPostmortem);
  }, [rel.release_id, rel.updated_at, rel.state, runId]);

  useEffect(() => setCursor(events.length - 1), [events.length]);
  useEffect(() => {
    if (!playing) return;
    const t = window.setInterval(() => setCursor((c) => {
      if (c >= events.length - 1) {
        setPlaying(false);
        return c;
      }
      return c + 1;
    }), 700);
    return () => window.clearInterval(t);
  }, [playing, events.length]);

  const reached = reachedSteps(events);
  const steps = trackStates(rel, reached);
  const evals = events.filter((e) => e.kind === "guardrail.evaluated");
  const cursorEvent = events[cursor];
  const bucketOf = (ts: number) => {
    if (!series.length || ts < series[0].t0) return -1;
    const i = series.findIndex((p) => ts <= p.t1 + 0.0005);
    return i === -1 ? series.length - 1 : i; // events right after the last recorded request
  };
  const markers: Marker[] = events.flatMap((e): Marker[] => {
    const i = bucketOf(e.ts);
    if (i < 0) return [];
    if (e.kind === "rollback.executed") return [{ index: i, label: "rollback", tone: "critical" as const }];
    if (e.kind === "release.canary_started") return [{ index: i, label: "canary start", tone: "info" as const }];
    if (e.kind === "release.promoted") return [{ index: i, label: "promoted", tone: "good" as const }];
    if (e.kind === "guardrail.evaluated" && e.payload.verdict === "breach") return [{ index: i, label: "breach", tone: "critical" as const }];
    return [];
  });
  const shiftStart = series.findIndex((p) => p.scenarios.includes("high_value_online_surge"));
  const bands = shiftStart >= 0 ? [{ from: shiftStart, to: series.length - 1, label: "SIMULATED shift" }] : [];
  const xl = series.map((p) => `req ${p.start_seq}–${p.end_seq}`);
  const cursorIdx = cursorEvent ? bucketOf(cursorEvent.ts) : null;
  const ev = (rel.last_evaluation ?? null);
  const breachEv = [...evals].reverse().find((e) => e.payload.verdict === "breach");
  const shown = cursorEvent && cursorEvent.kind === "guardrail.evaluated" ? cursorEvent.payload : breachEv?.payload ?? evals[evals.length - 1]?.payload;
  const cand = models.find((m) => m.version === rel.candidate_version);
  const champ = models.find((m) => m.version === rel.champion_version);

  return (
    <div className="release">
      <section className="card track-card">
        <div className="card-head">
          <h2>Release track <span className="mono dim">{rel.release_id}</span></h2>
          <span className="dim">{rel.candidate_version} vs champion {rel.champion_version ?? "—"} · opened by {rel.created_by}</span>
        </div>
        <ol className="track">
          {TRACK.map((s) => (
            <li key={s} className={`step step-${steps[s]}`}>
              <span className="dot-state" aria-hidden>{steps[s] === "done" ? "✓" : steps[s] === "failed" ? "✕" : steps[s] === "current" ? "◉" : "○"}</span>
              <span>{s}</span>
              <span className="sr">{steps[s]}</span>
            </li>
          ))}
        </ol>
        {rel.reason && (
          <div className={`reason ${stateTone(rel.state)}`}>
            <strong>{rel.state === "blocked" ? "Blocked" : rel.state === "rolled_back" ? "Rolled back" : rel.state === "paused" ? "Paused" : "Note"}:</strong> {rel.reason}
          </div>
        )}
        {rel.gate_report && <GateTable checks={rel.gate_report.checks} />}
      </section>

      <div className="grid2">
        <section className="card">
          <div className="card-head"><h2>Live traffic</h2><span className="dim">buckets of 250 recorded requests · this release</span></div>
          <LineChart title="Handler latency (ms)" xLabels={xl} yFormat={(v) => v.toFixed(1)} markers={markers} cursor={cursorIdx} bands={bands}
            series={[
              { key: "p50", label: "p50", color: "var(--series-1)", values: series.map((p) => p.latency_ms.p50) },
              { key: "p95", label: "p95", color: "var(--series-2)", values: series.map((p) => p.latency_ms.p95) },
              { key: "p99", label: "p99", color: "var(--series-7)", values: series.map((p) => p.latency_ms.p99), dashed: true },
            ]} caption="Measured end-to-end in the gateway (includes the paired champion score on canary requests)." />
          <LineChart title="Requests per bucket by arm" xLabels={xl} yFormat={(v) => v.toFixed(0)} markers={markers} cursor={cursorIdx} bands={bands}
            series={[
              { key: "champ", label: "champion arm", color: C_CHAMP, values: series.map((p) => p.arms.champion.requests) },
              { key: "cand", label: "candidate arm", color: C_CAND, values: series.map((p) => p.arms.candidate.requests) },
            ]} />
        </section>
        <section className="card">
          <div className="card-head"><h2>Quality monitor</h2><span className="dim">label-free first, delayed labels second</span></div>
          <LineChart title="Flag rate (share sent to review)" xLabels={xl} yFormat={(v) => pct(v, 0)} markers={markers} cursor={cursorIdx} bands={bands}
            series={[
              { key: "champ", label: "champion arm", color: C_CHAMP, values: series.map((p) => p.arms.champion.flag_rate) },
              { key: "cand", label: "candidate arm", color: C_CAND, values: series.map((p) => p.arms.candidate.flag_rate) },
            ]} />
          <LineChart title="Recall on labeled frauds (labels arrive late; ~20% never)" xLabels={xl} yFormat={(v) => pct(v, 0)} yMax={1} markers={markers} cursor={cursorIdx} bands={bands}
            series={[
              { key: "champ", label: "champion arm", color: C_CHAMP, values: series.map((p) => (p.arms.champion.labeled_positives >= 5 ? p.arms.champion.recall_labeled : null)) },
              { key: "cand", label: "candidate arm", color: C_CAND, values: series.map((p) => (p.arms.candidate.labeled_positives >= 5 ? p.arms.candidate.recall_labeled : null)) },
            ]} caption="Points with fewer than 5 labeled frauds in the bucket are hidden, not guessed. Guardrail decisions use cumulative paired evidence below." />
        </section>
      </div>

      <section className="card replay">
        <div className="card-head">
          <h2>Incident replay</h2>
          <div className="controls">
            <button onClick={() => setCursor(0)} aria-label="first event">⏮</button>
            <button onClick={() => setCursor((c) => Math.max(0, c - 1))} aria-label="previous event">◀</button>
            <button onClick={() => { if (cursor >= events.length - 1) setCursor(0); setPlaying((p) => !p); }} aria-label="play">{playing ? "❚❚" : "▶"}</button>
            <button onClick={() => setCursor((c) => Math.min(events.length - 1, c + 1))} aria-label="next event">▶</button>
            <input type="range" min={0} max={Math.max(0, events.length - 1)} value={cursor} onChange={(e) => setCursor(Number(e.target.value))} aria-label="timeline position" />
            <span className="mono dim">{cursor + 1}/{events.length}</span>
          </div>
        </div>
        <div className="replay-grid">
          <ol className="timeline">
            {events.map((e, i) => (
              <li key={e.seq} className={`tl ${i === cursor ? "on" : ""} ${i > cursor ? "future" : ""} k-${e.kind.split(".")[0]} ${e.kind === "rollback.executed" ? "critical" : ""}`} onClick={() => setCursor(i)}>
                <span className="mono dim">#{e.seq} {clock(e.ts)}</span>
                <span className="actor">{e.actor}</span>
                {e.simulated && <span className="sim">simulated</span>}
                <span className="desc">{describe(e)}</span>
              </li>
            ))}
          </ol>
          <div className="evidence">
            {shown ? <Evidence p={shown} /> : <div className="empty">No guardrail evaluation recorded for this release (it never reached canary).</div>}
            {cursorEvent && (
              <details className="raw"><summary>Raw event #{cursorEvent.seq} · hash {short(cursorEvent.hash, 14)}</summary><pre>{JSON.stringify(cursorEvent.payload, null, 2)}</pre></details>
            )}
          </div>
        </div>
      </section>

      <div className="grid2">
        <section className="card">
          <div className="card-head"><h2>Feature drift</h2><span className="dim">PSI vs training split · last 1500 requests of this release</span></div>
          {drift && drift.n > 0 ? (
            <BarChart title={`Population Stability Index (n=${drift.n}, overall ${drift.status})`} format={(v) => v.toFixed(3)}
              rows={Object.entries(drift.features).sort((a, b) => b[1].psi - a[1].psi).map(([k, v]) => ({ label: k, value: v.psi, status: v.status, detail: `${k}: training mean ${v.reference_mean} → live mean ${v.live_mean}` }))}
              thresholds={[{ value: drift.thresholds?.moderate ?? 0.1, label: "0.10 moderate" }, { value: drift.thresholds?.major ?? 0.25, label: "0.25 major" }]}
              caption={`Drift is an alert, never a rollback trigger: both arms see the same traffic.${drift.caveat ? ` Caveat: ${drift.caveat}.` : ""}`} />
          ) : <div className="empty">No traffic recorded for this release.</div>}
        </section>
        <section className="card">
          <div className="card-head"><h2>Champion vs candidate</h2><span className="dim">offline: frozen test split (synthetic)</span></div>
          <Compare a={champ} b={cand} onCard={onCard} />
          {rel.shadow_summary && (
            <div className="shadow-box">
              <h3>Shadow evidence <Pill tone={rel.shadow_summary.passed ? "good" : "critical"} text={rel.shadow_summary.passed ? "passed" : "failed"} /></h3>
              <p>{rel.shadow_summary.requests} identical requests scored by both; responses came from the champion only. Score JS divergence {num(rel.shadow_summary.score_js_divergence)}, decision disagreement {pct(rel.shadow_summary.decision_disagreement)}, flag rate {pct(rel.shadow_summary.champion_flag_rate)} vs {pct(rel.shadow_summary.candidate_flag_rate)}.</p>
            </div>
          )}
          {ev && <p className="dim small">Latest window {ev.window_index}: <Pill tone={verdictTone(ev.verdict)} text={ev.verdict} /> · healthy windows {ev.healthy_windows} · promotion eligible: {ev.promotion_eligible ? "yes (awaits release-manager approval)" : "no"}</p>}
        </section>
      </div>

      {postmortem && (
        <section className="card">
          <div className="card-head"><h2>Postmortem</h2><span className="dim">generated from the event log</span></div>
          <pre className="postmortem">{postmortem}</pre>
        </section>
      )}
    </div>
  );
}

// eslint-disable-next-line @typescript-eslint/no-explicit-any
function Evidence({ p }: { p: Record<string, any> }) {
  const c = p.arms.candidate;
  const h = p.arms.champion;
  const pr = p.labeled?.paired;
  return (
    <div>
      <h3>Window {p.window_index} <Pill tone={verdictTone(p.verdict)} text={p.verdict} /></h3>
      {p.breaches?.length > 0 && <ul className="breaches">{p.breaches.map((b: string) => <li key={b}>▲ {b}</li>)}</ul>}
      {p.verdict === "inconclusive_saturated" && p.saturation?.reason && <p className="small">◌ {p.saturation.reason}. Paused, not rolled back: the champion was slow in the same window, so the candidate's timeouts are not evidence against it.</p>}
      {p.insufficient?.length > 0 && <ul className="insufficient">{p.insufficient.map((b: string) => <li key={b}>◌ {b}</li>)}</ul>}
      <table className="data compact">
        <thead><tr><th>window signal</th><th><i className="sw" style={{ background: C_CAND }} />candidate</th><th><i className="sw" style={{ background: C_CHAMP }} />champion</th></tr></thead>
        <tbody>
          <tr><td>requests</td><td>{c.requests}</td><td>{h.requests}</td></tr>
          <tr><td>flag rate</td><td>{pct(c.flag_rate)}</td><td>{pct(h.flag_rate)}</td></tr>
          <tr><td>model errors</td><td>{c.model_failures} ({pct(c.error_rate)})</td><td>{h.model_failures}</td></tr>
          <tr><td>p95 latency</td><td>{ms(c.latency_ms.p95)}</td><td>{ms(h.latency_ms.p95)}</td></tr>
        </tbody>
      </table>
      {pr && pr.positives > 0 && (
        <>
          <IntervalPlot title="Recall on labeled frauds, cumulative (95% Wilson CI)" rows={[
            { label: "candidate", color: C_CAND, value: p.labeled.candidate.recall, ci: p.labeled.candidate.recall_ci95, n: p.labeled.candidate.labeled_positives },
            { label: "champion", color: C_CHAMP, value: p.labeled.champion.recall, ci: p.labeled.champion.recall_ci95, n: p.labeled.champion.labeled_positives },
          ]} />
          <p className="small">Paired on the {pr.positives} labeled frauds the candidate served: it caught <b>{pr.candidate_caught}</b>; the champion, scored on the same requests, would have caught <b>{pr.champion_would_have_caught}</b>. Exact one-sided McNemar p = {pr.mcnemar_p_one_sided.toPrecision(2)}.</p>
        </>
      )}
      {p.drift_status && <p className="small dim">Feature drift in window: {p.drift_status} (max PSI {num(p.drift_max_psi)}).</p>}
    </div>
  );
}

function GateTable({ checks }: { checks: Check[] }) {
  return (
    <details className="gate" open={checks.some((c) => !c.passed && !c.skipped)}>
      <summary>Offline gate: {checks.filter((c) => c.passed).length}/{checks.length} checks passed</summary>
      <table className="data compact">
        <thead><tr><th>check</th><th>result</th><th>observed</th><th>limit</th><th>reason</th></tr></thead>
        <tbody>
          {checks.map((c) => (
            <tr key={c.name} className={c.passed ? "" : c.skipped ? "" : "fail"}>
              <td className="mono">{c.name}</td>
              <td>{c.skipped ? <Pill tone="neutral" text="skipped" /> : c.passed ? <Pill tone="good" text="pass" /> : <Pill tone="critical" text="fail" />}</td>
              <td className="mono small">{typeof c.observed === "object" ? JSON.stringify(c.observed) : String(c.observed)}</td>
              <td className="mono small">{String(c.limit)}</td>
              <td className="small">{c.reason}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </details>
  );
}

function Compare({ a, b, onCard }: { a?: ModelSummary; b?: ModelSummary; onCard: (v: string) => void }) {
  if (!a || !b) return <div className="empty">Comparison needs both versions registered.</div>;
  // [label, display, raw value, higher is better]
  const rows: [string, (m: ModelSummary) => string, (m: ModelSummary) => number, boolean][] = [
    ["PR-AUC", (m) => `${num(m.test_metrics.pr_auc)} [${num(m.test_metrics.pr_auc_ci95[0], 2)}–${num(m.test_metrics.pr_auc_ci95[1], 2)}]`, (m) => m.test_metrics.pr_auc, true],
    ["precision @thr", (m) => num(m.test_metrics.precision), (m) => m.test_metrics.precision, true],
    ["recall @thr", (m) => num(m.test_metrics.recall), (m) => m.test_metrics.recall, true],
    ["F1", (m) => num(m.test_metrics.f1), (m) => m.test_metrics.f1, true],
    ["ECE", (m) => num(m.test_metrics.ece), (m) => m.test_metrics.ece, false],
  ];
  return (
    <table className="data compare">
      <thead><tr><th>metric</th><th><button className="link" onClick={() => onCard(a.version)}><i className="sw" style={{ background: C_CHAMP }} />{a.version}</button></th><th><button className="link" onClick={() => onCard(b.version)}><i className="sw" style={{ background: C_CAND }} />{b.version}</button></th><th>Δ</th></tr></thead>
      <tbody>
        {rows.map(([k, f, v, higherBetter]) => {
          const delta = v(b) - v(a);
          const worse = higherBetter ? delta < 0 : delta > 0;
          return <tr key={k}><td>{k}</td><td>{f(a)}</td><td>{f(b)}</td><td className={worse ? "neg" : "pos"}>{delta >= 0 ? "+" : ""}{delta.toFixed(3)}{worse ? " ▼" : ""}</td></tr>;
        })}
        <tr><td className="dim small" colSpan={4}>Δ is candidate − champion; ▼ marks the worse direction. Latency is compared live (gate + canary), not from training-time numbers, because absolute latency on this shared machine varies with load.</td></tr>
      </tbody>
    </table>
  );
}

function Registry({ models, champion, onOpen }: { models: ModelSummary[]; champion: string | null; onOpen: (v: string) => void }) {
  return (
    <section className="card">
      <div className="card-head"><h2>Model registry</h2><span className="dim">immutable versions · SHA-256 verified on every load · HMAC-signed manifests</span></div>
      <div className="table-scroll">
        <table className="data">
          <thead><tr><th>version</th><th>family</th><th>PR-AUC</th><th>F1</th><th>artifact sha256</th><th>signature</th><th>dataset</th><th>config</th><th>commit</th><th /></tr></thead>
          <tbody>
            {models.map((m) => (
              <tr key={m.version}>
                <td className="mono">{m.version} {m.version === champion && <Pill tone="good" text="champion" />} {m.deliberately_degraded && <Pill tone="warning" text="degraded (demo)" />}</td>
                <td>{m.family}</td>
                <td>{num(m.test_metrics.pr_auc)}</td>
                <td>{num(m.test_metrics.f1)}</td>
                <td className="mono small">{short(m.artifact_sha256, 16)}</td>
                <td className="mono small">{m.signature}</td>
                <td className="mono small">{short(m.dataset_hash, 10)}</td>
                <td className="mono small">{short(m.config_sha256, 10)}</td>
                <td className="mono small">{m.source.commit ? `${m.source.commit.slice(0, 8)}${m.source.dirty ? " (dirty)" : ""}` : "uncommitted"}</td>
                <td><button className="link" onClick={() => onOpen(m.version)}>card &amp; repro</button></td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}

function ReproDrawer({ version, onClose }: { version: string; onClose: () => void }) {
  const [m, setM] = useState<ModelDetail | null>(null);
  useEffect(() => {
    api.model(version).then(setM);
  }, [version]);
  useEffect(() => {
    const k = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", k);
    return () => window.removeEventListener("keydown", k);
  }, [onClose]);
  const man = m?.manifest;
  return (
    <div className="drawer-bg" onClick={onClose}>
      <aside className="drawer" role="dialog" aria-label={`Reproducibility for ${version}`} onClick={(e) => e.stopPropagation()}>
        <div className="card-head"><h2>Reproducibility · <span className="mono">{version}</span></h2><button className="link" onClick={onClose}>close ✕</button></div>
        {!man ? <div className="empty">Loading manifest…</div> : (
          <>
            <dl className="kv">
              <dt>Config</dt><dd className="mono">{man.training.config_path} · sha256 {short(man.training.config_sha256, 16)}</dd>
              <dt>Seed</dt><dd className="mono">{man.training.seed}</dd>
              <dt>Dataset</dt><dd className="mono">{man.dataset.name} · {short(man.dataset.dataset_hash, 16)} · generator {man.dataset.generator_version}</dd>
              <dt>Data change</dt><dd className="mono">{JSON.stringify(man.training.data_change)}</dd>
              <dt>Train rows used</dt><dd>{man.training.train_rows_used} ({man.training.train_positives_used} positives) · fit {man.training.fit_seconds}s</dd>
              <dt>Threshold</dt><dd>{man.threshold.value} — {man.threshold.rule} on {man.threshold.selected_on}</dd>
              <dt>Source commit</dt><dd className="mono">{man.source.commit ?? "not committed when trained"} {man.source.dirty ? "(dirty tree)" : ""}</dd>
              <dt>Environment</dt><dd className="mono">python {man.environment.python} · sklearn {man.environment.sklearn} · numpy {man.environment.numpy} · {man.environment.machine}</dd>
              <dt>Artifact</dt><dd className="mono">{man.artifact.format} · {man.artifact.bytes} bytes · {short(man.artifact.sha256, 24)}</dd>
              <dt>Test predictions</dt><dd className="mono">sha256 {short(man.test_prediction_sha256, 24)}</dd>
              <dt>Registered at</dt><dd>{m && dateTime((m as unknown as { created_at: number }).created_at)}</dd>
            </dl>
            <h3>Model card</h3>
            <pre className="postmortem">{m?.model_card_markdown}</pre>
          </>
        )}
      </aside>
    </div>
  );
}

function Onboarding() {
  return (
    <section className="card onboarding">
      <h2>No releases recorded yet</h2>
      <p>This control room only shows what the gateway has actually recorded. Create a recorded run:</p>
      <ol>
        <li><code>make setup &amp;&amp; make train-fixtures</code> — seeded dataset, four real model versions</li>
        <li><code>make demo</code> — promote v2, block v4, canary v3, inject a <b>simulated</b> shift, roll back</li>
        <li><code>make serve</code> and reload this page</li>
      </ol>
    </section>
  );
}

function Offline({ error, retry }: { error: string; retry: () => void }) {
  return (
    <div className="boot">
      <h1>Gateway unreachable</h1>
      <p>The UI needs the API at <code>{API || window.location.origin}</code>. Start it with <code>make serve</code>.</p>
      <p className="dim small">{error}</p>
      <button onClick={retry}>Retry</button>
    </div>
  );
}

function Badge({ tone, icon, label, title }: { tone: string; icon: string; label: string; title?: string }) {
  return <span className={`badge ${tone}`} title={title}><span aria-hidden>{icon}</span> {label}</span>;
}

function Pill({ tone, text }: { tone: string; text: string }) {
  return <span className={`pill ${tone}`}>{text}</span>;
}

function Stat({ label, value, mono }: { label: string; value: string; mono?: boolean }) {
  return <div className="stat"><span className="stat-label">{label}</span><span className={`stat-value ${mono ? "mono" : ""}`}>{value}</span></div>;
}
