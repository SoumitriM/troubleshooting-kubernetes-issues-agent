import { useCallback, useEffect, useRef, useState } from "react";
import {
  checkReadiness,
  deleteConversation,
  getConversation,
  listConversations,
  sendChatMessage,
} from "./api";
import { FormattedAnswer, relativeTime, sourceLabel } from "./format";

const ACTIVE_CONVERSATION_KEY = "kubesage.activeConversation";

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
  NEEDS_CLARIFICATION: "Needs details",
};

function Icon({ name, size = 20 }) {
  const paths = {
    mark: <><path d="M12 2 4 6v6c0 5 3.4 8.7 8 10 4.6-1.3 8-5 8-10V6l-8-4Z"/><path d="m9 12 2 2 4-5"/></>,
    pulse: <path d="M3 12h4l2-6 4 12 2-6h6" />,
    graph: <><circle cx="5" cy="12" r="2"/><circle cx="18" cy="6" r="2"/><circle cx="18" cy="18" r="2"/><path d="m7 11 9-4M7 13l9 4"/></>,
    book: <><path d="M4 5.5A2.5 2.5 0 0 1 6.5 3H11v17H6.5A2.5 2.5 0 0 0 4 22.5v-17ZM20 5.5A2.5 2.5 0 0 0 17.5 3H13v17h4.5a2.5 2.5 0 0 1 2.5 2.5v-17Z"/></>,
    send: <><path d="m22 2-7 20-4-9-9-4 20-7Z"/><path d="M22 2 11 13"/></>,
    link: <><path d="M10 13a5 5 0 0 0 7.5.5l2-2a5 5 0 0 0-7-7l-1.1 1"/><path d="M14 11a5 5 0 0 0-7.5-.5l-2 2a5 5 0 0 0 7 7l1.1-1"/></>,
    alert: <><path d="M12 9v4M12 17h.01"/><path d="M10.3 3.7 2.2 18a2 2 0 0 0 1.7 3h16.2a2 2 0 0 0 1.7-3L13.7 3.7a2 2 0 0 0-3.4 0Z"/></>,
    plus: <path d="M12 5v14M5 12h14" />,
    trash: <><path d="M4 7h16M10 11v6M14 11v6"/><path d="M6 7l1 13h10l1-13M9 7V4h6v3"/></>,
    menu: <path d="M4 6h16M4 12h16M4 18h16" />,
    chat: <path d="M21 12a8 8 0 0 1-11.8 7L4 20l1.1-4.6A8 8 0 1 1 21 12Z" />,
    help: <><circle cx="12" cy="12" r="9"/><path d="M9.5 9.5a2.5 2.5 0 1 1 3.5 2.3c-.6.3-1 .9-1 1.6M12 17h.01"/></>,
  };
  return <svg aria-hidden="true" width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">{paths[name]}</svg>;
}

function readStoredConversation() {
  try {
    return window.localStorage.getItem(ACTIVE_CONVERSATION_KEY);
  } catch {
    return null;
  }
}

function storeConversation(conversationId) {
  try {
    if (conversationId) window.localStorage.setItem(ACTIVE_CONVERSATION_KEY, conversationId);
    else window.localStorage.removeItem(ACTIVE_CONVERSATION_KEY);
  } catch {
    // Remembering the open conversation is a convenience only.
  }
}

function AssistantMessage({ message }) {
  const needsDetails = message.status === "needs_clarification";
  return (
    <article className={`message assistant${needsDetails ? " clarify" : ""}`}>
      <span className="avatar"><Icon name={needsDetails ? "help" : "mark"} size={17} /></span>
      <div className="bubble">
        {message.intent && (
          <span className="intent-pill">{INTENT_LABELS[message.intent] || message.intent}</span>
        )}
        <FormattedAnswer text={message.content} />
        {needsDetails && <p className="reply-hint">Reply below with these details to continue.</p>}
        {message.citations?.length > 0 && (
          <div className="sources">
            <span>Sources</span>
            <div>
              {message.citations.map((citation) => (
                <a key={citation} href={citation} target="_blank" rel="noreferrer" title={citation}>
                  <Icon name="link" size={13} />{sourceLabel(citation)}
                </a>
              ))}
            </div>
          </div>
        )}
      </div>
    </article>
  );
}

