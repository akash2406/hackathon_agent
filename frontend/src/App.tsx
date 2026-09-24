import { AuthenticatedTemplate, UnauthenticatedTemplate, useMsal } from "@azure/msal-react";
import { apiTokenRequest } from "./config";
import Chat from "./components/Chat";

export default function App() {
  const { instance, accounts } = useMsal();
  const account = instance.getActiveAccount() ?? accounts[0] ?? null;

  return (
    <div className="app">
      <header className="topbar">
        <div>
          <strong>CRIP</strong> <span className="muted">Cloud Resource Intelligence Platform: Azure cost, grounded</span>
        </div>
        <AuthenticatedTemplate>
          <span className="muted">{account?.name ?? account?.username}</span>{" "}
          <button className="link" onClick={() => instance.logoutRedirect()}>
            Sign out
          </button>
        </AuthenticatedTemplate>
      </header>

      <AuthenticatedTemplate>{account && <Chat account={account} />}</AuthenticatedTemplate>

      <UnauthenticatedTemplate>
        <main className="signin">
          <p>Sign in with your organisation account. Answers are scoped to what your own Azure permissions allow.</p>
          <button onClick={() => instance.loginRedirect(apiTokenRequest)}>Sign in</button>
        </main>
      </UnauthenticatedTemplate>
    </div>
  );
}
