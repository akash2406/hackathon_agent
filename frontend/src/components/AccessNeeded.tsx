/** Shown instead of a page the user's access level does not include. The backend would refuse anyway. */
import { useApp } from "../state";
import { Icon } from "./Icon";

export default function AccessNeeded({ kind }: { kind: "cost" | "admin" | "none" }) {
  const { navigate, subscriptions, scope } = useApp();
  const sub = subscriptions?.find((s) => `/subscriptions/${s.subscription_id}` === scope);
  const text = {
    cost: {
      title: "Cost views need cost access",
      body: `Your access to ${sub?.display_name ?? "this subscription"} (${sub?.via.join(", ") || "Reader"}) covers resources, security and network views, but not cost.`,
      how: "Ask for Cost Management Reader (or Contributor/Owner) on the subscription, or membership of the CRIP cost-reader group.",
    },
    admin: {
      title: "Platform admins only",
      body: "This area shows access reviews, the whole estate and CRIP usage.",
      how: "It needs the CRIP.PlatformAdmin app role or membership of the platform group.",
    },
    none: {
      title: "No subscriptions available",
      body: "You don't have access to any subscription CRIP covers.",
      how: "Ask for Reader (or a CRIP role) on a subscription under the platform management group.",
    },
  }[kind];
  return (
    <div className="access-needed">
      <div className="access-icon"><Icon name="lock" size={28} /></div>
      <h1>{text.title}</h1>
      <p>{text.body}</p>
      <p className="muted">{text.how}</p>
      {kind !== "none" && (
        <div className="access-actions">
          <button className="primary" onClick={() => navigate("/security")}><Icon name="shield" size={16} /> Security & reliability</button>
          <button className="secondary" onClick={() => navigate("/network")}>Network & policy</button>
        </div>
      )}
    </div>
  );
}
