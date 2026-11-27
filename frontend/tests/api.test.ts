import { afterEach, describe, expect, it, vi } from "vitest";
import { api, getCsrfToken, setCsrfToken } from "../lib/api";

afterEach(() => {
  vi.unstubAllGlobals();
  setCsrfToken(null);
  document.cookie = "ac_csrf=; Max-Age=0; path=/";
});

describe("API contract", () => {
  it("reads field errors from RFC 9457 responses", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({
      code: "validation_error", detail: "Invalid input",
      fields: [{ path: "name", message: "Required" }],
    }), { status: 422 })));
    await expect(api("/api/v1/projects")).rejects.toMatchObject({
      status: 422, code: "validation_error", fields: { name: "Required" },
    });
  });
  it("sends JSON, session credentials and CSRF on mutations", async () => {
    document.cookie = "ac_csrf=real-token; path=/";
    const fetcher = vi.fn().mockResolvedValue(new Response(null, { status: 204 }));
    vi.stubGlobal("fetch", fetcher);
    await expect(api("/api/v1/auth/logout", { method: "POST", body: {} })).resolves.toBeUndefined();
    expect(fetcher).toHaveBeenCalledWith("/api/v1/auth/logout", expect.objectContaining({
      credentials: "same-origin", body: "{}",
      headers: expect.objectContaining({ "X-CSRF-Token": "real-token" }),
    }));
  });
  it("uses a rotated cookie instead of a stale in-memory token", () => {
    setCsrfToken("old-token");
    document.cookie = "ac_csrf=new-token; path=/";
    expect(getCsrfToken()).toBe("new-token");
  });
  it("reports non-JSON proxy failures honestly", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response("Bad gateway", {
      status: 502, statusText: "Bad Gateway",
    })));
    await expect(api("/api/v1/auth/me")).rejects.toMatchObject({ status: 502, detail: "Bad Gateway" });
  });
});
