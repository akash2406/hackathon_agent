/**
 * Small, dependency-free SVG charts for grounded results.
 *
 * Mark specs follow the data-viz method used across CRIP: columns <= 24px with a
 * 4px rounded data-end and square baseline, 2px surface gap between adjacent
 * columns, hairline solid gridlines, text in ink tokens (never the series
 * colour), selective direct labels, a hover tooltip on every mark, and a data
 * table behind every chart so nothing is colour- or hover-only.
 */
import { useLayoutEffect, useRef, useState, type ReactNode } from "react";

export function formatMoney(value: number, currency: string): string {
  const code = /^[A-Z]{3}$/.test(currency) ? currency : undefined;
  try {
    return new Intl.NumberFormat(undefined, {
      style: code ? "currency" : "decimal",
      currency: code,
      maximumFractionDigits: Math.abs(value) >= 100 ? 0 : 2,
    }).format(value) + (code ? "" : ` ${currency}`);
  } catch {
    return `${value.toFixed(2)} ${currency}`;
  }
}

export function compactMoney(value: number, currency: string): string {
  const code = /^[A-Z]{3}$/.test(currency) ? currency : undefined;
  try {
    return new Intl.NumberFormat(undefined, {
      style: code ? "currency" : "decimal",
      currency: code,
      notation: "compact",
      maximumFractionDigits: 1,
    }).format(value);
  } catch {
    return value.toFixed(0);
  }
}

function useWidth<T extends HTMLElement>(): [React.RefObject<T | null>, number] {
  const ref = useRef<T>(null);
  const [width, setWidth] = useState(600);
  // Measure synchronously before paint (no flash at the default width), then track resizes.
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    setWidth(Math.max(240, el.getBoundingClientRect().width));
    const observer = new ResizeObserver(([entry]) => setWidth(Math.max(240, entry.contentRect.width)));
    observer.observe(el);
    return () => observer.disconnect();
  }, []);
  return [ref, width];
}

function niceTicks(max: number, count = 4): number[] {
  if (max <= 0) return [0];
  const raw = max / count;
  const magnitude = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * magnitude).find((s) => s >= raw) ?? raw;
  const ticks: number[] = [];
  for (let v = 0; v <= max + step * 0.001; v += step) ticks.push(v);
  if (ticks[ticks.length - 1] < max) ticks.push(ticks[ticks.length - 1] + step);
  return ticks;
}

// Column with 4px rounded data-end and a square baseline.
function columnPath(x: number, y: number, w: number, h: number): string {
  const r = Math.min(4, w / 2, h);
  return `M${x},${y + h}V${y + r}Q${x},${y} ${x + r},${y}H${x + w - r}Q${x + w},${y} ${x + w},${y + r}V${y + h}Z`;
}

export type ColumnTone = "base" | "forecast" | "spike";

export interface ColumnDatum {
  label: string; // ISO date
  value: number;
  tone: ColumnTone;
  note?: string;
}

const TONE_LABEL: Record<ColumnTone, string> = { base: "Actual", forecast: "Azure forecast", spike: "Spike" };

