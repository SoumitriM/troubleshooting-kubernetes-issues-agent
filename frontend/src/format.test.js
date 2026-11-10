import { describe, expect, it } from "vitest";

import { relativeTime, sourceLabel } from "./format";

describe("answer formatting helpers", () => {
  it("labels GitHub and documentation sources readably", () => {
    expect(sourceLabel("https://github.com/kubernetes/kubernetes/issues/141382")).toBe("Issue #141382");
    expect(sourceLabel("https://github.com/kubernetes/kubernetes/pull/12")).toBe("PR #12");
    expect(sourceLabel("https://kubernetes.io/docs/concepts/workloads/pods/pod-lifecycle/")).toBe("pod lifecycle");
    expect(sourceLabel("not a url")).toBe("not a url");
  });

  it("formats relative times", () => {
    const now = Date.parse("2026-11-03T12:00:00Z");
    expect(relativeTime("2026-11-03T11:59:30Z", now)).toBe("just now");
    expect(relativeTime("2026-11-03T11:15:00Z", now)).toBe("45m ago");
    expect(relativeTime("2026-11-03T07:00:00Z", now)).toBe("5h ago");
    expect(relativeTime("2026-11-01T12:00:00Z", now)).toBe("2d ago");
  });
});
