"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";

import { ApiError, api } from "@/lib/api";
import { relative } from "@/lib/format";
import { useSession } from "@/lib/session";
import type { Dashboard } from "@/lib/types";
import { Banner, Empty, SkeletonRows, StateBadge } from "@/components/ui";

export default function DashboardPage() {
  const { workspace } = useSession();
  const [data, setData] = useState<Dashboard | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async (workspaceId: string) => {
    try {
      setData(await api<Dashboard>(`/api/v1/workspaces/${workspaceId}/dashboard`));
      setError(null);
    } catch (err) {
      setError(err instanceof ApiError ? err.detail : "Could not load the dashboard.");
    }
  }, []);

  useEffect(() => {
    if (workspace) void load(workspace.id);
  }, [workspace, load]);

  if (!workspace) return null;

  return (
    <div className="stack" style={{ gap: 20 }}>
      <div className="row-between">
        <div>
          <h1>{workspace.name}</h1>
          <p className="sub" style={{ margin: 0 }}>
            Everything happening in this workspace.
          </p>
        </div>
        <Link href="/projects" className="btn btn-primary">
          New project
        </Link>
      </div>

      {error && <Banner tone="bad">{error}</Banner>}

      {!data ? (
        <div className="grid grid-3">
          <div className="skeleton" style={{ height: 84 }} />
          <div className="skeleton" style={{ height: 84 }} />
          <div className="skeleton" style={{ height: 84 }} />
        </div>
      ) : (
        <>
          <div className="grid grid-3">
            <Stat label="Projects" value={String(data.projects_total)} />
            <Stat label="Sources processed" value={String(data.sources_processed)} />
            <Stat
              label="Failed jobs"
              value={String(data.failed_jobs)}
              tone={data.failed_jobs > 0 ? "bad" : undefined}
            />
            <Stat label="Queue depth" value={String(data.queue_depth)} />
            <Stat label="Storage used" value={`${data.storage_mb.toFixed(1)} MB`} />
            <Stat label="Unread notifications" value={String(data.unread_notifications)} />
          </div>

          {Object.keys(data.projects_by_status).length > 0 && (
            <div className="card">
              <h2>Projects by stage</h2>
              <div className="row wrap" style={{ gap: 8 }}>
                {Object.entries(data.projects_by_status).map(([status, count]) => (
                  <span className="badge" key={status}>
                    {status.replace(/_/g, " ").toLowerCase()} · {count}
                  </span>
                ))}
              </div>
            </div>
          )}

          <div className="card">
            <div className="card-head">
              <h2 style={{ margin: 0 }}>Recent projects</h2>
              <Link href="/projects" className="btn btn-sm btn-ghost">
                View all
              </Link>
            </div>
            {data.recent_projects.length === 0 ? (
              <Empty>
                No projects yet.{" "}
                <Link href="/projects" style={{ color: "var(--accent)" }}>
                  Create one
                </Link>{" "}
                and upload a recording.
              </Empty>
            ) : (
              <div className="list">
                {data.recent_projects.map((project) => (
                  <Link
                    className="list-row"
                    key={project.id}
                    href={`/projects/${project.id}`}
                  >
                    <div className="list-link">
                      <div className="truncate" style={{ fontWeight: 550 }}>
                        {project.name}
                      </div>
                      <div className="faint">
                        {project.source?.original_filename ?? "no source"} ·{" "}
                        {project.clips_requested} clips · updated {relative(project.updated_at)}
                      </div>
                    </div>
                    {project.progress != null && project.progress < 100 && (
                      <span className="mono faint">{project.progress}%</span>
                    )}
                    <StateBadge state={project.status} />
                  </Link>
                ))}
              </div>
            )}
          </div>
        </>
      )}

      {!data && <SkeletonRows rows={4} />}
    </div>
  );
}

function Stat({
  label,
  value,
  tone,
}: {
  label: string;
  value: string;
  tone?: "bad" | "good";
}) {
  return (
    <div className="card">
      <div className="eyebrow">{label}</div>
      <div
        style={{
          fontSize: 25,
          fontWeight: 600,
          letterSpacing: "-0.02em",
          color: tone === "bad" ? "var(--bad)" : tone === "good" ? "var(--good)" : undefined,
        }}
      >
        {value}
      </div>
    </div>
  );
}
