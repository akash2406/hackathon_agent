/**
 * App-wide state: which API implementation is active, the selected Azure
 * subscription, a tiny path router, and the hook that loads a grounded tool
 * result for a dashboard card.
 */
import { createContext, useCallback, useContext, useEffect, useState, type ReactNode } from "react";
import { ApiFailure, type Api } from "./api";
import type { AgentResponse } from "./types";

// --------------------------------------------------------------------------- router

export type Route = "/" | "/spend" | "/trends" | "/savings" | "/inventory" | "/ask";
export const ROUTES: Route[] = ["/", "/spend", "/trends", "/savings", "/inventory", "/ask"];

function currentRoute(): Route {
  const path = window.location.pathname.replace(/\/+$/, "") || "/";
  return (ROUTES as string[]).includes(path) ? (path as Route) : "/";
}

export function useRouter(): [Route, (to: Route) => void] {
  const [route, setRoute] = useState<Route>(currentRoute);
  useEffect(() => {
    const onPop = () => setRoute(currentRoute());
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, []);
  const navigate = useCallback((to: Route) => {
    if (to !== currentRoute()) window.history.pushState(null, "", to);
    setRoute(to);
    document.querySelector(".content")?.scrollTo({ top: 0 });
  }, []);
  return [route, navigate];
}

// --------------------------------------------------------------------------- context

export interface Subscription {
  subscription_id: string;
  display_name: string;
  state: string;
}

interface AppState {
  api: Api;
  subscriptions: Subscription[] | null; // null = loading
  subscriptionError: string | null;
  scope: string | null;
  setScope: (scope: string) => void;
  navigate: (to: Route) => void;
  userName: string;
}

const Ctx = createContext<AppState | null>(null);

export function useApp(): AppState {
  const value = useContext(Ctx);
  if (!value) throw new Error("useApp outside AppProvider");
  return value;
}

const SCOPE_KEY = "crip.scope";

export function AppProvider({ api, navigate, userName, children }: { api: Api; navigate: (to: Route) => void; userName: string; children: ReactNode }) {
  const [subscriptions, setSubscriptions] = useState<Subscription[] | null>(null);
  const [subscriptionError, setSubscriptionError] = useState<string | null>(null);
  const [scope, setScopeState] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    api
      .callTool("costpulse_list_subscriptions", {})
      .then((r) => {
        if (cancelled) return;
        const subs = ((r.data?.["subscriptions"] as Subscription[] | undefined) ?? []).filter((s) => s.subscription_id);
        setSubscriptions(subs);
        if (r.status === "error") setSubscriptionError(r.answer);
        let saved: string | null = null;
        try {
          saved = localStorage.getItem(SCOPE_KEY);
        } catch {
          /* storage unavailable: fine */
        }
        const preferred = subs.find((s) => `/subscriptions/${s.subscription_id}` === saved) ?? subs[0];
        if (preferred) setScopeState(`/subscriptions/${preferred.subscription_id}`);
      })
      .catch((err) => {
        if (cancelled) return;
        setSubscriptions([]);
        setSubscriptionError(err instanceof ApiFailure ? err.message : String(err));
      });
    return () => {
      cancelled = true;
    };
  }, [api]);

  const setScope = useCallback((s: string) => {
    setScopeState(s);
    try {
      localStorage.setItem(SCOPE_KEY, s);
    } catch {
      /* storage unavailable: fine */
    }
  }, []);

  return (
    <Ctx.Provider value={{ api, subscriptions, subscriptionError, scope, setScope, navigate, userName }}>{children}</Ctx.Provider>
  );
}

// --------------------------------------------------------------------------- data

export type ToolState =
  | { state: "idle" }
  | { state: "loading" }
  | { state: "done"; response: AgentResponse }
  | { state: "failed"; failure: ApiFailure };

/** Load one grounded tool result for the current scope; re-runs when the scope or args change. */
export function useTool(name: string, args: Record<string, unknown> = {}): [ToolState, () => void] {
  const { api, scope } = useApp();
  const [state, setState] = useState<ToolState>({ state: "idle" });
  const [nonce, setNonce] = useState(0);
  const key = JSON.stringify(args);

  useEffect(() => {
    if (!scope) {
      setState({ state: "idle" });
      return;
    }
    let cancelled = false;
    setState({ state: "loading" });
    api
      .callTool(name, { scope, ...JSON.parse(key) })
      .then((response) => !cancelled && setState({ state: "done", response }))
      .catch((err) => !cancelled && setState({ state: "failed", failure: err instanceof ApiFailure ? err : new ApiFailure(String(err), "client_error", null, true) }));
    return () => {
      cancelled = true;
    };
  }, [api, name, scope, key, nonce]);

  return [state, () => setNonce((n) => n + 1)];
}

/** The grounded payload if the call succeeded with data (ok/partial), else null. */
export function dataOf(s: ToolState): Record<string, any> | null {
  return s.state === "done" && (s.response.status === "ok" || s.response.status === "partial") ? (s.response.data as Record<string, any>) : null;
}
