import type { AccountInfo } from "@azure/msal-browser";
import { AuthenticatedTemplate, UnauthenticatedTemplate, useMsal } from "@azure/msal-react";
import { useEffect, useMemo, useState } from "react";
import { demoApi, getCapabilities, liveApi } from "./api";
import { BrandLogo, ProductMark } from "./components/Brand";
import { Icon } from "./components/Icon";
import Shell from "./components/Shell";
import { agentLabel } from "./components/AssistantMessage";
import { apiTokenRequest, config } from "./config";
import Ask from "./pages/Ask";
import Inventory from "./pages/Inventory";
import Overview from "./pages/Overview";
import Savings from "./pages/Savings";
import Spend from "./pages/Spend";
import Trends from "./pages/Trends";
import { AppProvider, useRouter, type Route } from "./state";
import type { AgentCapability } from "./types";

function Page({ route }: { route: Route }) {
  switch (route) {
    case "/spend": return <Spend />;
    case "/trends": return <Trends />;
    case "/savings": return <Savings />;
    case "/inventory": return <Inventory />;
    case "/ask": return <Ask />;
    default: return <Overview />;
  }
}

function SignedIn({ account, route, navigate }: { account: AccountInfo; route: Route; navigate: (r: Route) => void }) {
  const { instance } = useMsal();
  const api = useMemo(() => liveApi(instance, account), [instance, account]);
  return (
    <AppProvider api={api} navigate={navigate} userName={account.name ?? account.username}>
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
  const sample = useMemo(() => (config.demoMode ? demoApi() : null), []);

  if (sample) {
    return (
      <AppProvider api={sample} navigate={navigate} userName="Demo User">
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
