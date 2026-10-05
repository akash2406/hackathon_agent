/**
 * Renders one composed answer: the Orchestrator's text, which agents it
 * consulted, a visual per grounded result, and the grounding proof inline.
 *
 * The proof (query used, data timestamp, scope, Azure request id) is visible
 * without any extra click. It comes from response.contributions, which the
 * backend copies verbatim from the tool results, not from the model's prose.
 */
import { useApp } from "../state";
import type { AgentResponse, ChatResponse } from "../types";
import Insights from "./Insights";

const STATUS_LABEL: Record<string, string> = {
  ok: "Grounded",
  partial: "Grounded, partial",
  no_data: "No data",
  error: "Retrieval failed",
};

const AGENT_LABEL: Record<string, string> = {
  costpulse: "CostPulse · spend",
  optimizer: "Optimizer · savings",
  inventory: "Inventory · resources",
  governance: "Governance · posture",
  platform: "Platform · admin",
};

export const agentLabel = (key: string) => AGENT_LABEL[key] ?? key;

// Shown exactly as the backend returned it: rounding or localising it here would
// be a (small) re-interpretation of the provenance.
function describeTimestamp(c: AgentResponse): string {
  if (!c.data_timestamp) return "n/a";
  const through = c.data?.["data_through"];
  return typeof through === "string" ? `${c.data_timestamp} (data through ${through})` : c.data_timestamp;
}

function Contribution({ c }: { c: AgentResponse }) {
  const { api } = useApp();
  return (
    <section className={`card status-${c.status}`}>
      <header className="card-head">
        <span className={`badge status-${c.status}`}>{STATUS_LABEL[c.status] ?? c.status}</span>
        <strong>{agentLabel(c.agent)}</strong>
        <span className="muted">data as of {describeTimestamp(c)}</span>
      </header>
      <Insights c={c} />
      <p className="tool-answer">{c.answer}</p>
      <div className="proof">
        <div className="proof-title">{api.demo ? "Sample data (demo mode): no Azure call was made" : "Grounding proof"}</div>
        <dl>
          {c.sources.map((s, i) => (
            <div key={i} className="source">
              <dt>Azure call</dt>
              <dd>
                <code>{s.scope}</code> with your own permissions · {new Date(s.invoked_at).toLocaleString()} · HTTP{" "}
                {s.http_status ?? "n/a"}
                {s.request_id && (
                  <>
                    {" "}
                    · request id <code>{s.request_id}</code>
                  </>
                )}
              </dd>
            </div>
          ))}
          <dt>Query used</dt>
          <dd>
            <pre className="query">{c.query_used ?? "(no Azure call was made)"}</pre>
          </dd>
          <dt>Confidence</dt>
          <dd>
            {c.confidence.level} ({c.confidence.score.toFixed(2)})
          </dd>
          {c.caveats.length > 0 && (
            <>
              <dt>Caveats</dt>
              <dd>
                <ul>
                  {c.caveats.map((cv, i) => (
                    <li key={i}>{cv}</li>
                  ))}
                </ul>
              </dd>
            </>
          )}
        </dl>
      </div>
    </section>
  );
}

export default function AssistantMessage({
  response,
  followUps,
  onAsk,
}: {
  response: ChatResponse;
  followUps: string[];
  onAsk: (q: string) => void;
}) {
  const agents = Array.from(new Set(response.contributions.map((c) => c.agent)));
  return (
    <div className="bubble assistant">
      <div className="answer-head">
        <span className={`badge status-${response.status}`}>{STATUS_LABEL[response.status] ?? response.status}</span>
        {!response.grounded && <span className="badge ungrounded">Not grounded in Azure data</span>}
        {agents.length > 0 && (
          <span className="muted">
            consulted: {agents.map((a) => agentLabel(a)).join(", ")}
          </span>
        )}
      </div>
      {/* Plain text rendering: model output is never injected as HTML. */}
      <div className="answer">{response.answer}</div>
      {response.caveats.length > 0 && (
        <ul className="caveats">
          {response.caveats.map((c, i) => (
            <li key={i}>{c}</li>
          ))}
        </ul>
      )}
      {response.contributions.map((c, i) => (
        <Contribution key={i} c={c} />
      ))}
      {followUps.length > 0 && (
        <div className="followups">
          {followUps.map((q) => (
            <button key={q} className="chip" onClick={() => onAsk(q)}>
              {q}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
