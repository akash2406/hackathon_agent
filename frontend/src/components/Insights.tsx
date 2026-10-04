/**
 * Turns a grounded tool result into the right visual for its job.
 *
 * Every figure here comes from AgentResponse.data, which the backend computed
 * from Azure's response. The UI never derives new numbers beyond formatting.
 * Results with status "error" carry no data by contract, so they never render
 * a chart.
 */
import type { ComponentType } from "react";
import type { AgentResponse } from "../types";
import { BarList, ColumnChart, DataTable, Meter, StatTile, formatMoney, type ColumnDatum } from "./charts";

type Money = Record<string, number>;
interface Group { group: string; cost?: number; count?: number; currency?: string }

const sumText = (m: Money | undefined) =>
  Object.entries(m ?? {}).map(([cur, v]) => formatMoney(v, cur)).join(" + ") || "n/a";

function Breakdown({ d }: { d: Record<string, any> }) {
  const top: Group[] = d.top ?? [];
  const currency = top[0]?.currency ?? Object.keys(d.total_by_currency ?? {})[0] ?? "";
  const by = String(d.group_by ?? "").replace("_", " ");
  return (
    <>
      <div className="stats">
        <StatTile label={`Total (${String(d.timeframe ?? "").replace(/_/g, " ")})`} value={sumText(d.total_by_currency)} sub={`through ${d.data_through}`} />
        <StatTile label={`${by}s`} value={String((d.top?.length ?? 0) + (d.remaining_groups ?? 0))} />
      </div>
      <BarList
        ariaLabel={`Cost by ${by}`}
        valueHeader={`Cost (${currency})`}
        data={top.map((g) => ({ label: g.group, value: g.cost ?? 0, display: formatMoney(g.cost ?? 0, g.currency ?? currency) }))}
      />
    </>
  );
}

function Trend({ d }: { d: Record<string, any> }) {
  const currency: string = d.currency ?? "";
  const spikes = new Map<string, any>((d.anomalies ?? []).map((a: any) => [a.date, a]));
  const data: ColumnDatum[] = (d.series ?? []).map((p: any) => {
    const a = spikes.get(p.date);
    return {
      label: p.date,
      value: p.cost,
      tone: a ? "spike" : "base",
      note: a
        ? `+${formatMoney(a.delta, currency)} vs typical` + (a.drivers?.[0] ? `; driven by ${a.drivers[0].service}` : "")
        : undefined,
    };
  });
  const change = d.change_pct_second_half_vs_first;
  return (
    <>
      <div className="stats">
        <StatTile label="Total" value={formatMoney(d.total ?? 0, currency)} sub={`${data.length} days`} />
        <StatTile label="Typical day" value={formatMoney(d.median_daily_cost ?? 0, currency)} />
        <StatTile label="Trend" value={change === null || change === undefined ? "n/a" : `${change > 0 ? "+" : ""}${change}%`} sub="2nd half vs 1st half" />
        <StatTile label="Spike days" value={String(d.anomalies?.length ?? 0)} />
      </div>
      <ColumnChart
        ariaLabel="Daily cost"
        currency={currency}
        data={data}
        reference={{ value: d.median_daily_cost ?? 0, label: "typical day" }}
      />
      {(d.anomalies ?? []).length > 0 && (
        <ul className="anomalies">
          {d.anomalies.map((a: any) => (
            <li key={a.date}>
              <span className="status-chip critical">▲ Spike</span> {a.date}: {formatMoney(a.cost, currency)} (typical{" "}
              {formatMoney(a.baseline, currency)})
              {a.drivers?.length > 0 && (
                <> · drivers: {a.drivers.map((x: any) => `${x.service} +${formatMoney(x.delta, currency)}`).join(", ")}</>
              )}
            </li>
          ))}
        </ul>
      )}
    </>
  );
}

function Forecast({ d }: { d: Record<string, any> }) {
  const currency: string = d.currency ?? "";
  const data: ColumnDatum[] = (d.series ?? []).map((p: any) => ({
    label: p.date,
    value: p.cost,
    tone: p.kind === "forecast" ? "forecast" : "base",
  }));
  return (
    <>
      <div className="stats">
        {d.projected_month_end !== null && d.projected_month_end !== undefined && (
          <StatTile hero label={`Projected ${d.month} total (Azure forecast)`} value={formatMoney(d.projected_month_end, currency)} />
        )}
        <StatTile label="Actual so far" value={formatMoney(d.actual_to_date ?? 0, currency)} sub={`through ${d.data_through}`} />
        {d.forecast_remaining !== null && d.forecast_remaining !== undefined && (
          <StatTile label="Forecast remaining" value={formatMoney(d.forecast_remaining, currency)} />
        )}
      </div>
      <ColumnChart ariaLabel={`Daily cost, ${d.month}: actual and Azure forecast`} currency={currency} data={data} />
    </>
  );
}

