/**
 * SAMPLE DATA for the UI preview (config.demoMode). Not from Azure.
 *
 * Only used when the server sets CRIP_UI_DEMO_MODE=true, so designers and
 * judges can see every screen before an Entra app registration exists. Every
 * response is visibly labelled: a banner in the shell, a caveat on every card,
 * and request ids of "demo-sample". The real app never loads this module.
 */
import type { AgentResponse, ChatResponse, Source } from "../types";

const DEMO_CAVEAT = "SAMPLE DATA for UI preview. Not from Azure.";
const SUB_A = "6f1c2d3e-4a5b-4c6d-8e9f-0a1b2c3d4e5f";
const SUB_B = "9a8b7c6d-5e4f-4a3b-8c2d-1e0f9a8b7c6d";
const TODAY = new Date();
const iso = (d: Date) => d.toISOString().slice(0, 10);
const daysAgo = (n: number) => new Date(Date.UTC(TODAY.getUTCFullYear(), TODAY.getUTCMonth(), TODAY.getUTCDate() - n));

// Deterministic "noise" so screenshots are stable.
const wobble = (i: number, amp: number) => Math.round(((Math.sin(i * 1.7) + Math.cos(i * 0.6)) * amp) * 100) / 100;

function source(tool: string, scope: string): Source {
  return {
    tool,
    api: "DEMO: sample data, no Azure call was made",
    invoked_at: new Date().toISOString(),
    scope,
    auth: "user_obo",
    request_id: "demo-sample",
    http_status: null,
  };
}

function ok(tool: string, agent: string, scope: string, answer: string, data: Record<string, unknown>, dataDate: Date, caveats: string[] = []): AgentResponse {
  return {
    agent,
    status: "ok",
    answer,
    confidence: { level: "high", score: 0.9 },
    data,
    query_used: `DEMO ${tool} ${JSON.stringify({ scope })}`,
    data_timestamp: `${iso(dataDate)}T00:00:00Z`,
    sources: [source(tool, scope)],
    caveats: [DEMO_CAVEAT, ...caveats],
  };
}

const scopeOf = (args: Record<string, unknown>) => String(args.scope ?? `/subscriptions/${SUB_A}`);

const GROUPS: Record<string, [string, number][]> = {
  resource_group: [["rg-ai-platform", 3120.4], ["rg-data-lake", 1840.75], ["rg-web-prod", 1265.2], ["rg-aks-shared", 980.6], ["rg-network-hub", 412.3], ["rg-devtest", 233.9], ["rg-monitoring", 118.4]],
  service: [["Azure OpenAI", 2410.1], ["Virtual Machines", 1980.2], ["Storage", 1102.4], ["Azure Kubernetes Service", 890.3], ["Azure Database for PostgreSQL", 744.9], ["Bandwidth", 402.1], ["Log Analytics", 321.5]],
  resource_type: [["microsoft.cognitiveservices/accounts", 2410.1], ["microsoft.compute/virtualmachines", 1980.2], ["microsoft.storage/storageaccounts", 1102.4], ["microsoft.containerservice/managedclusters", 890.3], ["microsoft.dbforpostgresql/flexibleservers", 744.9]],
  tag: [["platform-team", 3340.2], ["data-team", 2101.5], ["(untagged)", 1488.9], ["web-team", 1044.1]],
};

function queryCosts(args: Record<string, unknown>): AgentResponse {
  const scope = scopeOf(args);
  const groupBy = String(args.group_by ?? "resource_group");
  const timeframe = String(args.timeframe ?? "month_to_date");
  const factor = { month_to_date: 1, last_month: 1.9, last_7_days: 0.42, last_30_days: 1.85 }[timeframe] ?? 1;
  const top = (GROUPS[groupBy] ?? GROUPS.resource_group).map(([group, cost]) => ({ group, cost: Math.round(cost * factor * 100) / 100, currency: "AUD" }));
  const total = Math.round(top.reduce((s, g) => s + g.cost, 0) * 100) / 100;
  const through = daysAgo(1);
  return ok("costpulse_query_costs", "costpulse", scope, `Actual cost for ${scope} is ${total.toLocaleString()} AUD across ${top.length} groups.`, {
    kind: "breakdown", scope, timeframe, group_by: groupBy, tag_key: args.tag_key ?? null,
    total_by_currency: { AUD: total }, top, remaining_groups: 0, remaining_cost_by_currency: { AUD: 0 }, data_through: iso(through), row_count: 240,
  }, through, ["Azure Cost Management data typically lags 8-24 hours behind actual usage."]);
}

