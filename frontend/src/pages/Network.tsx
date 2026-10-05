/** Network & policy: exposure checks and Azure Policy compliance. Available at the resources level. */
import { BarList } from "../components/charts";
import { Icon } from "../components/Icon";
import { DataCard, Kpi, PageHeader } from "../components/ui";
import { useTool } from "../state";

export default function Network() {
  const [network, retryNetwork] = useTool("governance_network_posture");
  const [policy, retryPolicy] = useTool("governance_policy_compliance");
  const count = (d: Record<string, any>, check: string) => String(d.findings?.find((f: any) => f.check === check)?.count ?? 0);

  return (
    <>
      <PageHeader title="Network & policy" subtitle="Configuration checks from Azure Resource Graph and compliance from Azure Policy." />
      <div className="kpis">
        <Kpi label="Network issues" icon="network" state={network} accent="warn" value={(d) => String(d.issue_count)} sub={() => "excluding informational"} />
        <Kpi label="SSH/RDP open to internet" icon="alert" state={network} value={(d) => count(d, "open_management_ports")} />
        <Kpi label="Public IP addresses" icon="external" state={network} value={(d) => count(d, "public_ip_addresses")} />
        <Kpi label="Non-compliant resources" icon="shield" state={policy} accent="brand" value={(d) => String(d.non_compliant_resources)} sub={(d) => `${d.assignment_count} policy assignments`} />
      </div>

      <div className="grid">
        <DataCard span={3} title="Network exposure checks" subtitle="Deterministic checks on configuration; some findings may be intentional" state={network} onRetry={retryNetwork}>
          {(d) => (
            <div className="finding-grid">
              {(d.findings ?? []).map((f: any) => (
                <div key={f.check} className={`finding sev-${String(f.severity).toLowerCase()}`}>
                  <div className="finding-head">
                    <span className={`impact impact-${String(f.severity).toLowerCase()}`}>{f.severity}</span>
                    <span className="finding-count">{f.count}</span>
                  </div>
                  <strong>{f.title}</strong>
                  {f.count === 0 ? (
                    <p className="ok-line"><Icon name="shield" size={14} /> None found</p>
                  ) : (
                    <ul>
                      {(f.items ?? []).slice(0, 4).map((it: any, i: number) => (
                        <li key={i}><span className="mono">{it.name}</span><span className="muted"> · {it.resource_group}{it.detail ? ` · ${it.detail}` : ""}</span></li>
                      ))}
                      {f.count > 4 && f.items?.length > 0 && <li className="muted">+{f.count - 4} more</li>}
                    </ul>
                  )}
                </div>
              ))}
            </div>
          )}
        </DataCard>

        <DataCard span={2} title="Policy compliance by assignment" subtitle="Azure Policy, latest evaluation" state={policy} onRetry={retryPolicy}>
          {(d) => (
            <BarList ariaLabel="Non-compliant resources by policy assignment" valueHeader="Non-compliant"
              data={(d.by_assignment ?? []).map((r: any) => ({ label: r.assignment, value: r.non_compliant_resources, display: String(r.non_compliant_resources) }))} />
          )}
        </DataCard>
        <section className="panel ask-panel">
          <div className="panel-head"><div><h2>Why these checks?</h2><p>Common findings in cloud security reviews.</p></div></div>
          <ul className="explain">
            <li><strong>Open SSH/RDP</strong>: the most scanned ports on the internet; use Bastion or just-in-time access.</li>
            <li><strong>Subnets without NSG</strong>: no network-level filtering for anything deployed there.</li>
            <li><strong>Storage open to all networks</strong>: reachable from anywhere with a key or SAS.</li>
            <li><strong>Broken peerings</strong>: hub-spoke routing silently fails.</li>
          </ul>
        </section>
      </div>
    </>
  );
}