/** Daily columns: actual / forecast / anomalous days, with an optional "typical day" reference line. */
export function ColumnChart({
  data,
  currency,
  reference,
  ariaLabel,
}: {
  data: ColumnDatum[];
  currency: string;
  reference?: { value: number; label: string };
  ariaLabel: string;
}) {
  const [ref, width] = useWidth<HTMLDivElement>();
  const [hover, setHover] = useState<number | null>(null);
  if (data.length === 0) return null;

  const height = 180;
  const m = { top: 16, right: 8, bottom: 22, left: 52 };
  const plotW = width - m.left - m.right;
  const plotH = height - m.top - m.bottom;
  const max = Math.max(...data.map((d) => d.value), reference?.value ?? 0);
  const ticks = niceTicks(max);
  const top = ticks[ticks.length - 1] || 1;
  const y = (v: number) => m.top + plotH - (v / top) * plotH;
  const band = plotW / data.length;
  const colW = Math.min(24, Math.max(2, band - 2)); // <= 24px, 2px surface gap between neighbours
  const x = (i: number) => m.left + i * band + (band - colW) / 2;
  const tones = Array.from(new Set(data.map((d) => d.tone)));
  const labelIdx = new Set([0, Math.floor((data.length - 1) / 2), data.length - 1]);
  const hovered = hover === null ? null : data[hover];

  return (
    <figure className="chart" ref={ref}>
      {tones.length > 1 && (
        <div className="legend" aria-hidden="true">
          {tones.map((t) => (
            <span key={t} className="legend-item">
              <span className={`swatch tone-${t}`} />
              {TONE_LABEL[t]}
            </span>
          ))}
          {reference && (
            <span className="legend-item">
              <span className="swatch reference" />
              {reference.label} ({formatMoney(reference.value, currency)})
            </span>
          )}
        </div>
      )}
      <svg width={width} height={height} role="img" aria-label={ariaLabel} onMouseLeave={() => setHover(null)}>
        {ticks.map((t) => (
          <g key={t}>
            <line className="gridline" x1={m.left} x2={width - m.right} y1={y(t)} y2={y(t)} />
            <text className="tick" x={m.left - 6} y={y(t)} dy="0.32em" textAnchor="end">
              {compactMoney(t, currency)}
            </text>
          </g>
        ))}
        {data.map((d, i) => (
          <g key={d.label + i}>
            <path className={`mark tone-${d.tone}${hover === i ? " hovered" : ""}`} d={columnPath(x(i), y(d.value), colW, Math.max(0, y(0) - y(d.value)))} />
            {d.tone === "spike" && (
              <text className="mark-label" x={x(i) + colW / 2} y={y(d.value) - 5} textAnchor="middle">
                ▲ spike
              </text>
            )}
            {/* Hit target: the whole band, taller than the mark. */}
            <rect x={m.left + i * band} y={m.top} width={band} height={plotH} fill="transparent" onMouseEnter={() => setHover(i)} />
            {labelIdx.has(i) && (
              <text className="tick" x={x(i) + colW / 2} y={height - 6} textAnchor="middle">
                {d.label.slice(5)}
              </text>
            )}
          </g>
        ))}
        {/* Reference line only; its value lives in the legend and stat tile so no label collides with columns. */}
        {reference && <line className="reference-line" x1={m.left} x2={width - m.right} y1={y(reference.value)} y2={y(reference.value)} />}
        <line className="baseline" x1={m.left} x2={width - m.right} y1={y(0)} y2={y(0)} />
      </svg>
      {hovered && hover !== null && (
        <div className="tooltip" style={{ left: Math.min(width - 180, Math.max(0, x(hover) - 60)) }}>
          <strong>{hovered.label}</strong>
          <div>
            {TONE_LABEL[hovered.tone]}: {formatMoney(hovered.value, currency)}
          </div>
          {hovered.note && <div className="muted">{hovered.note}</div>}
        </div>
      )}
      <DataTable
        caption={ariaLabel}
        columns={["Date", "Kind", `Cost (${currency})`]}
        rows={data.map((d) => [d.label, TONE_LABEL[d.tone] + (d.note ? ` (${d.note})` : ""), d.value.toFixed(2)])}
      />
    </figure>
  );
}

export interface BarDatum {
  label: string;
  value: number;
  display: string; // formatted value shown at the bar tip
}

/** Horizontal bars, single series: ranked magnitudes (top groups, savings, counts). */
export function BarList({ data, ariaLabel, valueHeader }: { data: BarDatum[]; ariaLabel: string; valueHeader: string }) {
  const [hover, setHover] = useState<number | null>(null);
  if (data.length === 0) return null;
  const max = Math.max(...data.map((d) => d.value), 0) || 1;
  return (
    <figure className="chart">
      <div className="barlist" role="img" aria-label={ariaLabel}>
        {data.map((d, i) => (
          <div
            key={d.label + i}
            className={`barrow${hover === i ? " hovered" : ""}`}
            onMouseEnter={() => setHover(i)}
            onMouseLeave={() => setHover(null)}
            title={`${d.label}: ${d.display}`}
          >
            <span className="barlabel">{d.label}</span>
            <span className="bartrack">
              <span className="bar" style={{ width: `${Math.max(1, (d.value / max) * 100)}%` }} />
              <span className="barvalue">{d.display}</span>
            </span>
          </div>
        ))}
      </div>
      <DataTable caption={ariaLabel} columns={["Item", valueHeader]} rows={data.map((d) => [d.label, d.display])} />
    </figure>
  );
}

export function StatTile({ label, value, sub, hero }: { label: string; value: string; sub?: ReactNode; hero?: boolean }) {
  return (
    <div className={`stat${hero ? " hero" : ""}`}>
      <div className="stat-label">{label}</div>
      <div className="stat-value">{value}</div>
      {sub && <div className="stat-sub">{sub}</div>}
    </div>
  );
}

export function Meter({ label, pct }: { label: string; pct: number }) {
  return (
    <div className="meter" role="meter" aria-valuenow={pct} aria-valuemin={0} aria-valuemax={100} aria-label={label}>
      <div className="meter-head">
        <span>{label}</span>
        <strong>{pct}%</strong>
      </div>
      <div className="meter-track">
        <div className="meter-fill" style={{ width: `${Math.min(100, Math.max(0, pct))}%` }} />
      </div>
    </div>
  );
}

export function DataTable({ caption, columns, rows }: { caption: string; columns: string[]; rows: (string | number)[][] }) {
  if (rows.length === 0) return null;
  return (
    <details className="datatable">
      <summary>Table: {caption}</summary>
      <table>
        <caption className="sr-only">{caption}</caption>
        <thead>
          <tr>
            {columns.map((c) => (
              <th key={c}>{c}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((r, i) => (
            <tr key={i}>
              {r.map((cell, j) => (
                <td key={j}>{cell}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </details>
  );
}
