/** Savings: Azure Advisor recommendations and idle resources priced with actual cost. */
import { BarList, formatMoney } from "../components/charts";
import { DataCard, Kpi, PageHeader } from "../components/ui";
import { useTool } from "../state";

const money = (m: Record<string, number> | undefined) =>
  Object.entries(m ?? {}).map(([c, v]) => formatMoney(v, c)).join(" + ") || "n/a";

export default function Savings() {
  const [advisor, retryAdvisor] = useTool("optimizer_advisor_recommendations", { top_n: 25 });
  const [idle, retryIdle] = useTool("optimizer_find_idle_resources");

  return (
    <>
      <PageHeader title="Savings opportunities" subtitle="What Azure Advisor recommends, and what you are paying for that does nothing. Read-only: review before acting." />
      <div className="kpis">
        <Kpi label="Advisor savings / year" icon="savings" state={advisor} accent="good" value={(d) => money(d.total_annual_savings_by_currency)} sub={() => "Azure Advisor's estimate"} />
        <Kpi label="Recommendations" icon="spark" state={advisor} value={(d) => String(d.recommendation_count)} />
        <Kpi label="Idle cost this month" icon="alert" state={idle} accent="warn" value={(d) => money(d.cost_mtd_by_currency)} sub={() => "actual month-to-date cost"} />
        <Kpi label="Idle resources" icon="inventory" state={idle} value={(d) => String(d.resource_count)} />
      </div>

      <div className="grid">
        <DataCard span={2} title="Advisor recommendations" subtitle="Ranked by estimated annual savings" state={advisor} onRetry={retryAdvisor}>
          {(d) => (
            <table className="table">
              <thead><tr><th>Impact</th><th>Recommendation</th><th>Resource</th><th className="num">Savings / yr</th></tr></thead>
              <tbody>
                {(d.recommendations ?? []).map((r: any, i: number) => (
                  <tr key={i}>
                    <td><span className={`impact impact-${String(r.impact).toLowerCase()}`}>{r.impact}</span></td>
                    <td>{r.solution ?? r.problem}</td>
                    <td className="mono truncate" title={r.resource}>{r.resource}</td>
                    <td className="num strong">{r.annual_savings != null ? formatMoney(r.annual_savings, r.currency) : "n/a"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </DataCard>

        <DataCard title="Savings by type" subtitle="Advisor estimate per category" state={advisor} onRetry={retryAdvisor}>
          {(d) => (
            <BarList ariaLabel="Annual savings by recommendation type" valueHeader="Savings / yr"
              data={(d.by_problem ?? []).map((p: any) => ({ label: `${p.problem} (${p.count})`, value: p.annual_savings, display: formatMoney(p.annual_savings, p.currency) }))} />
          )}
        </DataCard>

        <DataCard span={2} title="Idle resources" subtitle="Found with Azure Resource Graph, priced with real month-to-date cost" state={idle} onRetry={retryIdle}>
          {(d) => (
            <table className="table">
              <thead><tr><th>Resource</th><th>Why it's idle</th><th>Resource group</th><th className="num">Cost MTD</th></tr></thead>
              <tbody>
                {(d.resources ?? []).map((r: any, i: number) => (
                  <tr key={i}>
                    <td className="mono truncate" title={r.name}>{r.name}</td>
                    <td><span className="tag">{r.category}</span></td>
                    <td className="truncate">{r.resource_group}</td>
                    <td className="num strong">{r.cost_mtd != null ? formatMoney(r.cost_mtd, r.currency) : "n/a"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </DataCard>

        <DataCard title="Idle cost by kind" subtitle="Month-to-date" state={idle} onRetry={retryIdle}>
          {(d) => {
            const currency = Object.keys(d.cost_mtd_by_currency ?? {})[0] ?? "";
            return (
              <BarList ariaLabel="Idle cost by kind" valueHeader="Cost MTD"
                data={(d.by_category ?? []).map((c: any) => ({ label: `${c.category} (${c.count})`, value: c.cost_mtd, display: formatMoney(c.cost_mtd, currency) }))} />
            );
          }}
        </DataCard>
      </div>
    </>
  );
}