function costTrend(args: Record<string, unknown>): AgentResponse {
  const scope = scopeOf(args);
  const days = Number(args.days ?? 30);
  const spikeIdx = Math.max(2, days - 12);
  const series = Array.from({ length: days }, (_, i) => {
    const d = daysAgo(days - i);
    const weekday = d.getUTCDay();
    const base = 245 + i * 1.6 + (weekday === 0 || weekday === 6 ? -38 : 0) + wobble(i, 9);
    return { date: iso(d), cost: Math.round((i === spikeIdx ? base + 412 : base) * 100) / 100 };
  });
  const sorted = [...series.map((s) => s.cost)].sort((a, b) => a - b);
  const median = sorted[Math.floor(sorted.length / 2)];
  const spike = series[spikeIdx];
  const total = Math.round(series.reduce((s, p) => s + p.cost, 0) * 100) / 100;
  return ok("costpulse_cost_trend", "costpulse", scope, `Daily cost over ${days} days: one anomalous day (${spike.date}).`, {
    kind: "trend", scope, currency: "AUD", series,
    anomalies: [{ date: spike.date, cost: spike.cost, baseline: median, delta: Math.round((spike.cost - median) * 100) / 100,
      drivers: [{ service: "Azure OpenAI", cost: 455.2, typical: 61.4, delta: 393.8 }, { service: "Bandwidth", cost: 41.9, typical: 18.2, delta: 23.7 }] }],
    median_daily_cost: median, total, change_pct_second_half_vs_first: 8.4,
    top_services: GROUPS.service.slice(0, 5).map(([group, cost]) => ({ group, cost, currency: "AUD" })),
    data_through: series[series.length - 1].date,
  }, daysAgo(1), ["Anomaly rule: > 3 scaled MADs above the window median and at least 30% above it."]);
}

function forecast(args: Record<string, unknown>): AgentResponse {
  const scope = scopeOf(args);
  const year = TODAY.getUTCFullYear(), month = TODAY.getUTCMonth();
  const daysInMonth = new Date(Date.UTC(year, month + 1, 0)).getUTCDate();
  const elapsed = Math.max(1, TODAY.getUTCDate() - 1);
  const series = Array.from({ length: daysInMonth }, (_, i) => ({
    date: iso(new Date(Date.UTC(year, month, i + 1))),
    cost: Math.round((262 + wobble(i, 11) + (i >= elapsed ? 6 : 0)) * 100) / 100,
    kind: i < elapsed ? "actual" : "forecast",
  }));
  const actual = series.filter((s) => s.kind === "actual").reduce((s, p) => s + p.cost, 0);
  const fc = series.filter((s) => s.kind === "forecast").reduce((s, p) => s + p.cost, 0);
  const r = (v: number) => Math.round(v * 100) / 100;
  return ok("costpulse_forecast_month_end", "costpulse", scope, `Projected month-end total ${r(actual + fc)} AUD (Azure forecast).`, {
    kind: "forecast", scope, month: `${year}-${String(month + 1).padStart(2, "0")}`, currency: "AUD",
    actual_to_date: r(actual), forecast_remaining: r(fc), projected_month_end: r(actual + fc), series, data_through: series[elapsed - 1].date,
  }, daysAgo(1), ["Forecast figures are Azure Cost Management's own forecast, not CRIP's."]);
}

