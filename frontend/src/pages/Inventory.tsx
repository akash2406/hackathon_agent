/** Inventory & tags: what runs where, and how much of it can be attributed to an owner. */
import { useState } from "react";
import { BarList, Meter } from "../components/charts";
import { DataCard, Kpi, PageHeader, Segmented } from "../components/ui";
import { useTool } from "../state";

const shortType = (t: string) => t.split("/").slice(-1)[0] ?? t;

export default function Inventory() {
  const [tagKey, setTagKey] = useState("owner");
  const [state, retry] = useTool("inventory_resource_summary", { tag_key: tagKey, top_n: 12 });

  return (
    <>
      <PageHeader title="Inventory & tagging" subtitle="Live from Azure Resource Graph: only resources your account can read." />
      <div className="toolbar">
        <Segmented label="Tag to measure" value={tagKey} onChange={setTagKey}
          options={[{ value: "owner", label: "owner" }, { value: "costCenter", label: "costCenter" }, { value: "environment", label: "environment" }, { value: "project", label: "project" }]} />
      </div>
      <div className="kpis">
        <Kpi label="Resources" icon="inventory" state={state} accent="brand" value={(d) => d.total_resources.toLocaleString()} />
        <Kpi label="Resource types" icon="overview" state={state} value={(d) => String(d.resource_type_count)} />
        <Kpi label="Regions" icon="trends" state={state} value={(d) => String(d.by_location?.length ?? 0)} />
        <Kpi label={`Tagged '${tagKey}'`} icon="shield" state={state} accent="good" value={(d) => (d.tag_coverage ? `${d.tag_coverage.coverage_pct}%` : "n/a")}
          sub={(d) => (d.tag_coverage ? `${d.tag_coverage.untagged} untagged` : "")} />
      </div>
      <div className="grid">
        <DataCard span={2} title="Resources by type" state={state} onRetry={retry}>
          {(d) => (
            <BarList ariaLabel="Resources by type" valueHeader="Count"
              data={(d.by_type ?? []).map((t: any) => ({ label: shortType(t.group), value: t.count, display: String(t.count) }))} />
          )}
        </DataCard>
        <DataCard title="Resources by region" state={state} onRetry={retry}>
          {(d) => (
            <BarList ariaLabel="Resources by region" valueHeader="Count"
              data={(d.by_location ?? []).map((t: any) => ({ label: t.group, value: t.count, display: String(t.count) }))} />
          )}
        </DataCard>
        <DataCard span={3} title={`Tag coverage: '${tagKey}'`} subtitle="Untagged resources cannot be attributed to a cost owner" state={state} onRetry={retry}>
          {(d) =>
            d.tag_coverage ? (
              <div className="coverage">
                <Meter label={`Resources carrying '${tagKey}'`} pct={d.tag_coverage.coverage_pct} />
                <table className="table">
                  <thead><tr><th>Resource group</th><th className="num">Untagged</th><th className="num">Total</th><th className="num">Coverage</th></tr></thead>
                  <tbody>
                    {(d.tag_coverage.worst_resource_groups ?? []).map((w: any) => (
                      <tr key={w.resource_group}>
                        <td>{w.resource_group}</td>
                        <td className="num strong">{w.untagged}</td>
                        <td className="num">{w.total}</td>
                        <td className="num">{w.total ? `${Math.round(((w.total - w.untagged) / w.total) * 100)}%` : "n/a"}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : (
              <div className="empty-state">No tag coverage returned.</div>
            )
          }
        </DataCard>
      </div>
    </>
  );
}