function App() {
  const [conversations, setConversations] = useState([]);
  const [conversationId, setConversationId] = useState(null);
  const [messages, setMessages] = useState([]);
  const [draft, setDraft] = useState("");
  const [sending, setSending] = useState(false);
  const [error, setError] = useState(null);
  const [serviceStatus, setServiceStatus] = useState("checking");
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const threadEnd = useRef(null);
  const composer = useRef(null);

  const refreshConversations = useCallback(async () => {
    try {
      setConversations(await listConversations());
    } catch {
      // The list is secondary; the chat still works without it.
    }
  }, []);

  const openConversation = useCallback(async (id) => {
    setSidebarOpen(false);
    setError(null);
    try {
      const detail = await getConversation(id);
      setConversationId(id);
      setMessages(detail.messages);
      storeConversation(id);
    } catch (requestError) {
      if (requestError.status === 404) storeConversation(null);
      setConversationId(null);
      setMessages([]);
    }
  }, []);

  useEffect(() => {
    checkReadiness()
      .then((ready) => setServiceStatus(ready ? "connected" : "degraded"))
      .catch(() => setServiceStatus("degraded"));
    refreshConversations();
    const stored = readStoredConversation();
    if (stored) openConversation(stored);
  }, [refreshConversations, openConversation]);

  useEffect(() => {
    threadEnd.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [messages, sending, error]);

  useEffect(() => {
    const element = composer.current;
    if (!element) return;
    element.style.height = "auto";
    element.style.height = `${Math.min(element.scrollHeight, 200)}px`;
  }, [draft]);

  const awaitingDetails = messages.at(-1)?.status === "needs_clarification";

  async function send(text) {
    const message = text.trim();
    if (!message || sending) return;
    const userMessage = { role: "user", content: message, created_at: new Date().toISOString() };
    setMessages((current) => [...current, userMessage]);
    setDraft("");
    setError(null);
    setSending(true);
    try {
      const body = await sendChatMessage({ message, conversationId });
      setConversationId(body.conversation_id);
      storeConversation(body.conversation_id);
      setMessages((current) => [...current, body.message]);
      refreshConversations();
    } catch (requestError) {
      setError({ text: requestError.message || "Unable to reach the assistant.", message });
      setMessages((current) => current.filter((item) => item !== userMessage));
    } finally {
      setSending(false);
      composer.current?.focus();
    }
  }

  function retry() {
    if (error) send(error.message);
  }

  function newChat() {
    setConversationId(null);
    setMessages([]);
    setError(null);
    setDraft("");
    setSidebarOpen(false);
    storeConversation(null);
    composer.current?.focus();
  }

  async function removeConversation(id) {
    try {
      await deleteConversation(id);
    } finally {
      if (id === conversationId) newChat();
      refreshConversations();
    }
  }

  function onKeyDown(event) {
    if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
      event.preventDefault();
      send(draft);
    }
  }

  return (
    <div className={`chat-shell${sidebarOpen ? " sidebar-open" : ""}`}>
      <aside className="sidebar" aria-label="Conversations">
        <a className="brand" href="/" aria-label="KubeSage home">
          <span className="brand-mark"><Icon name="mark" /></span>
          <span>Kube<span>Sage</span></span>
        </a>
        <button className="new-chat" onClick={newChat}><Icon name="plus" size={17} />New chat</button>
        <nav className="conversation-list">
          {conversations.length === 0 && <p className="empty-list">Your conversations will appear here.</p>}
          {conversations.map((conversation) => (
            <div key={conversation.id} className={`conversation${conversation.id === conversationId ? " active" : ""}`}>
              <button className="conversation-open" onClick={() => openConversation(conversation.id)}>
                <Icon name="chat" size={15} />
                <span><strong>{conversation.title}</strong><small>{relativeTime(conversation.updated_at)}</small></span>
              </button>
              <button className="conversation-delete" onClick={() => removeConversation(conversation.id)} aria-label={`Delete conversation: ${conversation.title}`}>
                <Icon name="trash" size={15} />
              </button>
            </div>
          ))}
        </nav>
        <div className={`service-state ${serviceStatus}`}>
          <span />
          {serviceStatus === "connected"
            ? "Knowledge service ready"
            : serviceStatus === "checking"
              ? "Checking service"
              : "Knowledge service degraded"}
        </div>
      </aside>
      {sidebarOpen && <button className="scrim" aria-label="Close conversations" onClick={() => setSidebarOpen(false)} />}

      <div className="chat-main">
        <header className="chat-topbar">
          <button className="menu-button" onClick={() => setSidebarOpen(true)} aria-label="Open conversations"><Icon name="menu" /></button>
          <span className="chat-title">
            {conversations.find((item) => item.id === conversationId)?.title || "New conversation"}
          </span>
        </header>

        <main className="thread" aria-live="polite">
          {messages.length === 0 && !sending ? (
            <section className="welcome" aria-labelledby="welcome-title">
              <div className="eyebrow"><span>AI-assisted</span> Kubernetes operations</div>
              <h1 id="welcome-title">Resolve incidents.<br/><em>Understand your cluster.</em></h1>
              <p>Ask in plain language and follow up naturally. KubeSage remembers the conversation and answers from incident evidence, graph analytics, or the official Kubernetes docs.</p>
              <div className="example-grid">
                {EXAMPLES.map((example) => (
                  <button key={example.label} onClick={() => send(example.query)}>
                    <span className={`example-icon ${example.icon}`}><Icon name={example.icon} /></span>
                    <span><strong>{example.label}</strong><small>{example.query}</small></span>
                  </button>
                ))}
              </div>
            </section>
          ) : (
            <div className="messages">
              {messages.map((message, index) =>
                message.role === "user" ? (
                  <div key={index} className="message user"><div className="bubble">{message.content}</div></div>
                ) : (
                  <AssistantMessage key={index} message={message} />
                ),
              )}
              {sending && (
                <div className="message assistant" role="status">
                  <span className="avatar"><Icon name="mark" size={17} /></span>
                  <div className="bubble typing"><span /><span /><span /><em>Finding evidence…</em></div>
                </div>
              )}
              {error && (
                <div className="message assistant error" role="alert">
                  <span className="avatar"><Icon name="alert" size={17} /></span>
                  <div className="bubble">
                    <p>{error.text}</p>
                    <button className="retry" onClick={retry}>Try again</button>
                  </div>
                </div>
              )}
            </div>
          )}
          <div ref={threadEnd} />
        </main>

        <form className="composer" onSubmit={(event) => { event.preventDefault(); send(draft); }}>
          <div className="composer-box">
            <textarea
              ref={composer}
              value={draft}
              onChange={(event) => setDraft(event.target.value)}
              onKeyDown={onKeyDown}
              placeholder={awaitingDetails
                ? "Add the error, component, version, or logs…"
                : messages.length
                  ? "Ask a follow-up…"
                  : "Describe an incident or ask about Kubernetes…"}
              rows="1"
              maxLength="4000"
              aria-label="Message"
            />
            <button type="submit" disabled={!draft.trim() || sending} aria-label="Send message">
              {sending ? <span className="spinner" /> : <Icon name="send" size={18} />}
            </button>
          </div>
          <p className="composer-note">Enter to send · Shift+Enter for a new line · Answers cite retrieved evidence only</p>
        </form>
      </div>
    </div>
  );
}

export default App;
