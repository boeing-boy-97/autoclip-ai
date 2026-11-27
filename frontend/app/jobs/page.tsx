"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import { ApiError, api } from "@/lib/api";
import { relative } from "@/lib/format";
import { useSession } from "@/lib/session";
import type { Job, Page } from "@/lib/types";
import { Banner, Empty, JobProgress, SkeletonRows, isTerminalJob } from "@/components/ui";

const POLL_MS = 3000;

export default function JobsPage() {
  const { workspace } = useSession();
  const [jobs, setJobs] = useState<Job[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const load = useCallback(async (workspaceId: string) => {
    try {
      const page = await api<Page<Job>>(`/api/v1/workspaces/${workspaceId}/jobs?limit=50`);
      setJobs(page.items);
      setError(null);
      return page.items;
    } catch (err) {
      setError(err instanceof ApiError ? err.detail : "Could not load jobs.");
      return [];
    }
  }, []);

  // Poll only while something is actually in flight. Polling a finished list forever is a wasted
  // request per user per three seconds, and it hides bugs in the progress reporting.
  useEffect(() => {
    if (!workspace) return;
    let cancelled = false;

    const tick = async () => {
      const items = await load(workspace.id);
      if (cancelled) return;
      const active = items.some((job) => !isTerminalJob(job));
      timer.current = setTimeout(tick, active ? POLL_MS : 15000);
    };

    void tick();
    return () => {
      cancelled = true;
      if (timer.current) clearTimeout(timer.current);
    };
  }, [workspace, load]);

  async function act(job: Job, action: "cancel" | "retry") {
    if (!workspace) return;
    setBusyId(job.id);
    setError(null);
    try {
      await api(`/api/v1/workspaces/${workspace.id}/jobs/${job.id}/${action}`, { method: "POST" });
      await load(workspace.id);
    } catch (err) {
      setError(err instanceof ApiError ? err.detail : `Could not ${action} that job.`);
    } finally {
      setBusyId(null);
    }
  }

  if (!workspace) return null;

  return (
    <div className="stack" style={{ gap: 20 }}>
      <div>
        <h1>Jobs</h1>
        <p className="sub" style={{ margin: 0 }}>
          Every stage the pipeline has run in this workspace, newest first.
        </p>
      </div>

      {error && <Banner tone="bad">{error}</Banner>}

      <div className="card">
        {!jobs ? (
          <SkeletonRows rows={5} />
        ) : jobs.length === 0 ? (
          <Empty>No jobs have run yet.</Empty>
        ) : (
          <div className="list">
            {jobs.map((job) => {
              const active = !isTerminalJob(job);
              const retryable = job.status === "FAILED" || job.status === "DEAD_LETTER";
              return (
                <div className="list-row" key={job.id} style={{ alignItems: "flex-start" }}>
                  <div className="list-link">
                    <div className="row-between" style={{ marginBottom: 6 }}>
                      <span className="row" style={{ gap: 8 }}>
                        <span style={{ fontWeight: 550 }}>{job.task_name}</span>
                        <span className="badge">{job.queue}</span>
                      </span>
                      <span className="faint">
                        {job.attempts}/{job.max_attempts} · {relative(job.created_at)}
                      </span>
                    </div>
                    <JobProgress job={job} />
                    {job.error_code && (
                      <div className="faint mono" style={{ marginTop: 4 }}>
                        {job.error_code}
                      </div>
                    )}
                  </div>
                  <div className="row" style={{ gap: 6, flex: "none" }}>
                    {active && (
                      <button
                        className="btn btn-sm"
                        disabled={busyId === job.id}
                        onClick={() => void act(job, "cancel")}
                      >
                        Cancel
                      </button>
                    )}
                    {retryable && (
                      <button
                        className="btn btn-sm"
                        disabled={busyId === job.id}
                        onClick={() => void act(job, "retry")}
                      >
                        Retry
                      </button>
                    )}
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </div>
    </div>
  );
}