function Savings({ d }: { d: Record<string, any> }) {
  const totals: Money = d.total_annual_savings_by_currency ?? {};
  const currency = Object.keys(totals)[0] ?? "";
  return (
    <>
      <div className="stats">
        <StatTile hero label="Estimated annual savings (Azure Advisor)" value={sumText(totals)} />
        <StatTile label="Recommendations" value={String(d.recommendation_count ?? 0)} />
      </div>
      <BarList
        ariaLabel="Estimated annual savings by recommendation"
        valueHeader={`Annual savings (${currency})`}
        data={(d.by_problem ?? []).map((p: any) => ({
          label: `${p.problem} (${p.count})`,
          value: p.annual_savings,
          display: formatMoney(p.annual_savings, p.currency ?? currency),
        }))}
      />
      <DataTable
        caption="Top recommendations"
        columns={["Resource", "Recommendation", "Impact", "Annual savings"]}
        rows={(d.recommendations ?? []).map((r: any) => [
          r.resource ?? "",
          r.solution ?? r.problem ?? "",
          r.impact ?? "",
          r.annual_savings != null ? formatMoney(r.annual_savings, r.currency ?? currency) : "n/a",
        ])}
      />
    </>
  );
}

function Idle({ d }: { d: Record<string, any> }) {
  const totals: Money = d.cost_mtd_by_currency ?? {};
  const currency = Object.keys(totals)[0] ?? "";
  const priced = Object.keys(totals).length > 0;
  return (
    <>
      <div className="stats">
        {priced && <StatTile hero label="Idle resources cost this month so far" value={sumText(totals)} />}
        <StatTile label="Idle resources" value={String(d.resource_count ?? 0)} />
      </div>
      <BarList
        ariaLabel={priced ? "Month-to-date cost by idle category" : "Idle resources by category"}
        valueHeader={priced ? `Cost MTD (${currency})` : "Count"}
        data={(d.by_category ?? []).map((c: any) => ({
          label: `${c.category} (${c.count})`,
          value: priced ? c.cost_mtd : c.count,
          display: priced ? formatMoney(c.cost_mtd, currency) : String(c.count),
        }))}
      />
      <DataTable
        caption="Idle resources"
        columns={["Name", "Category", "Resource group", "Location", "Cost MTD"]}
        rows={(d.resources ?? []).map((r: any) => [
          r.name ?? "",
          r.category ?? "",
          r.resource_group ?? "",
          r.location ?? "",
          r.cost_mtd != null ? formatMoney(r.cost_mtd, r.currency ?? currency) : "n/a",
        ])}
      />
    </>
  );
}

function Inventory({ d }: { d: Record<string, any> }) {
  const shortType = (t: string) => t.split("/").slice(-1)[0] ?? t;
  return (
    <>
      <div className="stats">
        <StatTile label="Resources" value={String(d.total_resources ?? 0)} />
        <StatTile label="Resource types" value={String(d.resource_type_count ?? 0)} />
        <StatTile label="Locations" value={String(d.by_location?.length ?? 0)} />
      </div>
      <BarList
        ariaLabel="Resources by type"
        valueHeader="Count"
        data={(d.by_type ?? []).map((t: Group) => ({ label: shortType(t.group), value: t.count ?? 0, display: String(t.count ?? 0) }))}
      />
      {d.tag_coverage && (
        <>
          <Meter label={`Resources tagged '${d.tag_coverage.tag_key}'`} pct={d.tag_coverage.coverage_pct} />
          <DataTable
            caption="Resource groups with the most untagged resources"
            columns={["Resource group", "Untagged", "Total"]}
            rows={(d.tag_coverage.worst_resource_groups ?? []).map((w: any) => [w.resource_group, w.untagged, w.total])}
          />
        </>
      )}
    </>
  );
}

function Subscriptions({ d }: { d: Record<string, any> }) {
  return (
    <DataTable
      caption="Subscriptions"
      columns={["Name", "Subscription id", "State"]}
      rows={(d.subscriptions ?? []).map((s: any) => [s.display_name ?? "", s.subscription_id ?? "", s.state ?? ""])}
    />
  );
}

const RENDERERS: Record<string, ComponentType<{ d: Record<string, any> }>> = {
  breakdown: Breakdown,
  trend: Trend,
  forecast: Forecast,
  savings: Savings,
  idle_resources: Idle,
  inventory: Inventory,
  subscriptions: Subscriptions,
};

export default function Insights({ c }: { c: AgentResponse }) {
  const kind = typeof c.data?.["kind"] === "string" ? (c.data["kind"] as string) : null;
  const Render = kind ? RENDERERS[kind] : undefined;
  if (!Render || !c.data) return null;
  return (
    <div className="insight">
      <Render d={c.data as Record<string, any>} />
    </div>
  );
}