function advisor(args: Record<string, unknown>): AgentResponse {
  const scope = scopeOf(args);
  const recommendations = [
    { problem: "Right-size or shutdown underutilized virtual machines", solution: "Resize vm-build-agent-01 to Standard_D2s_v5", impact: "High", resource: "vm-build-agent-01", annual_savings: 4380, currency: "AUD" },
    { problem: "Buy reserved instances to save money over pay-as-you-go", solution: "Reserve 3 x Standard_E4s_v5 for 1 year", impact: "High", resource: "vmss-api-prod", annual_savings: 3910, currency: "AUD" },
    { problem: "Right-size or shutdown underutilized virtual machines", solution: "Shut down vm-legacy-report", impact: "Medium", resource: "vm-legacy-report", annual_savings: 2160, currency: "AUD" },
    { problem: "Consider Cosmos DB autoscale", solution: "Enable autoscale on cosmos-orders", impact: "Medium", resource: "cosmos-orders", annual_savings: 1240, currency: "AUD" },
    { problem: "Use Standard Storage for unmanaged disk snapshots", solution: "Move snapshots to Standard HDD", impact: "Low", resource: "snap-archive-2024", annual_savings: 310, currency: "AUD" },
  ];
  const byProblem = new Map<string, { problem: string; count: number; annual_savings: number; currency: string }>();
  for (const r of recommendations) {
    const e = byProblem.get(r.problem) ?? { problem: r.problem, count: 0, annual_savings: 0, currency: "AUD" };
    e.count += 1; e.annual_savings += r.annual_savings; byProblem.set(r.problem, e);
  }
  return ok("optimizer_advisor_recommendations", "optimizer", scope, "Azure Advisor lists 5 cost recommendations.", {
    kind: "savings", scope, total_annual_savings_by_currency: { AUD: 12000 },
    by_problem: [...byProblem.values()].sort((a, b) => b.annual_savings - a.annual_savings), recommendations, recommendation_count: 5,
  }, daysAgo(0), ["Savings amounts are Azure Advisor's estimates, not CRIP's."]);
}

function idle(args: Record<string, unknown>): AgentResponse {
  const scope = scopeOf(args);
  const resources = [
    { name: "disk-old-sqlvm-data", category: "Unattached managed disk", resource_group: "rg-data-lake", location: "australiaeast", sku: "Premium_LRS", cost_mtd: 186.4, currency: "AUD" },
    { name: "vm-poc-genai-01", category: "VM stopped but not deallocated (compute still billed)", resource_group: "rg-devtest", location: "australiaeast", sku: "Standard_NC6s_v3", cost_mtd: 412.9, currency: "AUD" },
    { name: "disk-migration-temp", category: "Unattached managed disk", resource_group: "rg-devtest", location: "australiasoutheast", sku: "Standard_LRS", cost_mtd: 22.1, currency: "AUD" },
    { name: "pip-legacy-gateway", category: "Unassociated public IP address", resource_group: "rg-network-hub", location: "australiaeast", sku: "Standard", cost_mtd: 9.8, currency: "AUD" },
    { name: "asp-retired-portal", category: "Empty App Service plan", resource_group: "rg-web-prod", location: "australiaeast", sku: "P1v3", cost_mtd: 118.3, currency: "AUD" },
    { name: "nic-orphan-07", category: "Orphaned network interface", resource_group: "rg-devtest", location: "australiaeast", sku: null, cost_mtd: null, currency: null },
  ].sort((a, b) => (b.cost_mtd ?? 0) - (a.cost_mtd ?? 0));
  const cats = new Map<string, { category: string; count: number; cost_mtd: number }>();
  for (const r of resources) {
    const e = cats.get(r.category) ?? { category: r.category, count: 0, cost_mtd: 0 };
    e.count += 1; e.cost_mtd = Math.round((e.cost_mtd + (r.cost_mtd ?? 0)) * 100) / 100; cats.set(r.category, e);
  }
  return ok("optimizer_find_idle_resources", "optimizer", scope, "Found 6 idle resources costing 749.50 AUD month-to-date.", {
    kind: "idle_resources", scope, resource_count: resources.length, cost_mtd_by_currency: { AUD: 749.5 },
    by_category: [...cats.values()].sort((a, b) => b.cost_mtd - a.cost_mtd), resources,
  }, daysAgo(1), ["Review before deleting: some may be kept on purpose."]);
}

