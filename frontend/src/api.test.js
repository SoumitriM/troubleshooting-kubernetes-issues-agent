import { afterEach, describe, expect, it, vi } from "vitest";

import { requestKnowledge } from "./api";


afterEach(() => vi.unstubAllGlobals());

function response(body, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: { get: () => "application/json" },
    json: async () => body,
  };
}

describe("knowledge API integration", () => {
  it("lets the server create a fresh thread for every independent query", async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(response({ thread_id: "thread-1" }))
      .mockResolvedValueOnce(response({ thread_id: "thread-2" }));
    vi.stubGlobal("fetch", fetchMock);

    await requestKnowledge({ query: "First question" });
    await requestKnowledge({ query: "Second question" });

    expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toEqual({ query: "First question" });
    expect(JSON.parse(fetchMock.mock.calls[1][1].body)).toEqual({ query: "Second question" });
  });

  it("resumes clarification using the returned thread id", async () => {
    const fetchMock = vi.fn().mockResolvedValue(response({ status: "completed" }));
    vi.stubGlobal("fetch", fetchMock);

    await requestKnowledge({
      query: "kubelet logs show CrashLoopBackOff",
      clarification: true,
      threadId: "thread-1",
    });

    expect(fetchMock.mock.calls[0][0]).toBe("/api/knowledge/thread-1/resume");
    expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toEqual({
      answer: "kubelet logs show CrashLoopBackOff",
    });
  });

  it("surfaces backend conflict details", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response({ detail: "Conversation thread already exists" }, 409)));
    await expect(requestKnowledge({ query: "Question" })).rejects.toThrow(
      "Conversation thread already exists",
    );
  });
});
