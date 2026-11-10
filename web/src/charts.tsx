// Small, dependency-free SVG charts with a hover layer. Series identity: champion = slot 1 (blue),
// candidate = slot 2 (orange) — the validated adjacent pair from the reference palette. Lines are 2px,
// grid recessive, a legend is always present for >= 2 series, and every chart has a table fallback.
import { useMemo, useRef, useState } from "react";

export type Series = { key: string; label: string; color: string; values: (number | null)[]; dashed?: boolean };
export type Marker = { index: number; label: string; tone: "critical" | "good" | "info" | "warning" };

type LineProps = {
  title: string;
  series: Series[];
  xLabels: string[];
  yFormat: (v: number) => string;
  yMax?: number;
  height?: number;
  markers?: Marker[];
  cursor?: number | null;
  bands?: { from: number; to: number; label: string }[];
  caption?: string;
  empty?: string; // shown instead of the plot when there are no buckets
};

const W = 640;

export function LineChart({ title, series, xLabels, yFormat, yMax, height = 180, markers = [], cursor, bands = [], caption, empty = "No recorded traffic yet." }: LineProps) {
  const [hover, setHover] = useState<number | null>(null);
  const ref = useRef<SVGSVGElement>(null);
  const [showTable, setShowTable] = useState(false);
  const n = xLabels.length;
  const pad = { l: 52, r: 12, t: 10, b: 22 };
  const iw = W - pad.l - pad.r;
  const ih = height - pad.t - pad.b;
  const max = useMemo(() => {
    const vals = series.flatMap((s) => s.values.filter((v): v is number => v !== null));
    return yMax ?? Math.max(1e-9, ...vals) * 1.1;
  }, [series, yMax]);
  const x = (i: number) => pad.l + (n <= 1 ? iw / 2 : (i / (n - 1)) * iw);
  const y = (v: number) => pad.t + ih - (v / max) * ih;
  const ticks = [0, 0.25, 0.5, 0.75, 1].map((t) => t * max);

  const path = (vals: (number | null)[]) => {
    let d = "";
    let pen = false;
    vals.forEach((v, i) => {
      if (v === null) {
        pen = false;
        return;
      }
      d += `${pen ? "L" : "M"}${x(i).toFixed(1)},${y(v).toFixed(1)}`;
      pen = true;
    });
    return d;
  };

  const onMove = (ev: React.MouseEvent) => {
    const r = ref.current?.getBoundingClientRect();
    if (!r || n === 0) return;
    const px = ((ev.clientX - r.left) / r.width) * W;
    const i = Math.round(((px - pad.l) / iw) * (n - 1));
    setHover(Math.max(0, Math.min(n - 1, i)));
  };

  return (
    <figure className="chart">
      <figcaption>
        <span className="chart-title">{title}</span>
        <span className="legend">
          {series.map((s) => (
            <span key={s.key} className="legend-item">
              <svg width="18" height="8" aria-hidden>
                <line x1="0" y1="4" x2="18" y2="4" stroke={s.color} strokeWidth="2" strokeDasharray={s.dashed ? "4 3" : undefined} />
              </svg>
              {s.label}
            </span>
          ))}
          <button className="link" onClick={() => setShowTable((v) => !v)}>{showTable ? "chart" : "table"}</button>
        </span>
      </figcaption>
      {showTable ? (
        <div className="table-scroll">
          <table className="data">
            <thead>
              <tr><th>bucket</th>{series.map((s) => <th key={s.key}>{s.label}</th>)}</tr>
            </thead>
            <tbody>
              {xLabels.map((xl, i) => (
                <tr key={xl + i}><td>{xl}</td>{series.map((s) => <td key={s.key}>{s.values[i] === null ? "—" : yFormat(s.values[i] as number)}</td>)}</tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : n === 0 ? (
        <div className="empty">{empty}</div>
      ) : (
        <svg ref={ref} viewBox={`0 0 ${W} ${height}`} role="img" aria-label={title} onMouseMove={onMove} onMouseLeave={() => setHover(null)}>
          {bands.map((b) => (
            <g key={b.label + b.from}>
              <rect x={x(b.from)} y={pad.t} width={Math.max(2, x(b.to) - x(b.from))} height={ih} className="band" />
              <text x={x(b.from) + 4} y={pad.t + 11} className="band-label">{b.label}</text>
            </g>
          ))}
          {ticks.map((t) => (
            <g key={t}>
              <line x1={pad.l} x2={W - pad.r} y1={y(t)} y2={y(t)} className="grid" />
              <text x={pad.l - 6} y={y(t) + 3} className="tick" textAnchor="end">{yFormat(t)}</text>
            </g>
          ))}
          {series.map((s) => (
            <path key={s.key} d={path(s.values)} fill="none" stroke={s.color} strokeWidth={2} strokeDasharray={s.dashed ? "5 4" : undefined} strokeLinejoin="round" strokeLinecap="round" />
          ))}
          {markers.map((m) => (
            <g key={m.label + m.index}>
              <line x1={x(m.index)} x2={x(m.index)} y1={pad.t} y2={pad.t + ih} className={`marker marker-${m.tone}`} />
              <text x={x(m.index) > W * 0.85 ? x(m.index) - 3 : x(m.index) + 3} y={pad.t + ih - 4} textAnchor={x(m.index) > W * 0.85 ? "end" : "start"} className={`marker-label marker-${m.tone}`}>{m.label}</text>
            </g>
          ))}
          {cursor !== null && cursor !== undefined && cursor >= 0 && <line x1={x(cursor)} x2={x(cursor)} y1={pad.t} y2={pad.t + ih} className="cursor" />}
          {hover !== null && (
            <g>
              <line x1={x(hover)} x2={x(hover)} y1={pad.t} y2={pad.t + ih} className="crosshair" />
              {series.map((s) => s.values[hover] !== null && (
                <circle key={s.key} cx={x(hover)} cy={y(s.values[hover] as number)} r={4} fill={s.color} className="dot" />
              ))}
            </g>
          )}
          <text x={pad.l} y={height - 6} className="tick">{xLabels[0]}</text>
          <text x={W - pad.r} y={height - 6} className="tick" textAnchor="end">{xLabels[n - 1]}</text>
        </svg>
      )}
      {hover !== null && !showTable && (
        <div className="tooltip" role="status">
          <strong>{xLabels[hover]}</strong>
          {series.map((s) => (
            <span key={s.key}><i style={{ background: s.color }} />{s.label}: {s.values[hover] === null ? "—" : yFormat(s.values[hover] as number)}</span>
          ))}
        </div>
      )}
      {caption && <p className="caption">{caption}</p>}
    </figure>
  );
}

type BarProps = {
  title: string;
  rows: { label: string; value: number; status: string; detail?: string }[];
  thresholds: { value: number; label: string }[];
  format: (v: number) => string;
  caption?: string;
};

/** Horizontal bars (one magnitude per row, sequential single hue) with reference threshold lines. */
export function BarChart({ title, rows, thresholds, format, caption }: BarProps) {
  const [hover, setHover] = useState<number | null>(null);
  const max = Math.max(...rows.map((r) => r.value), ...thresholds.map((t) => t.value)) * 1.1 || 1;
  const lw = 150;
  const rowH = 22;
  const h = rows.length * rowH + 24;
  const bx = (v: number) => lw + (v / max) * (W - lw - 60);
  return (
    <figure className="chart">
      <figcaption><span className="chart-title">{title}</span></figcaption>
      {rows.length === 0 ? <div className="empty">No observed rows in this window.</div> : (
        <svg viewBox={`0 0 ${W} ${h}`} role="img" aria-label={title}>
          {rows.map((r, i) => (
            <g key={r.label} onMouseEnter={() => setHover(i)} onMouseLeave={() => setHover(null)}>
              <rect x={0} y={i * rowH} width={W} height={rowH} fill="transparent" />
              <text x={lw - 8} y={i * rowH + 15} className="tick" textAnchor="end">{r.label}</text>
              <rect x={lw} y={i * rowH + 5} width={Math.max(2, bx(r.value) - lw)} height={12} rx={4} className={`bar bar-${r.status}`} />
              <text x={bx(r.value) + 6} y={i * rowH + 15} className="value">{format(r.value)}{r.status !== "stable" ? ` · ${r.status}` : ""}</text>
            </g>
          ))}
          {thresholds.map((t, i) => (
            <g key={t.label}>
              <line x1={bx(t.value)} x2={bx(t.value)} y1={0} y2={rows.length * rowH} className="threshold" />
              <text x={bx(t.value) + (i % 2 ? 4 : -4)} y={rows.length * rowH + 16} className="tick" textAnchor={i % 2 ? "start" : "end"}>{t.label}</text>
            </g>
          ))}
        </svg>
      )}
      {hover !== null && rows[hover]?.detail && <div className="tooltip" role="status">{rows[hover].detail}</div>}
      {caption && <p className="caption">{caption}</p>}
    </figure>
  );
}

/** Interval plot: point estimate with 95% CI for two arms (identity by color + label, never color alone). */
export function IntervalPlot({ title, rows }: { title: string; rows: { label: string; color: string; value: number | null; ci: [number, number]; n: number }[] }) {
  const h = rows.length * 34 + 26;
  const lw = 130;
  const sx = (v: number) => lw + v * (W - lw - 30);
  return (
    <figure className="chart">
      <figcaption><span className="chart-title">{title}</span></figcaption>
      <svg viewBox={`0 0 ${W} ${h}`} role="img" aria-label={title}>
        {[0, 0.25, 0.5, 0.75, 1].map((t) => (
          <g key={t}>
            <line x1={sx(t)} x2={sx(t)} y1={0} y2={rows.length * 34} className="grid" />
            <text x={sx(t)} y={rows.length * 34 + 16} className="tick" textAnchor="middle">{Math.round(t * 100)}%</text>
          </g>
        ))}
        {rows.map((r, i) => (
          <g key={r.label}>
            <text x={lw - 8} y={i * 34 + 21} className="tick" textAnchor="end">{r.label} (n={r.n})</text>
            <line x1={sx(r.ci[0])} x2={sx(r.ci[1])} y1={i * 34 + 17} y2={i * 34 + 17} stroke={r.color} strokeWidth={2} />
            <line x1={sx(r.ci[0])} x2={sx(r.ci[0])} y1={i * 34 + 11} y2={i * 34 + 23} stroke={r.color} strokeWidth={2} />
            <line x1={sx(r.ci[1])} x2={sx(r.ci[1])} y1={i * 34 + 11} y2={i * 34 + 23} stroke={r.color} strokeWidth={2} />
            {r.value !== null && <circle cx={sx(r.value)} cy={i * 34 + 17} r={5} fill={r.color} className="dot" />}
          </g>
        ))}
      </svg>
    </figure>
  );
}
