/** Overview for resource-level access (e.g. Azure Reader): security, network, policy and inventory; no cost. */
import { BarList, Meter } from "../components/charts";
import { Icon } from "../components/Icon";
import { DataCard, Kpi, PageHeader } from "../components/ui";
import { useApp, useTool } from "../state";
import { askLater } from "./Ask";

const shortType = (t: string) => t.split("/").slice(-1)[0] ?? t;

export default function PostureOverview() {
  const { navigate } = useApp();
  const [posture, retryPosture] = useTool("governance_security_posture");
  const [network, retryNetwork] = useTool("governance_network_posture");
  const [advisor] = useTool("governance_advisor_recommendations", { top_n: 5 });
  const [policy] = useTool("governance_policy_compliance");
  const [inventory, retryInventory] = useTool("inventory_resource_summary", { tag_key: "owner", top_n: 6 });

  return (
    <>
      <PageHeader
        title="Subscription health"
        subtitle="Your access covers resources, security and network posture. Cost views need a cost role."
        actions={
          <button className="primary" onClick={() => { askLater("How healthy and secure is this subscription, and what should I fix first?"); navigate("/ask"); }}>
            <Icon name="spark" size={16} /> Ask CRIP what to fix first
          </button>
        }
      />
      <div className="kpis">
        <Kpi label="Secure score" icon="shield" state={posture} accent="brand" value={(d) => (d.secure_score_pct != null ? `${d.secure_score_pct}%` : "n/a")} sub={() => "Defender for Cloud"} />
        <Kpi label="Network issues" icon="network" state={network} accent="warn" value={(d) => String(d.issue_count)} sub={() => "exposure checks"} />
        <Kpi label="High-impact Advisor items" icon="alert" state={advisor} value={(d) => String(d.high_impact)} sub={(d) => `${d.total} in total`} />
        <Kpi label="Non-compliant resources" icon="log" state={policy} value={(d) => String(d.non_compliant_resources)} sub={() => "Azure Policy"} />
      </div>
      <div className="grid">
        <DataCard span={2} title="What to fix first" subtitle="Highest-severity security findings" state={posture} onRetry={retryPosture}
          actions={<button className="link small" onClick={() => navigate("/security")}>Security & reliability →</button>}>
          {(d) => (
            <ul className="rec-list">
              {(d.findings ?? []).slice(0, 4).map((f: any, i: number) => (
                <li key={i}>
                  <span className={`impact impact-${String(f.severity).toLowerCase()}`}>{f.severity}</span>
                  <div className="rec-text"><strong>{f.recommendation}</strong><span>{f.resources} resource(s)</span></div>
                  <span />
                </li>
              ))}
            </ul>
          )}
        </DataCard>
        <DataCard title="Network exposure" state={network} onRetry={retryNetwork}
          actions={<button className="link small" onClick={() => navigate("/network")}>Details →</button>}>
          {(d) => (
            <BarList ariaLabel="Network findings" valueHeader="Count"
              data={(d.findings ?? []).filter((f: any) => f.severity !== "Info").map((f: any) => ({ label: f.title, value: f.count, display: String(f.count) }))} />
          )}
        </DataCard>
        <DataCard span={2} title="What runs here" subtitle="Resources by type" state={inventory} onRetry={retryInventory}>
          {(d) => (
            <BarList ariaLabel="Resources by type" valueHeader="Count"
              data={(d.by_type ?? []).map((t: any) => ({ label: shortType(t.group), value: t.count, display: String(t.count) }))} />
          )}
        </DataCard>
        <DataCard title="Ownership tagging" subtitle="Resources with an 'owner' tag" state={inventory} onRetry={retryInventory}>
          {(d) => (d.tag_coverage ? <Meter label="Tagged 'owner'" pct={d.tag_coverage.coverage_pct} /> : <div className="empty-state">No tag data.</div>)}
        </DataCard>
      </div>
    </>
  );
}
