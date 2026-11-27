"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useCallback, useEffect, useState } from "react";

import { ApiError, api } from "@/lib/api";
import { relative } from "@/lib/format";
import { useSession } from "@/lib/session";
import type { Clip } from "@/lib/types";
import { Banner, Loading } from "@/components/ui";
import { ClipCard } from "@/components/ClipCard";

/** A clip also has its own address, so a link to one clip can be shared inside a workspace. */
export default function ClipPage() {
  const params = useParams<{ clipId: string }>();
  const { workspace } = useSession();
  const [clip, setClip] = useState<Clip | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async (workspaceId: string) => {
    try {
      setClip(await api<Clip>(`/api/v1/workspaces/${workspaceId}/clips/${params.clipId}`));
      setError(null);
    } catch (err) {
      setError(err instanceof ApiError ? err.detail : "Could not load this clip.");
    }
  }, [params.clipId]);

  useEffect(() => {
    if (workspace) void load(workspace.id);
  }, [workspace, load]);

  if (!workspace) return null;

  return (
    <div className="stack" style={{ gap: 18 }}>
      <div>
        <Link href={`/projects/${clip?.project_id ?? ""}`} className="faint">
          ← Back to project
        </Link>
        <h1 style={{ marginTop: 4 }}>Clip</h1>
        {clip && (
          <p className="sub" style={{ margin: 0 }}>
            Updated {relative(clip.created_at)}
          </p>
        )}
      </div>

      {error && <Banner tone="bad">{error}</Banner>}
      {!clip ? (
        error ? null : <Loading label="Loading clip…" />
      ) : (
        <ClipCard clip={clip} workspaceId={workspace.id} onChange={() => void load(workspace.id)} />
      )}
    </div>
  );
}
