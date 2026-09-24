/**
 * Renders one composed answer with its grounding shown inline.
 *
 * The citation (query used, data timestamp, scope, Azure request id) is visible
 * without any extra click: for this build the grounding proof is the point.
 * Citations come from response.contributions, which the backend copies verbatim
 * from the tool results, not from the model's prose.
 */
import type { AgentResponse, ChatResponse } from "../types";

const STATUS_LABEL: Record<string, string> = {
  ok: "Grounded",
  partial: "Grounded, partial",
  no_data: "No data",
  error: "Retrieval failed",
};

// Shown exactly as the backend returned it: rounding or localising it here would
// be a (small) re-interpretation of the provenance.
function describeTimestamp(c: AgentResponse): string {
  if (!c.data_timestamp) return "n/a";
  const through = c.data?.["data_through"];
  return typeof through === "string"
    ? `${c.data_timestamp} (latest billing day present in the data: ${through})`
    : c.data_timestamp;
}

function Citation({ c }: { c: AgentResponse }) {
  return (
    <div className={`citation status-${c.status}`}>
      <div className="citation-head">
        <span className={`badge status-${c.status}`}>{STATUS_LABEL[c.status] ?? c.status}</span>
        <span>
          <strong>{c.agent}</strong> · confidence {c.confidence.level} ({c.confidence.score.toFixed(2)})
        </span>
      </div>
      <dl>
        <dt>Data timestamp</dt>
        <dd>{describeTimestamp(c)}</dd>
        {c.sources.map((s, i) => (
          <div key={i} className="source">
            <dt>Scope</dt>
            <dd>
              <code>{s.scope}</code> (queried with your own Azure permissions)
            </dd>
            <dt>Called</dt>
            <dd>
              {new Date(s.invoked_at).toLocaleString()} · HTTP {s.http_status ?? "n/a"}
              {s.request_id && <> · x-ms-request-id <code>{s.request_id}</code></>}
            </dd>
          </div>
        ))}
        <dt>Query used</dt>
        <dd>
          <pre className="query">{c.query_used ?? "(no Azure call was made)"}</pre>
        </dd>
        <dt>Tool result</dt>
        <dd>{c.answer}</dd>
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
  );
}

export default function AssistantMessage({ response }: { response: ChatResponse }) {
  return (
    <div className="bubble assistant">
      <div className="answer-head">
        <span className={`badge status-${response.status}`}>{STATUS_LABEL[response.status] ?? response.status}</span>
        {!response.grounded && <span className="badge ungrounded">Not grounded in Azure data</span>}
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
        <Citation key={i} c={c} />
      ))}
    </div>
  );
}
