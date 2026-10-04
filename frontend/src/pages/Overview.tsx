/** Executive overview: four headline numbers, the daily trend, where money goes and where to save. */
import { BarList, ColumnChart, formatMoney, type ColumnDatum } from "../components/charts";
import { Icon } from "../components/Icon";
import { DataCard, Kpi, PageHeader } from "../components/ui";
import { askLater } from "./Ask";
import { useApp, useTool } from "../state";

const money = (m: Record<string, number> | undefined) =>
  Object.entries(m ?? {}).map(([c, v]) => formatMoney(v, c)).join(" + ") || "n/a";

export default function Overview() {
  const { navigate } = useApp();
  const [mtd, retryMtd] = useTool("costpulse_query_costs", { group_by: "resource_group", timeframe: "month_to_date", top_n: 6 });
  const [forecast] = useTool("costpulse_forecast_month_end");
  const [trend, retryTrend] = useTool("costpulse_cost_trend", { days: 30 });
  const [advisor, retryAdvisor] = useTool("optimizer_advisor_recommendations", { top_n: 5 });
  const [idle] = useTool("optimizer_find_idle_resources");

  return (
    <>
      <PageHeader
        title="Cost overview"
        subtitle="Where your Azure money goes, where it is heading and where you can save. Live from Azure, with your permissions."
        actions={
          <button className="primary" onClick={() => { askLater("Give me a cost overview: what I've spent, what month-end will be, and where I can save."); navigate("/ask"); }}>
            <Icon name="spark" size={16} /> Ask CRIP to explain
          </button>
        }
      />

      <div className="kpis">
        <Kpi label="Month-to-date spend" icon="spend" state={mtd} accent="brand" value={(d) => money(d.total_by_currency)} sub={(d) => `through ${d.data_through}`} />
        <Kpi label="Projected month end" icon="trends" state={forecast} value={(d) => (d.projected_month_end != null ? formatMoney(d.projected_month_end, d.currency) : "No forecast")} sub={() => "Azure Cost Management forecast"} />
        <Kpi label="Advisor savings / year" icon="savings" state={advisor} accent="good" value={(d) => money(d.total_annual_savings_by_currency)} sub={(d) => `${d.recommendation_count} recommendations`} />
        <Kpi label="Idle resources this month" icon="alert" state={idle} accent="warn" value={(d) => money(d.cost_mtd_by_currency)} sub={(d) => `${d.resource_count} idle resources`} />
      </div>

      <div className="grid">
        <DataCard span={2} title="Daily cost, last 30 days" subtitle="Spikes are flagged with a stated, deterministic rule" state={trend} onRetry={retryTrend}>
          {(d) => {
            const spikes = new Map<string, any>((d.anomalies ?? []).map((a: any) => [a.date, a]));
            const data: ColumnDatum[] = (d.series ?? []).map((p: any) => ({
              label: p.date,
              value: p.cost,
              tone: spikes.has(p.date) ? "spike" : "base",
              note: spikes.has(p.date) ? `driven by ${spikes.get(p.date).drivers?.[0]?.service ?? "n/a"}` : undefined,
            }));
            const first = (d.anomalies ?? [])[0];
            return (
              <>
                <ColumnChart ariaLabel="Daily cost" currency={d.currency} data={data} reference={{ value: d.median_daily_cost, label: "typical day" }} />
                {first && (
                  <div className="callout danger">
                    <span className="status-chip critical">▲ Spike</span> {first.date}: {formatMoney(first.cost, d.currency)} vs typical{" "}
                    {formatMoney(first.baseline, d.currency)}
                    {first.drivers?.[0] && <> · driven by <strong>{first.drivers[0].service}</strong> (+{formatMoney(first.drivers[0].delta, d.currency)})</>}
                  </div>
                )}
              </>
            );
          }}
        </DataCard>

        <DataCard title="Top resource groups" subtitle="Month-to-date actual cost" state={mtd} onRetry={retryMtd}>
          {(d) => (
            <BarList
              ariaLabel="Month-to-date cost by resource group"
              valueHeader="Cost"
              data={(d.top ?? []).map((g: any) => ({ label: g.group, value: g.cost, display: formatMoney(g.cost, g.currency) }))}
            />
          )}
        </DataCard>

        <DataCard span={2} title="Top savings opportunities" subtitle="Azure Advisor's own estimates" state={advisor} onRetry={retryAdvisor}
          actions={<button className="link small" onClick={() => navigate("/savings")}>All savings →</button>}>
          {(d) => (
            <ul className="rec-list">
              {(d.recommendations ?? []).slice(0, 4).map((r: any, i: number) => (
                <li key={i}>
                  <span className={`impact impact-${String(r.impact).toLowerCase()}`}>{r.impact}</span>
                  <div className="rec-text">
                    <strong>{r.solution ?? r.problem}</strong>
                    <span>{r.resource}</span>
                  </div>
                  <span className="rec-value">{r.annual_savings != null ? `${formatMoney(r.annual_savings, r.currency)}/yr` : "n/a"}</span>
                </li>
              ))}
            </ul>
          )}
        </DataCard>

        <section className="panel ask-panel">
          <div className="panel-head"><div><h2>Ask in plain language</h2><p>Several specialist agents work together on one answer.</p></div></div>
          {[
            "Why did my costs change in the last 30 days, and what can I do about it?",
            "Which idle resources cost me the most this month?",
            "How many of my resources are missing an 'owner' tag?",
          ].map((q) => (
            <button key={q} className="prompt" onClick={() => { askLater(q); navigate("/ask"); }}>
              <Icon name="spark" size={14} /> {q}
            </button>
          ))}
        </section>
      </div>
    </>
  );
}
