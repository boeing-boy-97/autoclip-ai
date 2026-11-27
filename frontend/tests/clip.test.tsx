import React from "react";
import { render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { ClipCard } from "../components/ClipCard";
import type { Clip } from "../lib/types";

vi.mock("../lib/api", async (original) => ({
  ...await original<typeof import("../lib/api")>(),
  api: vi.fn(async (path: string) => {
    if (path.endsWith("/versions")) return [{ id: "v1", version_no: 1, bytes: 1000 }];
    if (path.endsWith("/captions")) return { cues: [] };
    return { render_plan: null };
  }),
}));

describe("rendered clip", () => {
  it("provides a real authenticated video and download link", async () => {
    const clip = {
      id: "clip1", title: "An idea", current_version_id: "v1", state: "QA_PASSED",
      start_ms: 0, end_ms: 1000, duration_ms: 1000, layout: "single", template_key: "podcast",
    } as Clip;
    const { container } = render(<ClipCard clip={clip} workspaceId="ws1" onChange={vi.fn()} />);
    await waitFor(() => expect(container.querySelector("video")).toHaveAttribute(
      "src", "/api/v1/workspaces/ws1/clips/clip1/media?version_id=v1",
    ));
    expect(screen.getByRole("link", { name: "Download MP4" })).toHaveAttribute(
      "href", "/api/v1/workspaces/ws1/clips/clip1/media?download=true&version_id=v1",
    );
  });
});
