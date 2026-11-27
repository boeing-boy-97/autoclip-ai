import React from "react";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError, api } from "../lib/api";
import { SessionProvider, useSession } from "../lib/session";

const navigation = vi.hoisted(() => ({ push: vi.fn() }));
vi.mock("next/navigation", () => ({ useRouter: () => navigation }));
vi.mock("../lib/api", async (original) => ({
  ...await original<typeof import("../lib/api")>(), api: vi.fn(),
}));
const user = { id: "user", workspaces: [{ id: "ws1", name: "Studio", role: "owner" }] };
function Probe() {
  const session = useSession();
  return <>
    <span>{session.status}</span><span>{session.workspace?.name}</span>
    <button onClick={() => void session.refresh()}>Refresh</button>
    <button onClick={() => session.selectWorkspace("ws1")}>Select</button>
  </>;
}
afterEach(() => vi.restoreAllMocks());
describe("session resilience", () => {
  it("keeps valid sessions when localStorage is blocked", async () => {
    vi.mocked(api).mockResolvedValue(user);
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => { throw new DOMException("Blocked"); });
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw new DOMException("Blocked"); });
    render(<SessionProvider><Probe /></SessionProvider>);
    await screen.findByText("authenticated");
    fireEvent.click(screen.getByText("Select"));
    expect(screen.getByText("Studio")).toBeInTheDocument();
  });
  it("shows a retryable outage instead of treating a 503 as logout", async () => {
    vi.mocked(api).mockRejectedValueOnce(new ApiError(503, "unavailable", "Database unavailable"));
    render(<SessionProvider><Probe /></SessionProvider>);
    await screen.findByText("error");
    vi.mocked(api).mockResolvedValueOnce(user);
    fireEvent.click(screen.getByText("Refresh"));
    await screen.findByText("authenticated");
  });
  it("does not apply an older refresh after a newer one", async () => {
    let resolveOld!: (value: unknown) => void;
    vi.mocked(api).mockImplementationOnce(() => new Promise((resolve) => { resolveOld = resolve; }));
    render(<SessionProvider><Probe /></SessionProvider>);
    vi.mocked(api).mockResolvedValueOnce(user);
    fireEvent.click(screen.getByText("Refresh"));
    await screen.findByText("authenticated");
    await act(async () => resolveOld({ id: "old", workspaces: [] }));
    await waitFor(() => expect(screen.getByText("Studio")).toBeInTheDocument());
  });
});
