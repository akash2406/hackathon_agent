/** Platform admin pages: estate overview, access review, usage log, settings & health. */
import { useEffect, useState, type ReactNode } from "react";
import { ApiFailure } from "../api";
import { BarList, formatMoney } from "../components/charts";
import { Icon } from "../components/Icon";
import { DataCard, Kpi, PageHeader, Segmented } from "../components/ui";
import { useApp, useTool, type ToolState } from "../state";
import type { AdminSettings, UsageReport } from "../types";

const money = (m: Record<string, number> | undefined) =>
  Object.entries(m ?? {}).map(([c, v]) => formatMoney(v, c)).join(" + ") || "n/a";

// --------------------------------------------------------------------------- estate

export function Estate() {
  const { setScope, navigate } = useApp();
  const [state, retry] = useTool("platform_estate_overview", {}, { scoped: false });
  return (
    <>
      <PageHeader title="Estate overview" subtitle="Every subscription CRIP covers in one view: spend, security and open recommendations." />
      <div className="kpis">
        <Kpi label="Subscriptions" icon="estate" state={state} accent="brand" value={(d) => String(d.subscription_count)} />
        <Kpi label="Month-to-date spend" icon="spend" state={state} value={(d) => money(d.total_cost_mtd_by_currency)} />
        <Kpi label="Lowest secure score" icon="shield" state={state} accent="warn"
          value={(d) => { const s = (d.subscriptions ?? []).map((r: any) => r.secure_score_pct).filter((x: any) => x != null); return s.length ? `${Math.min(...s)}%` : "n/a"; }} />
        <Kpi label="High-impact recommendations" icon="alert" state={state} value={(d) => String((d.subscriptions ?? []).reduce((a: number, r: any) => a + r.advisor_high_impact, 0))} />
      </div>
      <div className="grid">
        <DataCard span={3} title="Subscriptions" subtitle="Click a row to open its dashboards" state={state} onRetry={retry}>
          {(d) => (
            <table className="table clickable">
              <thead><tr><th>Subscription</th><th className="num">Cost MTD</th><th className="num">Secure score</th><th className="num">Security recs</th><th className="num">Reliability recs</th><th className="num">Cost recs</th><th className="num">High impact</th></tr></thead>
              <tbody>
                {(d.subscriptions ?? []).map((r: any) => (
                  <tr key={r.subscription_id} onClick={() => { setScope(`/subscriptions/${r.subscription_id}`); navigate("/"); }}>
                    <td><strong>{r.name}</strong><div className="mono small muted">{r.subscription_id}</div></td>
                    <td className="num strong">{r.cost_mtd != null ? formatMoney(r.cost_mtd, r.currency) : "n/a"}</td>
                    <td className="num"><ScorePill pct={r.secure_score_pct} /></td>
                    <td className="num">{r.advisor_security}</td>
                    <td className="num">{r.advisor_reliability}</td>
                    <td className="num">{r.advisor_cost}</td>
                    <td className="num strong">{r.advisor_high_impact}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </DataCard>
        <DataCard span={2} title="Spend by subscription" subtitle="Month-to-date" state={state} onRetry={retry}>
          {(d) => (
            <BarList ariaLabel="Month-to-date cost by subscription" valueHeader="Cost MTD"
              data={(d.subscriptions ?? []).filter((r: any) => r.cost_mtd != null).map((r: any) => ({ label: r.name, value: r.cost_mtd, display: formatMoney(r.cost_mtd, r.currency) }))} />
          )}
        </DataCard>
        <DataCard title="Secure score by subscription" subtitle="Defender for Cloud" state={state} onRetry={retry}>
          {(d) => (
            <BarList ariaLabel="Secure score by subscription" valueHeader="Score"
              data={(d.subscriptions ?? []).filter((r: any) => r.secure_score_pct != null).sort((a: any, b: any) => a.secure_score_pct - b.secure_score_pct)
                .map((r: any) => ({ label: r.name, value: r.secure_score_pct, display: `${r.secure_score_pct}%` }))} />
          )}
        </DataCard>
      </div>
    </>
  );
}

function ScorePill({ pct }: { pct: number | null }) {
  if (pct == null) return <span className="muted">n/a</span>;
  const tone = pct >= 75 ? "good" : pct >= 55 ? "warn" : "bad";
  return <span className={`score-pill ${tone}`}>{pct}%</span>;
}

// --------------------------------------------------------------------------- access review

export function AccessReview() {
  const { subscriptions } = useApp();
  const [filter, setFilter] = useState<"privileged" | "all">("privileged");
  const [state, retry] = useTool("platform_access_review");
  const privileged = new Set(["Owner", "Contributor", "User Access Administrator", "Role Based Access Control Administrator"]);
  return (
    <>
      <PageHeader title="Access review" subtitle={`Who holds which Azure role on the selected subscription (${subscriptions?.length ?? 0} in scope), including inherited grants. Names from Microsoft Graph.`} />
      <FindingsRow state={state} />
      <div className="grid">
        <DataCard span={3} title="Role assignments" subtitle="Subscription and inherited from management groups" state={state} onRetry={retry}
          actions={<Segmented label="Show" value={filter} onChange={setFilter} options={[{ value: "privileged", label: "Privileged roles" }, { value: "all", label: "All roles" }]} />}>
          {(d) => (
            <table className="table">
              <thead><tr><th>Principal</th><th>Type</th><th>Role</th><th>Granted at</th><th>Flags</th></tr></thead>
              <tbody>
                {(d.assignments ?? []).filter((a: any) => filter === "all" || privileged.has(a.role)).map((a: any, i: number) => (
                  <tr key={i}>
                    <td><strong>{a.name ?? <span className="muted">{a.orphaned ? "Deleted identity" : "Name unavailable"}</span>}</strong>
                      <div className="mono small muted">{a.upn ?? a.principal_id}</div></td>
                    <td><span className="tag subtle">{a.principal_type}</span></td>
                    <td><strong>{a.role}</strong></td>
                    <td className="small">{a.inherited ? <><Icon name="estate" size={13} /> {a.scope.split("/").pop()} (inherited)</> : "This subscription"}</td>
                    <td>
                      {a.guest && <span className="impact impact-high">Guest</span>} {a.orphaned && <span className="impact impact-low">Orphaned</span>}{" "}
                      {privileged.has(a.role) && String(a.principal_type).toLowerCase() === "user" && <span className="impact impact-medium">Direct user</span>}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </DataCard>
      </div>
    </>
  );
}

function FindingsRow({ state }: { state: ToolState }) {
  const d = state.state === "done" && state.response.data ? (state.response.data as Record<string, any>) : null;
  if (!d) return null;
  return (
    <div className="kpis">
      {(d.findings ?? []).map((f: any) => (
        <div key={f.finding} className={`kpi ${f.severity === "High" ? "warn" : f.severity === "Info" ? "good" : ""}`}>
          <div className="kpi-top"><span className="kpi-icon"><Icon name={f.severity === "High" ? "alert" : "users"} size={16} /></span><span className="kpi-label">{f.finding}</span></div>
          <div className="kpi-value">{f.count}</div>
          <div className="kpi-sub">{f.advice}</div>
        </div>
      ))}
    </div>
  );
}

// --------------------------------------------------------------------------- usage

function actionLabel(action: string): string {
  if (action === "chat") return "Chat question";
  if (action === "me") return "Signed in";
  if (action.startsWith("admin:")) return `Admin: ${action.split("/").pop()}`;
  if (action.startsWith("tool:")) return action.slice(5).replace(/^(costpulse|optimizer|inventory|governance|platform)_/, "").replace(/_/g, " ");
  return action;
}

export function Usage() {
  const { api, subscriptions } = useApp();
  const subscriptionLabel = (scope: string | null) => {
    if (!scope) return "—";
    const id = scope.split("/")[2];
    return subscriptions?.find((s) => s.subscription_id === id)?.display_name ?? `…${(id ?? scope).slice(-8)}`;
  };
  const [days, setDays] = useState(7);
  const [report, setReport] = useState<UsageReport | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    setReport(null);
    setError(null);
    api.adminUsage(days).then(setReport).catch((e) => setError(e instanceof ApiFailure ? e.message : String(e)));
  }, [api, days]);
  return (
    <>
      <PageHeader title="Usage log" subtitle="Who used CRIP, how, and what was denied. Sourced from CRIP's own audit log." />
      <div className="toolbar">
        <Segmented label="Window" value={days} onChange={setDays} options={[{ value: 1, label: "24 hours" }, { value: 7, label: "7 days" }, { value: 30, label: "30 days" }, { value: 90, label: "90 days" }]} />
      </div>
      {error && <div className="state-error"><Icon name="alert" /> <div><strong>Could not load the usage log.</strong> {error}</div></div>}
      {!report && !error && <div className="skeleton h120" />}
      {report && (
        <>
          <div className="kpis">
            <div className="kpi brand"><div className="kpi-top"><span className="kpi-icon"><Icon name="log" size={16} /></span><span className="kpi-label">Requests</span></div><div className="kpi-value">{report.total_events.toLocaleString()}</div></div>
            <div className="kpi"><div className="kpi-top"><span className="kpi-icon"><Icon name="users" size={16} /></span><span className="kpi-label">Distinct users</span></div><div className="kpi-value">{report.distinct_users}</div></div>
            <div className="kpi warn"><div className="kpi-top"><span className="kpi-icon"><Icon name="lock" size={16} /></span><span className="kpi-label">Denied requests</span></div><div className="kpi-value">{report.denied}</div><div className="kpi-sub">users asking for views their access doesn't include</div></div>
            <div className="kpi"><div className="kpi-top"><span className="kpi-icon"><Icon name="ask" size={16} /></span><span className="kpi-label">Chat questions</span></div><div className="kpi-value">{report.by_action.find((a) => a.name === "chat")?.events ?? 0}</div></div>
          </div>
          <div className="grid">
            <section className="panel">
              <div className="panel-head"><div><h2>Most active users</h2></div></div>
              <BarList ariaLabel="Requests by user" valueHeader="Requests" data={report.top_users.map((u) => ({ label: u.user, value: u.events, display: String(u.events) }))} />
            </section>
            <section className="panel span-2">
              <div className="panel-head"><div><h2>Recent activity</h2><p>Newest first</p></div></div>
              <div className="table-wrap">
              <table className="table">
                <thead><tr><th>When</th><th>User</th><th>Action</th><th>Subscription</th><th>Outcome</th><th className="num">Latency</th></tr></thead>
                <tbody>
                  {report.events.slice(0, 40).map((e, i) => (
                    <tr key={i}>
                      <td className="small nowrap">{new Date(e.at).toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" })}</td>
                      <td className="nowrap">{e.user_name ?? "unknown"}</td>
                      <td title={e.detail ?? e.action}>{actionLabel(e.action)}</td>
                      <td className="small truncate">{subscriptionLabel(e.scope)}</td>
                      <td><span className={`outcome ${e.outcome}`}>{e.outcome}</span></td>
                      <td className="num small">{e.latency_ms != null ? `${e.latency_ms} ms` : ""}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
              </div>
            </section>
          </div>
        </>
      )}
    </>
  );
}

// --------------------------------------------------------------------------- settings & health

export function Settings() {
  const { api, me } = useApp();
  const [settings, setSettings] = useState<AdminSettings | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    api.adminSettings().then(setSettings).catch((e) => setError(e instanceof ApiFailure ? e.message : String(e)));
  }, [api]);
  const s = settings as Record<string, any> | null;
  const row = (label: string, value: ReactNode, ok?: boolean) => (
    <tr><td>{label}</td><td>{value}</td><td>{ok === undefined ? null : ok ? <span className="outcome ok">ok</span> : <span className="outcome error">check</span>}</td></tr>
  );
  return (
    <>
      <PageHeader title="Settings & health" subtitle="How CRIP is configured and whether its dependencies are healthy. Read-only: change these in App Settings / Bicep." />
      {error && <div className="state-error"><Icon name="alert" /> <div>{error}</div></div>}
      {!s && !error && <div className="skeleton h120" />}
      {s && (
        <div className="grid">
          <section className="panel span-2">
            <div className="panel-head"><div><h2>Access model</h2><p>Who can see what (docs/access-model.md)</p></div></div>
            <table className="table">
              <tbody>
                {row("Azure access mode", s.access_mode === "app_identity" ? "CRIP identity reads Azure; CRIP checks each user's access" : "Each user's own identity (on-behalf-of)")}
                {row("Management group", s.management_group_id ?? "not set (all subscriptions CRIP can read)")}
                {row("Subscriptions in scope", String(s.subscriptions_in_scope))}
                {row("Azure RBAC check", s.rbac_access_check ? "on: Reader → resource views; cost roles → cost views" : "off: app roles / groups only", s.rbac_access_check)}
                {row("Group mappings", ["platform_admin", "cost_reader", "reader"].filter((k) => s.group_mappings?.[k]).join(", ") || "none (app roles only)")}
                {row("Access cache", `${s.access_cache_seconds} s`)}
                {row("Microsoft Graph (names, groups)", s.graph, s.graph === "ok")}
              </tbody>
            </table>
          </section>
          <section className="panel">
            <div className="panel-head"><div><h2>Your access</h2><p>As CRIP sees it right now</p></div></div>
            <ul className="explain">
              <li>Platform admin via <strong>{me?.global_via.join(", ") || "n/a"}</strong></li>
              {me?.subscriptions.map((x) => <li key={x.subscription_id}>{x.display_name}: <strong>{x.level}</strong> <span className="muted">({x.via.join(", ")})</span></li>)}
            </ul>
          </section>
          <section className="panel span-3">
            <div className="panel-head"><div><h2>Agents & platform</h2></div></div>
            <table className="table">
              <tbody>
                {row("Foundry project", <span className="mono small">{String(s.foundry_project_endpoint)}</span>)}
                {row("Model deployment", String(s.foundry_model_deployment ?? "not set"), !!s.foundry_model_deployment)}
                {row("Agents", (s.agents as string[]).join(", "))}
                {row("Register agents at startup", s.register_agents_on_startup ? "yes" : "no")}
                {row("Storage", String(s.storage))}
                {row("Environment", String(s.environment))}
              </tbody>
            </table>
          </section>
        </div>
      )}
    </>
  );
}
