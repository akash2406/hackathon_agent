import type { AccountInfo } from "@azure/msal-browser";
import { AuthenticatedTemplate, UnauthenticatedTemplate, useMsal } from "@azure/msal-react";
import { useEffect, useMemo, useState } from "react";
import { demoApi, getCapabilities, liveApi, type DemoPersona } from "./api";
import AccessNeeded from "./components/AccessNeeded";
import { BrandLogo, ProductMark } from "./components/Brand";
import { Icon } from "./components/Icon";
import Shell from "./components/Shell";
import { agentLabel } from "./components/AssistantMessage";
import { apiTokenRequest, config } from "./config";
import { AccessReview, Estate, Settings, Usage } from "./pages/Admin";
import Ask from "./pages/Ask";
import Network from "./pages/Network";
import Security from "./pages/Security";
import Inventory from "./pages/Inventory";
import Overview from "./pages/Overview";
import Savings from "./pages/Savings";
import Spend from "./pages/Spend";
import Trends from "./pages/Trends";
import { AppProvider, useApp, useRouter, type Route } from "./state";
import type { AgentCapability } from "./types";

const COST_ROUTES = new Set<Route>(["/spend", "/trends", "/savings"]);

function Page({ route }: { route: Route }) {
  const { me, meError, level, isAdmin, subscriptions } = useApp();
  if (meError) return <div className="state-error" role="alert"><div><strong>CRIP could not load your access.</strong> {meError}</div></div>;
  if (!me) return <div className="skeleton-block" aria-busy="true"><div className="skeleton w60" /><div className="skeleton h120" /></div>;
  if (route.startsWith("/admin/")) {
    if (!isAdmin) return <AccessNeeded kind="admin" />;
    switch (route) {
      case "/admin/access": return <AccessReview />;
      case "/admin/usage": return <Usage />;
      case "/admin/settings": return <Settings />;
      default: return <Estate />;
    }
  }
  if (route !== "/ask" && subscriptions?.length === 0) return <AccessNeeded kind="none" />;
  if (COST_ROUTES.has(route) && level !== "cost") return <AccessNeeded kind="cost" />;
  switch (route) {
    case "/spend": return <Spend />;
    case "/trends": return <Trends />;
    case "/savings": return <Savings />;
    case "/inventory": return <Inventory />;
    case "/security": return <Security />;
    case "/network": return <Network />;
    case "/ask": return <Ask />;
    default: return <Overview />;
  }
}

function SignedIn({ account, route, navigate }: { account: AccountInfo; route: Route; navigate: (r: Route) => void }) {
  const { instance } = useMsal();
  const api = useMemo(() => liveApi(instance, account), [instance, account]);
  return (
    <AppProvider api={api} navigate={navigate} fallbackName={account.name ?? account.username}>
      <Shell route={route} onSignOut={() => instance.logoutRedirect()}>
        <Page route={route} />
      </Shell>
    </AppProvider>
  );
}

function SignIn() {
  const { instance } = useMsal();
  const [capabilities, setCapabilities] = useState<AgentCapability[]>([]);
  useEffect(() => {
    getCapabilities().then((c) => setCapabilities(c.agents)).catch(() => setCapabilities([]));
  }, []);
  return (
    <div className="landing">
      <header className="landing-top">
        <BrandLogo size={34} variant="light" />
        <ProductMark />
      </header>
      <main className="landing-main">
        <section className="landing-hero">
          <span className="eyebrow"><Icon name="spark" size={14} /> AI FinOps copilot for Azure</span>
          <h1>Know where every dollar of your cloud goes, and how to save it.</h1>
          <p>
            Specialist AI agents read live Azure cost, Advisor and inventory data with <strong>your own permissions</strong>,
            and every figure arrives with its proof: the exact query, the source and how fresh it is.
          </p>
          <div className="landing-cta">
            <button className="primary large" onClick={() => instance.loginRedirect(apiTokenRequest)}>Sign in with Microsoft</button>
            <span className="landing-note"><Icon name="shield" size={14} /> Read-only. Nothing in Azure is ever changed.</span>
          </div>
          <ul className="landing-points">
            <li><strong>Grounded</strong><span>No number without an Azure source and timestamp</span></li>
            <li><strong>Your access</strong><span>On-behalf-of: see exactly what your RBAC allows</span></li>
            <li><strong>Multi-agent</strong><span>Spend, savings and inventory specialists, one answer</span></li>
          </ul>
        </section>
        <section className="landing-cards">
          {(capabilities.length ? capabilities : []).map((a) => (
            <div key={a.key} className="landing-card">
              <span className="landing-card-icon"><Icon name={a.key === "optimizer" ? "savings" : a.key === "inventory" ? "inventory" : "trends"} /></span>
              <div>
                <strong>{agentLabel(a.key)}</strong>
                <p>{a.summary}</p>
              </div>
            </div>
          ))}
        </section>
      </main>
    </div>
  );
}

export default function App() {
  const [route, navigate] = useRouter();
  const { instance, accounts } = useMsal();
  const account = instance.getActiveAccount() ?? accounts[0] ?? null;
  // Demo only: ?as=reader|cost|admin picks the starting persona (handy for demo links).
  const [persona, setPersona] = useState<DemoPersona>(() => {
    const as = new URLSearchParams(window.location.search).get("as");
    return as === "reader" || as === "cost" ? as : "admin";
  });
  const sample = useMemo(() => (config.demoMode ? demoApi(persona) : null), [persona]);

  if (sample) {
    return (
      <AppProvider api={sample} navigate={navigate} fallbackName="Demo User" persona={{ value: persona, set: setPersona }}>
        <Shell route={route}>
          <Page route={route} />
        </Shell>
      </AppProvider>
    );
  }
  return (
    <>
      <AuthenticatedTemplate>{account && <SignedIn account={account} route={route} navigate={navigate} />}</AuthenticatedTemplate>
      <UnauthenticatedTemplate>
        <SignIn />
      </UnauthenticatedTemplate>
    </>
  );
}
