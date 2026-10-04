/**
 * Building blocks for the dashboard pages.
 *
 * ``DataCard`` is the honesty boundary of the UI: it renders a chart only for
 * a grounded result (ok / partial). Errors and "no data" are shown as such,
 * never as an empty chart or a zero. Every grounded card carries its proof.
 */
import { useState, type ReactNode } from "react";
import { useApp, type ToolState } from "../state";
import type { AgentResponse } from "../types";
import { Icon } from "./Icon";

export function PageHeader({ title, subtitle, actions }: { title: string; subtitle: string; actions?: ReactNode }) {
  return (
    <header className="page-header">
      <div>
        <h1>{title}</h1>
        <p>{subtitle}</p>
      </div>
      {actions && <div className="page-actions">{actions}</div>}
    </header>
  );
}

export function Segmented<T extends string | number>({ value, options, onChange, label }: { value: T; options: { value: T; label: string }[]; onChange: (v: T) => void; label: string }) {
  return (
    <div className="segmented" role="radiogroup" aria-label={label}>
      {options.map((o) => (
        <button key={String(o.value)} role="radio" aria-checked={o.value === value} className={o.value === value ? "active" : ""} onClick={() => onChange(o.value)}>
          {o.label}
        </button>
      ))}
    </div>
  );
}

function asOf(r: AgentResponse): string {
  const through = r.data?.["data_through"];
  if (typeof through === "string") return `data through ${through}`;
  return r.data_timestamp ? `as of ${new Date(r.data_timestamp).toLocaleString()}` : "";
}

export function Proof({ r }: { r: AgentResponse }) {
  const { api } = useApp();
  const [open, setOpen] = useState(false);
  const ids = r.sources.map((s) => s.request_id).filter(Boolean);
  return (
    <footer className="proof-bar">
      {api.demo ? (
        <span className="proof-chip sample" title="Demo mode: sample data, no Azure call was made">
          <Icon name="alert" size={14} /> Sample data · not from Azure
        </span>
      ) : (
        <span className="proof-chip" title="Queried with your own Azure permissions (on-behalf-of)">
          <Icon name="shield" size={14} /> Grounded · your permissions
        </span>
      )}
      <span>{asOf(r)}</span>
      {ids.length > 0 && <span className="mono">req {ids[0]}{ids.length > 1 ? ` +${ids.length - 1}` : ""}</span>}
      <button className="link small" onClick={() => setOpen((o) => !o)} aria-expanded={open}>
        {open ? "Hide proof" : "Show query & caveats"}
      </button>
      {open && (
        <div className="proof-detail">
          {r.sources.map((s, i) => (
            <div key={i} className="mono small">
              {s.api} · scope {s.scope} · HTTP {s.http_status ?? "n/a"}
            </div>
          ))}
          <pre className="query">{r.query_used ?? "(no Azure call was made)"}</pre>
          {r.caveats.length > 0 && (
            <ul>
              {r.caveats.map((c, i) => (
                <li key={i}>{c}</li>
              ))}
            </ul>
          )}
        </div>
      )}
    </footer>
  );
}

export function DataCard({
  title,
  subtitle,
  state,
  onRetry,
  children,
  span = 1,
  actions,
}: {
  title: string;
  subtitle?: string;
  state: ToolState;
  onRetry?: () => void;
  children: (data: Record<string, any>, response: AgentResponse) => ReactNode;
  span?: 1 | 2 | 3;
  actions?: ReactNode;
}) {
  let body: ReactNode;
  if (state.state === "idle") {
    body = <div className="empty-state">Choose a subscription to load this view.</div>;
  } else if (state.state === "loading") {
    body = (
      <div className="skeleton-block" aria-busy="true" aria-label="Loading">
        <div className="skeleton w60" />
        <div className="skeleton h120" />
        <div className="skeleton w40" />
      </div>
    );
  } else if (state.state === "failed") {
    body = (
      <div className="state-error" role="alert">
        <Icon name="alert" /> <div><strong>No data shown: the request failed.</strong> {state.failure.message}
          {state.failure.correlationId && <div className="mono small">correlation id {state.failure.correlationId}</div>}
        </div>
        {onRetry && <button className="secondary small" onClick={onRetry}>Retry</button>}
      </div>
    );
  } else if (state.response.status === "error") {
    body = (
      <>
        <div className="state-error" role="alert">
          <Icon name="alert" /> <div><strong>Azure data could not be retrieved.</strong> {state.response.answer}</div>
          {onRetry && <button className="secondary small" onClick={onRetry}>Retry</button>}
        </div>
        <Proof r={state.response} />
      </>
    );
  } else if (state.response.status === "no_data" || !state.response.data) {
    body = (
      <>
        <div className="empty-state">{state.response.answer}</div>
        <Proof r={state.response} />
      </>
    );
  } else {
    body = (
      <>
        {state.response.status === "partial" && <div className="partial-note">Partial result: {state.response.caveats.find((c) => !c.startsWith("Azure Cost")) ?? "see caveats"}</div>}
        {children(state.response.data as Record<string, any>, state.response)}
        <Proof r={state.response} />
      </>
    );
  }
  return (
    <section className={`panel span-${span}`}>
      <div className="panel-head">
        <div>
          <h2>{title}</h2>
          {subtitle && <p>{subtitle}</p>}
        </div>
        {actions}
      </div>
      {body}
    </section>
  );
}

export function Kpi({
  label,
  icon,
  state,
  value,
  sub,
  accent,
}: {
  label: string;
  icon: string;
  state: ToolState;
  value: (d: Record<string, any>) => string;
  sub?: (d: Record<string, any>) => string;
  accent?: "brand" | "good" | "warn";
}) {
  const d = state.state === "done" && (state.response.status === "ok" || state.response.status === "partial") ? (state.response.data as Record<string, any>) : null;
  const failed = state.state === "failed" || (state.state === "done" && state.response.status === "error");
  const none = state.state === "done" && state.response.status === "no_data";
  return (
    <div className={`kpi ${accent ?? ""}`}>
      <div className="kpi-top">
        <span className="kpi-icon"><Icon name={icon} size={16} /></span>
        <span className="kpi-label">{label}</span>
      </div>
      {state.state === "loading" || state.state === "idle" ? (
        <div className="skeleton kpi-skel" />
      ) : d ? (
        <>
          <div className="kpi-value">{value(d)}</div>
          {sub && <div className="kpi-sub">{sub(d)}</div>}
        </>
      ) : (
        <div className="kpi-value muted">{failed ? "Unavailable" : none ? "None" : "n/a"}</div>
      )}
    </div>
  );
}
