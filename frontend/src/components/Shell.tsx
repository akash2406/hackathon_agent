/** App frame: brand sidebar with navigation, top bar with subscription picker and user. */
import type { ReactNode } from "react";
import { useApp, type Route } from "../state";
import { BrandLogo, ProductMark } from "./Brand";
import { Icon } from "./Icon";

const NAV: { route: Route; label: string; icon: string; hint: string }[] = [
  { route: "/", label: "Overview", icon: "overview", hint: "Spend, forecast and savings at a glance" },
  { route: "/spend", label: "Spend", icon: "spend", hint: "Break down cost by any dimension" },
  { route: "/trends", label: "Trends & forecast", icon: "trends", hint: "Daily cost, spikes, month-end" },
  { route: "/savings", label: "Savings", icon: "savings", hint: "Advisor and idle resources" },
  { route: "/inventory", label: "Inventory & tags", icon: "inventory", hint: "What runs where, tag coverage" },
  { route: "/ask", label: "Ask CRIP", icon: "ask", hint: "Multi-agent AI assistant" },
];

export default function Shell({ route, onSignOut, children }: { route: Route; onSignOut?: () => void; children: ReactNode }) {
  const { api, subscriptions, scope, setScope, navigate, userName } = useApp();
  const initials = userName.split(/\s+/).map((p) => p[0]).filter(Boolean).slice(0, 2).join("").toUpperCase() || "U";
  const current = NAV.find((n) => n.route === route) ?? NAV[0];

  return (
    <div className="shell">
      <aside className="sidebar">
        <div className="sidebar-brand">
          <BrandLogo variant="light" size={30} />
          <ProductMark />
        </div>
        <nav aria-label="Main">
          {NAV.map((n) => (
            <a
              key={n.route}
              href={n.route}
              className={`nav-item${n.route === route ? " active" : ""}`}
              aria-current={n.route === route ? "page" : undefined}
              onClick={(e) => {
                e.preventDefault();
                navigate(n.route);
              }}
            >
              <Icon name={n.icon} />
              <span>
                <span className="nav-label">{n.label}</span>
                <span className="nav-hint">{n.hint}</span>
              </span>
            </a>
          ))}
        </nav>
        <div className="sidebar-foot">
          <div className="trust">
            <Icon name="shield" size={16} />
            <span>Read-only. Every figure is queried with your own Azure permissions and shows its source.</span>
          </div>
        </div>
      </aside>

      <div className="main">
        {api.demo && (
          <div className="demo-banner" role="status">
            <strong>Demo mode:</strong> sample data for previewing the interface. Nothing on this screen comes from Azure.
          </div>
        )}
        <header className="topbar">
          <div className="topbar-title">
            <span className="mobile-logo"><BrandLogo size={22} /></span>
            <span className="crumb">{current.label}</span>
          </div>
          <div className="topbar-actions">
            <label className="scope-picker">
              <span>Subscription</span>
              <select
                value={scope ?? ""}
                onChange={(e) => setScope(e.target.value)}
                disabled={!subscriptions || subscriptions.length === 0}
              >
                {!subscriptions && <option value="">Loading…</option>}
                {subscriptions?.length === 0 && <option value="">No subscriptions visible</option>}
                {subscriptions?.map((s) => (
                  <option key={s.subscription_id} value={`/subscriptions/${s.subscription_id}`}>
                    {s.display_name}
                  </option>
                ))}
              </select>
            </label>
            <div className="user">
              <span className="avatar" aria-hidden="true">{initials}</span>
              <span className="user-name">{userName}</span>
              {onSignOut && (
                <button className="icon-button" onClick={onSignOut} title="Sign out" aria-label="Sign out">
                  <Icon name="logout" />
                </button>
              )}
            </div>
          </div>
        </header>
        <main className={`content${route === "/ask" ? " content-chat" : ""}`}>{children}</main>
      </div>
    </div>
  );
}
