const REQUEST_TIMEOUT_MS = 175_000;

async function requestJson(url, { method = "GET", body, timeoutMs = REQUEST_TIMEOUT_MS } = {}) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(url, {
      method,
      headers: body === undefined ? undefined : { "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: controller.signal,
    });
    const contentType = response.headers.get("content-type") || "";
    const payload = contentType.includes("application/json")
      ? await response.json()
      : {};
    if (!response.ok) {
      const detail = Array.isArray(payload.detail)
        ? payload.detail.map((item) => item.msg).filter(Boolean).join("; ")
        : payload.detail;
      const error = new Error(detail || `Request failed with status ${response.status}.`);
      error.status = response.status;
      throw error;
    }
    return payload;
  } catch (error) {
    if (error.name === "AbortError") {
      throw new Error("The request took too long. Please try again.");
    }
    throw error;
  } finally {
    clearTimeout(timeout);
  }
}

export async function requestKnowledge({ query, clarification, threadId }) {
  const isClarification = Boolean(clarification && threadId);
  return requestJson(
    isClarification
      ? `/api/knowledge/${encodeURIComponent(threadId)}/resume`
      : "/api/knowledge/query",
    { method: "POST", body: isClarification ? { answer: query } : { query } },
  );
}

export async function sendChatMessage({ message, conversationId }) {
  return requestJson("/api/chat", {
    method: "POST",
    body: conversationId
      ? { message, conversation_id: conversationId }
      : { message },
  });
}

export async function listConversations() {
  return requestJson("/api/chat/conversations", { timeoutMs: 10_000 });
}

export async function getConversation(conversationId) {
  return requestJson(
    `/api/chat/conversations/${encodeURIComponent(conversationId)}`,
    { timeoutMs: 10_000 },
  );
}

export async function deleteConversation(conversationId) {
  const response = await fetch(
    `/api/chat/conversations/${encodeURIComponent(conversationId)}`,
    { method: "DELETE", signal: AbortSignal.timeout(10_000) },
  );
  if (!response.ok && response.status !== 404) {
    throw new Error(`Could not delete the conversation (status ${response.status}).`);
  }
}

export async function checkReadiness() {
  const response = await fetch("/ready", { signal: AbortSignal.timeout(5000) });
  return response.ok;
}
