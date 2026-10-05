/**
 * App frame: brand sidebar with role-aware navigation, top bar with
 * subscription picker (showing your access level) and user.
 *
 * Navigation only *hides* what you cannot use; the backend enforces access.
 */
import type { ReactNode } from "react";
import type { DemoPersona } from "../api";
import { useApp, type Route } from "../state";
import { BrandLogo, ProductMark } from "./Brand";
import { Icon } from "./Icon";

interface NavItem { route: Route; label: string; icon: string; hint: string; needs?: "cost" | "admin" }

const INSIGHTS: NavItem[] = [
  { route: "/", label: "Overview", icon: "overview", hint: "Your subscription at a glance" },
  { route: "/spend", label: "Spend", icon: "spend", hint: "Break down cost by any dimension", needs: "cost" },
  { route: "/trends", label: "Trends & forecast", icon: "trends", hint: "Daily cost, spikes, month-end", needs: "cost" },
  { route: "/savings", label: "Savings", icon: "savings", hint: "Advisor and idle resources", needs: "cost" },
  { route: "/security", label: "Security & reliability", icon: "shield", hint: "Secure score, Advisor posture" },
  { route: "/network", label: "Network & policy", icon: "network", hint: "Exposure checks, compliance" },
  { route: "/inventory", label: "Inventory & tags", icon: "inventory", hint: "What runs where, tag coverage" },
  { route: "/ask", label: "Ask CRIP", icon: "ask", hint: "Multi-agent AI assistant" },
];
const ADMIN: NavItem[] = [
  { route: "/admin/estate", label: "Estate overview", icon: "estate", hint: "Every subscription, one view", needs: "admin" },
  { route: "/admin/access", label: "Access review", icon: "users", hint: "Who holds which Azure role", needs: "admin" },
  { route: "/admin/usage", label: "Usage log", icon: "log", hint: "Who uses CRIP, what was denied", needs: "admin" },
  { route: "/admin/settings", label: "Settings & health", icon: "settings", hint: "Access model, agents, Graph", needs: "admin" },
];
export const ALL_NAV = [...INSIGHTS, ...ADMIN];

const LEVEL_LABEL: Record<string, string> = { cost: "Cost + resources", resources: "Resources only" };

export default function Shell({ route, onSignOut, children }: { route: Route; onSignOut?: () => void; children: ReactNode }) {
  const { api, subscriptions, scope, setScope, navigate, userName, level, isAdmin, persona, me } = useApp();
  const initials = userName.split(/\s+/).map((p) => p[0]).filter(Boolean).slice(0, 2).join("").toUpperCase() || "U";
  const current = ALL_NAV.find((n) => n.route === route) ?? INSIGHTS[0];
  const locked = (n: NavItem) => n.needs === "cost" && level !== "cost";

  const item = (n: NavItem) => (
    <a
      key={n.route}
      href={n.route}
      className={`nav-item${n.route === route ? " active" : ""}${locked(n) ? " locked" : ""}`}
      aria-current={n.route === route ? "page" : undefined}
      title={locked(n) ? "Needs cost access on this subscription" : undefined}
      onClick={(e) => {
        e.preventDefault();
        navigate(n.route);
      }}
    >
      <Icon name={n.icon} />
      <span>
        <span className="nav-label">{n.label}{locked(n) && <Icon name="lock" size={13} />}</span>
        <span className="nav-hint">{n.hint}</span>
      </span>
    </a>
  );

  return (
    <div className="shell">
      <aside className="sidebar">
        <div className="sidebar-brand">
          <BrandLogo variant="light" size={30} />
          <ProductMark />
        </div>
        <nav aria-label="Insights">
          <div className="nav-section">Insights</div>
          {INSIGHTS.map(item)}
        </nav>
        {isAdmin && (
          <nav aria-label="Platform admin">
            <div className="nav-section">Platform admin</div>
            {ADMIN.map(item)}
          </nav>
        )}
        <div className="sidebar-foot">
          <div className="trust">
            <Icon name="shield" size={16} />
            <span>
              Read-only. You see what your Azure access allows{me ? ` (${me.access_mode === "user_obo" ? "your own identity" : "checked by CRIP"})` : ""}.
              Usage is logged for platform admins.
            </span>
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
            {persona && (
              <label className="scope-picker">
                <span>View as</span>
                <select value={persona.value} onChange={(e) => persona.set(e.target.value as DemoPersona)}>
                  <option value="admin">Platform admin</option>
                  <option value="cost">Cost viewer</option>
                  <option value="reader">Reader (no cost)</option>
                </select>
              </label>
            )}
            <label className="scope-picker">
              <span>Subscription</span>
              <select value={scope ?? ""} onChange={(e) => setScope(e.target.value)} disabled={!subscriptions || subscriptions.length === 0}>
                {!subscriptions && <option value="">Loading…</option>}
                {subscriptions?.length === 0 && <option value="">No subscriptions available</option>}
                {subscriptions?.map((s) => (
                  <option key={s.subscription_id} value={`/subscriptions/${s.subscription_id}`}>
                    {s.display_name}
                  </option>
                ))}
              </select>
            </label>
            {level && <span className={`access-pill ${level}`} title={subscriptions?.find((s) => `/subscriptions/${s.subscription_id}` === scope)?.via.join(", ")}>{LEVEL_LABEL[level]}</span>}
            <div className="user">
              <span className="avatar" aria-hidden="true">{initials}</span>
              <span className="user-name">{userName}{isAdmin && <span className="admin-badge">Admin</span>}</span>
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
