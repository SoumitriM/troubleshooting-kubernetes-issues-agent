const REQUEST_TIMEOUT_MS = 175_000;

export async function requestKnowledge({ query, clarification, threadId }) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
  const isClarification = Boolean(clarification && threadId);
  try {
    const response = await fetch(
      isClarification
        ? `/api/knowledge/${encodeURIComponent(threadId)}/resume`
        : "/api/knowledge/query",
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(
          isClarification ? { answer: query } : { query },
        ),
        signal: controller.signal,
      },
    );
    const contentType = response.headers.get("content-type") || "";
    const body = contentType.includes("application/json")
      ? await response.json()
      : {};
    if (!response.ok) {
      const detail = Array.isArray(body.detail)
        ? body.detail.map((item) => item.msg).filter(Boolean).join("; ")
        : body.detail;
      throw new Error(detail || `Request failed with status ${response.status}.`);
    }
    return body;
  } catch (error) {
    if (error.name === "AbortError") {
      throw new Error("The request took too long. Please try again.");
    }
    throw error;
  } finally {
    clearTimeout(timeout);
  }
}

export async function checkReadiness() {
  const response = await fetch("/ready", { signal: AbortSignal.timeout(5000) });
  return response.ok;
}
