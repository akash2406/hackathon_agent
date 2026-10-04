/** Spend explorer: break actual cost down by any dimension and time window. */
import { useState } from "react";
import { BarList, formatMoney } from "../components/charts";
import { DataCard, Kpi, PageHeader, Segmented } from "../components/ui";
import { useTool } from "../state";

type GroupBy = "resource_group" | "service" | "resource_type" | "tag";
type Timeframe = "month_to_date" | "last_month" | "last_7_days" | "last_30_days";

const GROUPS: { value: GroupBy; label: string }[] = [
  { value: "resource_group", label: "Resource group" },
  { value: "service", label: "Service" },
  { value: "resource_type", label: "Resource type" },
  { value: "tag", label: "Tag" },
];
const WINDOWS: { value: Timeframe; label: string }[] = [
  { value: "month_to_date", label: "Month to date" },
  { value: "last_month", label: "Last month" },
  { value: "last_7_days", label: "7 days" },
  { value: "last_30_days", label: "30 days" },
];

export default function Spend() {
  const [groupBy, setGroupBy] = useState<GroupBy>("resource_group");
  const [timeframe, setTimeframe] = useState<Timeframe>("month_to_date");
  const [tagKey, setTagKey] = useState("costCenter");
  const args = { group_by: groupBy, timeframe, top_n: 15, ...(groupBy === "tag" ? { tag_key: tagKey || "costCenter" } : {}) };
  const [state, retry] = useTool("costpulse_query_costs", args);
  const label = GROUPS.find((g) => g.value === groupBy)!.label.toLowerCase();

  return (
    <>
      <PageHeader title="Spend explorer" subtitle="Actual cost from Azure Cost Management, broken down the way you need it." />
      <div className="toolbar">
        <Segmented label="Group by" value={groupBy} options={GROUPS} onChange={setGroupBy} />
        {groupBy === "tag" && (
          <input className="text-input" value={tagKey} onChange={(e) => setTagKey(e.target.value)} placeholder="Tag key, e.g. costCenter" aria-label="Tag key" />
        )}
        <Segmented label="Time window" value={timeframe} options={WINDOWS} onChange={setTimeframe} />
      </div>

      <div className="kpis three">
        <Kpi label="Total actual cost" icon="spend" state={state} accent="brand"
          value={(d) => Object.entries(d.total_by_currency ?? {}).map(([c, v]) => formatMoney(v as number, c)).join(" + ")}
          sub={(d) => `data through ${d.data_through}`} />
        <Kpi label={`Distinct ${label}s`} icon="inventory" state={state} value={(d) => String((d.top?.length ?? 0) + (d.remaining_groups ?? 0))} />
        <Kpi label={`Largest ${label}`} icon="trends" state={state}
          value={(d) => d.top?.[0]?.group ?? "n/a"}
          sub={(d) => (d.top?.[0] ? formatMoney(d.top[0].cost, d.top[0].currency) : "")} />
      </div>

      <div className="grid">
        <DataCard span={2} title={`Cost by ${label}`} subtitle={WINDOWS.find((w) => w.value === timeframe)!.label} state={state} onRetry={retry}>
          {(d) => (
            <BarList ariaLabel={`Cost by ${label}`} valueHeader="Cost"
              data={(d.top ?? []).map((g: any) => ({ label: g.group, value: g.cost, display: formatMoney(g.cost, g.currency) }))} />
          )}
        </DataCard>
        <DataCard title="Share of total" subtitle="Top items" state={state} onRetry={retry}>
          {(d) => {
            const totals = d.total_by_currency ?? {};
            return (
              <table className="table">
                <thead><tr><th>{label}</th><th className="num">Share</th></tr></thead>
                <tbody>
                  {(d.top ?? []).slice(0, 10).map((g: any) => (
                    <tr key={g.group}>
                      <td className="truncate" title={g.group}>{g.group}</td>
                      <td className="num">{totals[g.currency] ? `${((g.cost / totals[g.currency]) * 100).toFixed(1)}%` : "n/a"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            );
          }}
        </DataCard>
      </div>
    </>
  );
}
