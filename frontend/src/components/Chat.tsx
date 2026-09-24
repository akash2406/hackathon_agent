import type { AccountInfo } from "@azure/msal-browser";
import { useMsal } from "@azure/msal-react";
import { useEffect, useRef, useState, type FormEvent, type KeyboardEvent } from "react";
import { ApiFailure, postChat } from "../api";
import type { ChatResponse } from "../types";
import AssistantMessage from "./AssistantMessage";

type Entry =
  | { kind: "user"; id: string; text: string }
  | { kind: "assistant"; id: string; response: ChatResponse }
  | { kind: "error"; id: string; failure: ApiFailure };

const EXAMPLES = [
  "Which subscriptions can I see?",
  "What is my month-to-date spend by resource group?",
  "What did I spend last month by resource type?",
];

export default function Chat({ account }: { account: AccountInfo }) {
  const { instance } = useMsal();
  const [entries, setEntries] = useState<Entry[]>([]);
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const bottom = useRef<HTMLDivElement>(null);

  useEffect(() => bottom.current?.scrollIntoView({ behavior: "smooth" }), [entries, busy]);

  async function send(text: string) {
    const message = text.trim();
    if (!message || busy) return;
    setDraft("");
    setBusy(true);
    setEntries((e) => [...e, { kind: "user", id: crypto.randomUUID(), text: message }]);
    try {
      const response = await postChat(instance, account, message, sessionId);
      setSessionId(response.session_id);
      setEntries((e) => [...e, { kind: "assistant", id: response.message_id, response }]);
    } catch (err) {
      const failure =
        err instanceof ApiFailure ? err : new ApiFailure(String(err), "client_error", null, true);
      setEntries((e) => [...e, { kind: "error", id: crypto.randomUUID(), failure }]);
    } finally {
      setBusy(false);
    }
  }

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
    <main className="chat">
      <div className="thread">
        {entries.length === 0 && (
          <div className="empty">
            <p>Ask a question about your Azure costs. Every answer shows the exact Cost Management query and how fresh the data is.</p>
            <div className="examples">
              {EXAMPLES.map((q) => (
                <button key={q} className="chip" onClick={() => void send(q)}>
                  {q}
                </button>
              ))}
            </div>
          </div>
        )}
        {entries.map((entry) => {
          if (entry.kind === "user") return <div key={entry.id} className="bubble user">{entry.text}</div>;
          if (entry.kind === "assistant") return <AssistantMessage key={entry.id} response={entry.response} />;
          return (
            <div key={entry.id} className="bubble error" role="alert">
              <strong>No answer was produced.</strong> {entry.failure.message}
              <div className="meta">
                code: {entry.failure.code}
                {entry.failure.correlationId && <> · correlation id: {entry.failure.correlationId}</>}
                {entry.failure.retryable && <> · you can retry</>}
              </div>
            </div>
          );
        })}
        {busy && <div className="bubble assistant pending">Querying Azure…</div>}
        <div ref={bottom} />
      </div>

      <form className="composer" onSubmit={onSubmit}>
        <textarea
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={onKeyDown}
          placeholder="e.g. What is my month-to-date spend by resource group?"
          rows={2}
          maxLength={4000}
          disabled={busy}
        />
        <button type="submit" disabled={busy || !draft.trim()}>
          Send
        </button>
        {sessionId && (
          <button
            type="button"
            className="link"
            onClick={() => {
              setSessionId(null);
              setEntries([]);
            }}
          >
            New conversation
          </button>
        )}
      </form>
    </main>
  );
}
