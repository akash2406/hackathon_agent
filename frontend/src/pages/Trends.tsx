/** Trends & forecast: daily cost with spike detection, drivers, and Azure's month-end forecast. */
import { useState } from "react";
import { BarList, ColumnChart, formatMoney, type ColumnDatum } from "../components/charts";
import { DataCard, Kpi, PageHeader, Segmented } from "../components/ui";
import { useTool } from "../state";

export default function Trends() {
  const [days, setDays] = useState(30);
  const [trend, retryTrend] = useTool("costpulse_cost_trend", { days });
  const [forecast, retryForecast] = useTool("costpulse_forecast_month_end");

  return (
    <>
      <PageHeader title="Trends & forecast" subtitle="Daily cost, unusual spikes and what caused them, and where the month is heading." />
      <div className="toolbar">
        <Segmented label="Window" value={days} onChange={setDays}
          options={[{ value: 14, label: "14 days" }, { value: 30, label: "30 days" }, { value: 60, label: "60 days" }, { value: 90, label: "90 days" }]} />
      </div>

      <div className="kpis">
        <Kpi label={`Total, last ${days} days`} icon="spend" state={trend} accent="brand" value={(d) => formatMoney(d.total, d.currency)} sub={(d) => `data through ${d.data_through}`} />
        <Kpi label="Typical day" icon="overview" state={trend} value={(d) => formatMoney(d.median_daily_cost, d.currency)} sub={() => "median daily cost"} />
        <Kpi label="Trend" icon="trends" state={trend}
          value={(d) => (d.change_pct_second_half_vs_first == null ? "n/a" : `${d.change_pct_second_half_vs_first > 0 ? "+" : ""}${d.change_pct_second_half_vs_first}%`)}
          sub={() => "second half vs first half"} />
        <Kpi label="Spike days" icon="alert" state={trend} accent="warn" value={(d) => String(d.anomalies?.length ?? 0)} sub={() => "flagged by the anomaly rule"} />
      </div>

      <div className="grid">
        <DataCard span={3} title="Daily cost" subtitle="Hover a column for details" state={trend} onRetry={retryTrend}>
          {(d) => {
            const spikes = new Map<string, any>((d.anomalies ?? []).map((a: any) => [a.date, a]));
            const data: ColumnDatum[] = (d.series ?? []).map((p: any) => ({
              label: p.date,
              value: p.cost,
              tone: spikes.has(p.date) ? "spike" : "base",
              note: spikes.has(p.date) ? `+${formatMoney(spikes.get(p.date).delta, d.currency)} vs typical` : undefined,
            }));
            return (
              <>
                <ColumnChart ariaLabel="Daily cost" currency={d.currency} data={data} reference={{ value: d.median_daily_cost, label: "typical day" }} />
                {(d.anomalies ?? []).length > 0 ? (
                  <div className="spike-grid">
                    {d.anomalies.map((a: any) => (
                      <div key={a.date} className="spike-card">
                        <div className="spike-head"><span className="status-chip critical">▲ Spike</span> <strong>{a.date}</strong></div>
                        <div className="spike-value">{formatMoney(a.cost, d.currency)} <span>vs {formatMoney(a.baseline, d.currency)} typical</span></div>
                        <ul>
                          {(a.drivers ?? []).map((x: any) => (
                            <li key={x.service}><span>{x.service}</span><strong>+{formatMoney(x.delta, d.currency)}</strong></li>
                          ))}
                        </ul>
                      </div>
                    ))}
                  </div>
                ) : (
                  <div className="callout good">No unusual days in this window.</div>
                )}
              </>
            );
          }}
        </DataCard>

        <DataCard span={2} title="Month-end forecast" subtitle="Actual so far plus Azure Cost Management's forecast" state={forecast} onRetry={retryForecast}>
          {(d) => (
            <>
              <div className="inline-stats">
                <div><span>Projected total</span><strong className="big">{d.projected_month_end != null ? formatMoney(d.projected_month_end, d.currency) : "No forecast"}</strong></div>
                <div><span>Actual so far</span><strong>{formatMoney(d.actual_to_date, d.currency)}</strong></div>
                <div><span>Forecast remaining</span><strong>{d.forecast_remaining != null ? formatMoney(d.forecast_remaining, d.currency) : "n/a"}</strong></div>
              </div>
              <ColumnChart ariaLabel={`Daily cost ${d.month}: actual and Azure forecast`} currency={d.currency}
                data={(d.series ?? []).map((p: any) => ({ label: p.date, value: p.cost, tone: p.kind === "forecast" ? "forecast" : "base" }))} />
            </>
          )}
        </DataCard>

        <DataCard title="Top services" subtitle={`Last ${days} days`} state={trend} onRetry={retryTrend}>
          {(d) => (
            <BarList ariaLabel="Cost by service" valueHeader="Cost"
              data={(d.top_services ?? []).map((s: any) => ({ label: s.group, value: s.cost, display: formatMoney(s.cost, s.currency) }))} />
          )}
        </DataCard>
      </div>
    </>
  );
}
