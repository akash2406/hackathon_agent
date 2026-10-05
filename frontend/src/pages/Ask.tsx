/** Ask CRIP: the multi-agent assistant. The Orchestrator picks which specialists answer. */
import { useEffect, useRef, useState, type FormEvent, type KeyboardEvent } from "react";
import { ApiFailure, getCapabilities } from "../api";
import AssistantMessage, { agentLabel } from "../components/AssistantMessage";
import { Icon } from "../components/Icon";
import { useApp } from "../state";
import type { AgentCapability, ChatResponse } from "../types";

const PENDING_KEY = "crip.pendingQuestion";

/** Queue a question for the Ask page (used by "Ask CRIP" buttons on other pages). */
export function askLater(question: string) {
  try {
    sessionStorage.setItem(PENDING_KEY, question);
  } catch {
    /* storage unavailable: the user can type it */
  }
}

type Entry =
  | { kind: "user"; id: string; text: string }
  | { kind: "assistant"; id: string; response: ChatResponse; question: string }
  | { kind: "error"; id: string; failure: ApiFailure };

const SHOWCASE = [
  "Give me a cost overview: what I've spent, what month-end will be, and where I can save.",
  "Why did my costs change in the last 30 days, and what can I do about it?",
];
const PROGRESS = ["Routing your question to the right specialists…", "Querying Azure with your permissions…", "Composing a grounded answer…"];

function followUpsFor(response: ChatResponse, asked: string, capabilities: AgentCapability[]): string[] {
  const consulted = new Set(response.contributions.map((c) => c.agent));
  const others = capabilities.filter((a) => !consulted.has(a.key)).flatMap((a) => a.examples.slice(0, 1));
  const same = capabilities.filter((a) => consulted.has(a.key)).flatMap((a) => a.examples);
  return [...others, ...same].filter((q) => q !== asked).slice(0, 3);
}

export default function Ask() {
  const { api, isAdmin, level } = useApp();
  const [allCapabilities, setCapabilities] = useState<AgentCapability[]>([]);
  // Suggest only what this user can actually get answers for (the backend enforces it anyway).
  const capabilities = allCapabilities.filter(
    (a) => (a.key !== "platform" || isAdmin) && (level === "cost" || !["costpulse", "optimizer"].includes(a.key)),
  );
  const [entries, setEntries] = useState<Entry[]>([]);
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const [progress, setProgress] = useState(0);
  const bottom = useRef<HTMLDivElement>(null);
  const started = useRef(false);

  useEffect(() => {
    getCapabilities().then((c) => setCapabilities(c.agents)).catch(() => setCapabilities([]));
  }, []);
  useEffect(() => bottom.current?.scrollIntoView({ behavior: "smooth" }), [entries, busy]);
  useEffect(() => {
    if (!busy) return;
    setProgress(0);
    const t = setInterval(() => setProgress((p) => Math.min(p + 1, PROGRESS.length - 1)), 4000);
    return () => clearInterval(t);
  }, [busy]);

  async function send(text: string) {
    const message = text.trim();
    if (!message || busy) return;
    setDraft("");
    setBusy(true);
    setEntries((e) => [...e, { kind: "user", id: crypto.randomUUID(), text: message }]);
    try {
      const response = await api.chat(message, sessionId);
      setSessionId(response.session_id);
      setEntries((e) => [...e, { kind: "assistant", id: response.message_id, response, question: message }]);
    } catch (err) {
      const failure = err instanceof ApiFailure ? err : new ApiFailure(String(err), "client_error", null, true);
      setEntries((e) => [...e, { kind: "error", id: crypto.randomUUID(), failure }]);
    } finally {
      setBusy(false);
    }
  }

  // A question queued from another page is asked once on arrival.
  useEffect(() => {
    if (started.current) return;
    started.current = true;
    let pending: string | null = null;
    try {
      pending = sessionStorage.getItem(PENDING_KEY);
      sessionStorage.removeItem(PENDING_KEY);
    } catch {
      /* ignore */
    }
    if (pending) void send(pending);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  function onSubmit(ev: FormEvent) {
    ev.preventDefault();
    void send(draft);
  }
  function onKeyDown(ev: KeyboardEvent<HTMLTextAreaElement>) {
    if (ev.key === "Enter" && !ev.shiftKey) {
      ev.preventDefault();
      void send(draft);
    }
  }

  return (
    <div className="chat">
      <div className="thread">
        {entries.length === 0 && !busy && (
          <div className="chat-welcome">
            <div className="welcome-icon"><Icon name="spark" size={26} /></div>
            <h1>What would you like to know about your Azure spend?</h1>
            <p>Specialist agents answer together from live Azure data, with your own permissions, and show the proof.</p>
            <div className="showcase">
              {SHOWCASE.map((q) => (
                <button key={q} className="prompt hero" onClick={() => void send(q)}>
                  <Icon name="spark" size={14} /> {q}
                </button>
              ))}
            </div>
            <div className="agent-grid">
              {capabilities.map((a) => (
                <div key={a.key} className="agent-card">
                  <div className="agent-card-head">{agentLabel(a.key)}</div>
                  <p>{a.summary}</p>
                  {a.examples.slice(0, 2).map((q) => (
                    <button key={q} className="prompt" onClick={() => void send(q)}>{q}</button>
                  ))}
                </div>
              ))}
            </div>
          </div>
        )}
        {entries.map((entry, idx) => {
          if (entry.kind === "user") return <div key={entry.id} className="bubble user">{entry.text}</div>;
          if (entry.kind === "assistant") {
            const last = idx === entries.length - 1;
            return (
              <AssistantMessage key={entry.id} response={entry.response}
                followUps={last && !busy ? followUpsFor(entry.response, entry.question, capabilities) : []}
                onAsk={(q) => void send(q)} />
            );
          }
          return (
            <div key={entry.id} className="bubble error" role="alert">
              <strong>No answer was produced.</strong> {entry.failure.message}
              <div className="meta">
                code {entry.failure.code}
                {entry.failure.correlationId && <> · correlation id {entry.failure.correlationId}</>}
                {entry.failure.retryable && <> · you can retry</>}
              </div>
            </div>
          );
        })}
        {busy && (
          <div className="bubble assistant pending" aria-live="polite">
            <span className="dots"><i /><i /><i /></span> {PROGRESS[progress]}
          </div>
        )}
        <div ref={bottom} />
      </div>
      <form className="composer" onSubmit={onSubmit}>
        <textarea value={draft} onChange={(e) => setDraft(e.target.value)} onKeyDown={onKeyDown}
          placeholder="Ask anything, e.g. Were there any cost spikes last month? What caused them?"
          rows={2} maxLength={4000} disabled={busy} aria-label="Your question" />
        <div className="composer-actions">
          {sessionId && (
            <button type="button" className="link small" onClick={() => { setSessionId(null); setEntries([]); }}>New conversation</button>
          )}
          <button type="submit" className="primary" disabled={busy || !draft.trim()}>
            <Icon name="spark" size={16} /> Ask
          </button>
        </div>
      </form>
    </div>
  );
}