function inventory(args: Record<string, unknown>): AgentResponse {
  const scope = scopeOf(args);
  const tagKey = args.tag_key ? String(args.tag_key) : null;
  return ok("inventory_resource_summary", "inventory", scope, "Scope contains 486 resources across 41 types and 3 locations.", {
    kind: "inventory", scope, total_resources: 486, resource_type_count: 41,
    by_type: [
      { group: "microsoft.network/networkinterfaces", count: 74 }, { group: "microsoft.compute/disks", count: 69 },
      { group: "microsoft.compute/virtualmachines", count: 52 }, { group: "microsoft.storage/storageaccounts", count: 38 },
      { group: "microsoft.web/sites", count: 31 }, { group: "microsoft.insights/components", count: 22 },
      { group: "microsoft.keyvault/vaults", count: 19 }, { group: "microsoft.network/publicipaddresses", count: 17 },
    ],
    by_location: [{ group: "australiaeast", count: 401 }, { group: "australiasoutheast", count: 71 }, { group: "global", count: 14 }],
    ...(tagKey
      ? { tag_coverage: { tag_key: tagKey, tagged: 331, untagged: 155, coverage_pct: 68.1,
          worst_resource_groups: [{ resource_group: "rg-devtest", total: 88, untagged: 61 }, { resource_group: "rg-network-hub", total: 57, untagged: 39 }, { resource_group: "rg-data-lake", total: 64, untagged: 22 }] } }
      : {}),
  }, daysAgo(0));
}

function subscriptions(): AgentResponse {
  return ok("costpulse_list_subscriptions", "costpulse", "/subscriptions", "You can see 2 subscriptions.", {
    kind: "subscriptions",
    subscriptions: [
      { subscription_id: SUB_A, display_name: "Hackathon Production (sample)", state: "Enabled" },
      { subscription_id: SUB_B, display_name: "Hackathon Dev/Test (sample)", state: "Enabled" },
    ],
  }, daysAgo(0));
}

export function sampleTool(name: string, args: Record<string, unknown>): AgentResponse {
  switch (name) {
    case "costpulse_query_costs": return queryCosts(args);
    case "costpulse_cost_trend": return costTrend(args);
    case "costpulse_forecast_month_end": return forecast(args);
    case "costpulse_list_subscriptions": return subscriptions();
    case "optimizer_advisor_recommendations": return advisor(args);
    case "optimizer_find_idle_resources": return idle(args);
    case "inventory_resource_summary": return inventory(args);
    default: throw new Error(`No sample data for ${name}`);
  }
}

export function sampleChat(message: string): ChatResponse {
  const scope = { scope: `/subscriptions/${SUB_A}` };
  const contributions = [forecast(scope), costTrend({ ...scope, days: 30 }), advisor(scope), idle(scope)];
  return {
    session_id: "demo-session",
    message_id: crypto.randomUUID(),
    status: "ok",
    grounded: true,
    created_at: new Date().toISOString(),
    caveats: [DEMO_CAVEAT],
    contributions,
    answer:
      `(Sample answer: in the real app this is composed by the Orchestrator from live Azure data. You asked: "${message}")\n\n` +
      "You're on track for about AUD 8,240 this month (Azure's forecast). One spike stands out: Azure OpenAI drove an extra ~AUD 390 on a single day.\n\n" +
      "Where to save:\n• Azure Advisor estimates AUD 12,000/year, led by right-sizing vm-build-agent-01 and reserving the API scale set.\n" +
      "• 6 idle resources cost AUD 749.50 so far this month; the stopped-but-not-deallocated GPU VM vm-poc-genai-01 is the biggest.",
  };
}
