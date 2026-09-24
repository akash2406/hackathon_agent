import { EventType, PublicClientApplication, type AuthenticationResult } from "@azure/msal-browser";
import { MsalProvider } from "@azure/msal-react";
import { StrictMode, type ComponentType } from "react";
import { createRoot } from "react-dom/client";
import "./styles.css";

async function start() {
  const root = createRoot(document.getElementById("root")!);
  // config.ts validates window.CRIP_CONFIG when first imported; import it (and
  // everything that depends on it) dynamically so a misconfiguration renders a
  // readable message instead of a blank page.
  let msal: PublicClientApplication;
  let App: ComponentType;
  try {
    const { msalConfig } = await import("./config");
    App = (await import("./App")).default;
    msal = new PublicClientApplication(msalConfig);
    await msal.initialize();
  } catch (err) {
    root.render(<pre className="fatal">CRIP cannot start: {String(err)}</pre>);
    return;
  }

  const accounts = msal.getAllAccounts();
  if (!msal.getActiveAccount() && accounts.length > 0) msal.setActiveAccount(accounts[0]);
  msal.addEventCallback((event) => {
    if (event.eventType === EventType.LOGIN_SUCCESS && event.payload) {
      msal.setActiveAccount((event.payload as AuthenticationResult).account);
    }
  });

  root.render(
    <StrictMode>
      <MsalProvider instance={msal}>
        <App />
      </MsalProvider>
    </StrictMode>,
  );
}

void start();
