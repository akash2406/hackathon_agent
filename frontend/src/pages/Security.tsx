/** Security & reliability: Defender secure score and Advisor beyond cost. Available at the resources level. */
import { useState } from "react";
import { BarList, Meter } from "../components/charts";
import { DataCard, Kpi, PageHeader, Segmented } from "../components/ui";
import { useTool } from "../state";

type Category = "all" | "security" | "reliability" | "operational_excellence" | "performance";
const CATEGORY_LABEL: Record<string, string> = {
  security: "Security", reliability: "Reliability", operational_excellence: "Operational excellence", performance: "Performance",
};

export default function Security() {
  const [category, setCategory] = useState<Category>("all");
  const [posture, retryPosture] = useTool("governance_security_posture");
  const [advisor, retryAdvisor] = useTool("governance_advisor_recommendations", { category, top_n: 25 });

  return (
    <>
      <PageHeader title="Security & reliability" subtitle="Defender for Cloud and Azure Advisor's view of how secure and resilient this subscription is." />
      <div className="kpis">
        <Kpi label="Secure score" icon="shield" state={posture} accent="brand"
          value={(d) => (d.secure_score_pct != null ? `${d.secure_score_pct}%` : "n/a")}
          sub={(d) => (d.secure_score_current != null ? `${d.secure_score_current} of ${d.secure_score_max} points` : "Defender score unavailable")} />
        <Kpi label="High-severity findings" icon="alert" state={posture} accent="warn"
          value={(d) => String(d.unhealthy_by_severity?.High ?? 0)} sub={() => "affected resources"} />
        <Kpi label="Advisor recommendations" icon="spark" state={advisor} value={(d) => String(d.total)} sub={() => "security, reliability, ops, performance"} />
        <Kpi label="High impact" icon="trends" state={advisor} accent="warn" value={(d) => String(d.high_impact)} />
      </div>

      <div className="grid">
        <DataCard title="Secure score" subtitle="Defender for Cloud" state={posture} onRetry={retryPosture}>
          {(d) => (
            <>
              {d.secure_score_pct != null && <Meter label="Current score" pct={d.secure_score_pct} />}
              <BarList ariaLabel="Unhealthy resources by severity" valueHeader="Resources"
                data={["High", "Medium", "Low"].filter((s) => d.unhealthy_by_severity?.[s]).map((s) => ({ label: `${s} severity`, value: d.unhealthy_by_severity[s], display: String(d.unhealthy_by_severity[s]) }))} />
            </>
          )}
        </DataCard>
        <DataCard span={2} title="Top security recommendations" subtitle="Unhealthy assessments affecting the most resources" state={posture} onRetry={retryPosture}>
          {(d) => (
            <table className="table">
              <thead><tr><th>Severity</th><th>Recommendation</th><th className="num">Resources</th></tr></thead>
              <tbody>
                {(d.findings ?? []).map((f: any, i: number) => (
                  <tr key={i}>
                    <td><span className={`impact impact-${String(f.severity).toLowerCase()}`}>{f.severity}</span></td>
                    <td>{f.recommendation}</td>
                    <td className="num strong">{f.resources}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </DataCard>

        <DataCard span={3} title="Azure Advisor beyond cost" subtitle="Ordered by impact" state={advisor} onRetry={retryAdvisor}
          actions={<Segmented label="Category" value={category} onChange={setCategory}
            options={[{ value: "all", label: "All" }, { value: "security", label: "Security" }, { value: "reliability", label: "Reliability" },
              { value: "operational_excellence", label: "Operations" }, { value: "performance", label: "Performance" }]} />}>
          {(d) => (
            <table className="table">
              <thead><tr><th>Impact</th><th>Category</th><th>Recommendation</th><th>Resource</th></tr></thead>
              <tbody>
                {(d.recommendations ?? []).map((r: any, i: number) => (
                  <tr key={i}>
                    <td><span className={`impact impact-${String(r.impact).toLowerCase()}`}>{r.impact}</span></td>
                    <td><span className="tag">{CATEGORY_LABEL[r.category] ?? r.category}</span></td>
                    <td><strong>{r.solution ?? r.problem}</strong><div className="muted small">{r.problem}</div></td>
                    <td className="mono truncate" title={r.resource}>{r.resource}</td>
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
