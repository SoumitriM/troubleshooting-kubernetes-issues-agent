import { useEffect, useState } from "react";
import { checkReadiness, requestKnowledge } from "./api";

const EXAMPLES = [
  {
    label: "Investigate an incident",
    query: "My kubelet started crashing after upgrading to v1.35",
    icon: "pulse",
  },
  {
    label: "Explore issue patterns",
    query: "What are the most common kubelet issues in v1.35?",
    icon: "graph",
  },
  {
    label: "Ask Kubernetes basics",
    query: "What does kubelet do?",
    icon: "book",
  },
];

const INTENT_LABELS = {
  INCIDENT_TROUBLESHOOTING: "Incident troubleshooting",
  GRAPH_ANALYTICS: "Graph analytics",
  GENERAL_KUBERNETES_QUESTION: "Kubernetes knowledge",
  NEEDS_CLARIFICATION: "Needs clarification",
};

function Icon({ name, size = 20 }) {
  const paths = {
    mark: <><path d="M12 2 4 6v6c0 5 3.4 8.7 8 10 4.6-1.3 8-5 8-10V6l-8-4Z"/><path d="m9 12 2 2 4-5"/></>,
    pulse: <path d="M3 12h4l2-6 4 12 2-6h6" />,
    graph: <><circle cx="5" cy="12" r="2"/><circle cx="18" cy="6" r="2"/><circle cx="18" cy="18" r="2"/><path d="m7 11 9-4M7 13l9 4"/></>,
    book: <><path d="M4 5.5A2.5 2.5 0 0 1 6.5 3H11v17H6.5A2.5 2.5 0 0 0 4 22.5v-17ZM20 5.5A2.5 2.5 0 0 0 17.5 3H13v17h4.5a2.5 2.5 0 0 1 2.5 2.5v-17Z"/></>,
    send: <><path d="m22 2-7 20-4-9-9-4 20-7Z"/><path d="M22 2 11 13"/></>,
    link: <><path d="M10 13a5 5 0 0 0 7.5.5l2-2a5 5 0 0 0-7-7l-1.1 1"/><path d="M14 11a5 5 0 0 0-7.5-.5l-2 2a5 5 0 0 0 7 7l1.1-1"/></>,
    check: <path d="m5 12 4 4L19 6" />,
    alert: <><path d="M12 9v4M12 17h.01"/><path d="M10.3 3.7 2.2 18a2 2 0 0 0 1.7 3h16.2a2 2 0 0 0 1.7-3L13.7 3.7a2 2 0 0 0-3.4 0Z"/></>,
  };
  return <svg aria-hidden="true" width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">{paths[name]}</svg>;
}

function App() {
  const [query, setQuery] = useState("");
  const [response, setResponse] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [serviceStatus, setServiceStatus] = useState("checking");

  useEffect(() => {
    checkReadiness()
      .then((ready) => setServiceStatus(ready ? "connected" : "degraded"))
      .catch(() => setServiceStatus("degraded"));
  }, []);

  async function submit(event, suggestedQuery) {
    event?.preventDefault();
    const question = (suggestedQuery ?? query).trim();
    if (!question || loading) return;
    const isClarification = response?.status === "needs_clarification" && !suggestedQuery;
    setQuery(question);
    setLoading(true);
    setError("");
    if (!isClarification) setResponse(null);
    try {
      const body = await requestKnowledge({
        query: question,
        clarification: isClarification,
        threadId: response?.thread_id,
      });
      setResponse(body);
      if (body.status === "needs_clarification") setQuery("");
    } catch (requestError) {
      setError(requestError.message || "Unable to reach the assistant.");
    } finally {
      setLoading(false);
    }
  }

  function reset() {
    setQuery("");
    setResponse(null);
    setError("");
  }

  return (
    <div className="app-shell">
      <header className="topbar">
        <a className="brand" href="/" aria-label="KubeSage home">
          <span className="brand-mark"><Icon name="mark" /></span>
          <span>Kube<span>Sage</span></span>
        </a>
        <div className={`service-state ${serviceStatus}`}>
          <span />
          {serviceStatus === "connected"
            ? "Knowledge service ready"
            : serviceStatus === "checking"
              ? "Checking service"
              : "Knowledge service degraded"}
        </div>
      </header>

      <main>
        <section className="hero" aria-labelledby="page-title">
          <div className="eyebrow"><span>AI-assisted</span> Kubernetes operations</div>
          <h1 id="page-title">Resolve incidents.<br/><em>Understand your cluster.</em></h1>
          <p>Ask a question in plain language. KubeSage routes it to incident evidence, graph analytics, or trusted Kubernetes knowledge.</p>
        </section>

        <section className="workspace" aria-label="Ask KubeSage">
          <form onSubmit={submit} className="query-box">
            <label htmlFor="query">
              {response?.status === "needs_clarification"
                ? "Add the requested details"
                : "What can I help you investigate?"}
            </label>
            <textarea
              id="query"
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              placeholder={response?.status === "needs_clarification"
                ? "Provide the error, component, version, logs, or other details…"
                : "Describe an incident or ask about your Kubernetes environment…"}
              rows="3"
              maxLength="4000"
              disabled={loading}
            />
            <div className="query-actions">
              <span>{query.length.toLocaleString()} / 4,000</span>
              <button type="submit" disabled={!query.trim() || loading}>
                {loading ? <span className="spinner" /> : <Icon name="send" size={18} />}
                {loading
                  ? "Analyzing"
                  : response?.status === "needs_clarification"
                    ? "Continue"
                    : "Ask KubeSage"}
              </button>
            </div>
          </form>

          {!response && !error && !loading && (
            <div className="examples">
              <p>Try an example</p>
              <div className="example-grid">
                {EXAMPLES.map((example) => (
                  <button key={example.label} onClick={(event) => submit(event, example.query)}>
                    <span className={`example-icon ${example.icon}`}><Icon name={example.icon} /></span>
                    <span><strong>{example.label}</strong><small>{example.query}</small></span>
                    <span className="arrow">→</span>
                  </button>
                ))}
              </div>
            </div>
          )}

          {loading && (
            <div className="result-card loading-card" role="status">
              <div className="thinking-orbit"><span/><span/><span/></div>
              <div><strong>Understanding your question</strong><p>Classifying intent and finding the safest evidence path…</p></div>
            </div>
          )}

          {error && (
            <div className="result-card error-card" role="alert">
              <span className="result-symbol"><Icon name="alert" /></span>
              <div><strong>We couldn’t complete that request</strong><p>{error}</p><button onClick={() => setError("")}>Try again</button></div>
            </div>
          )}

          {response && !loading && !error && (
            <article className="result-card answer-card" aria-live="polite">
              <div className="answer-heading">
                <span className="result-symbol"><Icon name="check" /></span>
                <div><span className="intent-pill">{INTENT_LABELS[response.intent] || response.intent}</span><h2>KubeSage response</h2></div>
                <button className="new-query" onClick={reset}>New question</button>
              </div>
              <div className="answer-copy">{response.answer}</div>
              {response.citations?.length > 0 && (
                <div className="citations"><h3>Sources</h3>{response.citations.map((citation) => <a key={citation} href={citation} target="_blank" rel="noreferrer"><Icon name="link" size={16}/>{citation}</a>)}</div>
              )}
              <footer><span>Route: {response.route.replaceAll("_", " ")}</span><span>Thread {response.thread_id.slice(0, 8)}</span></footer>
            </article>
          )}
        </section>
      </main>

      <footer className="site-footer"><span>KubeSage</span><span>Answers are grounded in retrieved evidence and validated graph queries.</span></footer>
    </div>
  );
}

export default App;
