/**
 * App-wide state: the active API, who the user is and what they may see
 * (/api/me), the selected subscription, a tiny path router, and the hook that
 * loads a grounded tool result for a dashboard card.
 *
 * The UI hides what a user cannot use, but it is never the enforcement point:
 * the backend checks access on every call and answers "Access denied" itself.
 */
import { createContext, useCallback, useContext, useEffect, useState, type ReactNode } from "react";
import { ApiFailure, type Api, type DemoPersona } from "./api";
import type { AccessLevel, AgentResponse, Me, SubscriptionAccess } from "./types";

// --------------------------------------------------------------------------- router

export type Route =
  | "/" | "/spend" | "/trends" | "/savings" | "/inventory" | "/security" | "/network" | "/ask"
  | "/admin/estate" | "/admin/access" | "/admin/usage" | "/admin/settings";
export const ROUTES: Route[] = [
  "/", "/spend", "/trends", "/savings", "/inventory", "/security", "/network", "/ask",
  "/admin/estate", "/admin/access", "/admin/usage", "/admin/settings",
];

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

interface AppState {
  api: Api;
  me: Me | null; // null = loading
  meError: string | null;
  subscriptions: SubscriptionAccess[] | null;
  scope: string | null;
  setScope: (scope: string) => void;
  /** Access level for the selected subscription. */
  level: AccessLevel | null;
  isAdmin: boolean;
  navigate: (to: Route) => void;
  userName: string;
  /** Demo mode only: switch the sample persona to show role-based views. */
  persona?: { value: DemoPersona; set: (p: DemoPersona) => void };
}

const Ctx = createContext<AppState | null>(null);

export function useApp(): AppState {
  const value = useContext(Ctx);
  if (!value) throw new Error("useApp outside AppProvider");
  return value;
}

const SCOPE_KEY = "crip.scope";

export function AppProvider({
  api,
  navigate,
  fallbackName,
  persona,
  children,
}: {
  api: Api;
  navigate: (to: Route) => void;
  fallbackName: string;
  persona?: AppState["persona"];
  children: ReactNode;
}) {
  const [me, setMe] = useState<Me | null>(null);
  const [meError, setMeError] = useState<string | null>(null);
  const [scope, setScopeState] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setMe(null);
    api
      .me()
      .then((m) => {
        if (cancelled) return;
        setMe(m);
        let saved: string | null = null;
        try {
          saved = localStorage.getItem(SCOPE_KEY);
        } catch {
          /* storage unavailable: fine */
        }
        const preferred = m.subscriptions.find((s) => `/subscriptions/${s.subscription_id}` === saved) ?? m.subscriptions[0];
        setScopeState(preferred ? `/subscriptions/${preferred.subscription_id}` : null);
      })
      .catch((err) => {
        if (cancelled) return;
        setMeError(err instanceof ApiFailure ? err.message : String(err));
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

  const current = me?.subscriptions.find((s) => `/subscriptions/${s.subscription_id}` === scope) ?? null;
  return (
    <Ctx.Provider
      value={{
        api,
        me,
        meError,
        subscriptions: me ? me.subscriptions : null,
        scope,
        setScope,
        level: current?.level ?? null,
        isAdmin: !!me?.is_platform_admin,
        navigate,
        userName: me?.user.name ?? fallbackName,
        persona,
      }}
    >
      {children}
    </Ctx.Provider>
  );
}

// --------------------------------------------------------------------------- data

export type ToolState =
  | { state: "idle" }
  | { state: "loading" }
  | { state: "done"; response: AgentResponse }
  | { state: "failed"; failure: ApiFailure };

/**
 * Load one grounded tool result; re-runs when the scope or args change.
 * ``scoped: false`` for estate-wide tools that take no subscription.
 */
export function useTool(name: string, args: Record<string, unknown> = {}, opts: { scoped?: boolean; enabled?: boolean } = {}): [ToolState, () => void] {
  const { api, scope } = useApp();
  const scoped = opts.scoped ?? true;
  const enabled = opts.enabled ?? true;
  const [state, setState] = useState<ToolState>({ state: "idle" });
  const [nonce, setNonce] = useState(0);
  const key = JSON.stringify(args);

  useEffect(() => {
    if (!enabled || (scoped && !scope)) {
      setState({ state: "idle" });
      return;
    }
    let cancelled = false;
    setState({ state: "loading" });
    api
      .callTool(name, scoped ? { scope, ...JSON.parse(key) } : JSON.parse(key))
      .then((response) => !cancelled && setState({ state: "done", response }))
      .catch((err) => !cancelled && setState({ state: "failed", failure: err instanceof ApiFailure ? err : new ApiFailure(String(err), "client_error", null, true) }));
    return () => {
      cancelled = true;
    };
  }, [api, name, scope, key, nonce, scoped, enabled]);

  return [state, () => setNonce((n) => n + 1)];
}

/** The grounded payload if the call succeeded with data (ok/partial), else null. */
export function dataOf(s: ToolState): Record<string, any> | null {
  return s.state === "done" && (s.response.status === "ok" || s.response.status === "partial") ? (s.response.data as Record<string, any>) : null;
}
