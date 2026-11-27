"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";

import { ApiError, api } from "@/lib/api";
import { duration, relative } from "@/lib/format";
import { useSession } from "@/lib/session";
import type { Page, Project } from "@/lib/types";
import { Banner, Empty, SkeletonRows, StateBadge } from "@/components/ui";

const TEMPLATES = [
  "podcast",
  "business",
  "comedy",
  "educational",
  "interview",
  "motivational",
  "news",
  "tutorial",
];

export default function ProjectsPage() {
  const { workspace } = useSession();
  const [projects, setProjects] = useState<Project[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [loadingMore, setLoadingMore] = useState(false);

  const load = useCallback(async (workspaceId: string, cursor?: string) => {
    setLoadingMore(true);
    try {
      const query = cursor ? `?cursor=${encodeURIComponent(cursor)}` : "";
      const page = await api<Page<Project>>(`/api/v1/workspaces/${workspaceId}/projects${query}`);
      setProjects((previous) => cursor
        ? [...(previous ?? []), ...page.items.filter((item) => !previous?.some((row) => row.id === item.id))]
        : page.items);
      setNextCursor(page.next_cursor ?? null);
      setError(null);
    } catch (err) {
      setError(err instanceof ApiError ? err.detail : "Could not load projects.");
    } finally {
      setLoadingMore(false);
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
          <h1>Projects</h1>
          <p className="sub" style={{ margin: 0 }}>
            One project per recording you want to cut up.
          </p>
        </div>
        <button className="btn btn-primary" onClick={() => setCreating((v) => !v)}>
          {creating ? "Cancel" : "New project"}
        </button>
      </div>

      {error && <Banner tone="bad">{error}</Banner>}

      {creating && (
        <CreateProject
          workspaceId={workspace.id}
          onCreated={(project) => {
            setCreating(false);
            void load(workspace.id);
            window.location.href = `/projects/${project.id}`;
          }}
        />
      )}

      <div className="card">
        <div className="card-head">
          <h2 style={{ margin: 0 }}>All projects</h2>
          {projects && <span className="faint">{projects.length} loaded</span>}
        </div>
        {!projects ? (
          <SkeletonRows rows={4} />
        ) : projects.length === 0 ? (
          <Empty>No projects yet. Create one to upload a recording.</Empty>
        ) : (
          <div className="list">
            {projects.map((project) => (
              <Link className="list-row" key={project.id} href={`/projects/${project.id}`}>
                <div className="list-link">
                  <div className="truncate" style={{ fontWeight: 550 }}>
                    {project.name}
                  </div>
                  <div className="faint truncate">
                    {project.source
                      ? `${project.source.original_filename} · ${duration(project.source.duration_ms)} · ${project.source.width}×${project.source.height}`
                      : "no source uploaded"}
                    {" · "}
                    {project.clips_requested} clips · {relative(project.created_at)}
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
        {nextCursor && <button className="btn" disabled={loadingMore}
          onClick={() => void load(workspace.id, nextCursor)}>
          {loadingMore ? "Loading…" : "Load more projects"}
        </button>}
      </div>
    </div>
  );
}

function CreateProject({
  workspaceId,
  onCreated,
}: {
  workspaceId: string;
  onCreated: (project: Project) => void;
}) {
  const [name, setName] = useState("");
  const [clips, setClips] = useState(3);
  const [template, setTemplate] = useState("podcast");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const project = await api<Project>(`/api/v1/workspaces/${workspaceId}/projects`, {
        method: "POST",
        body: { name, clips_requested: clips, template_key: template },
      });
      onCreated(project);
    } catch (err) {
      setError(err instanceof ApiError ? err.detail : "Could not create the project.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <form className="card stack" onSubmit={submit}>
      <h2 style={{ margin: 0 }}>New project</h2>
      {error && <Banner tone="bad">{error}</Banner>}

      <div className="grid grid-3">
        <div className="field">
          <label className="label" htmlFor="name">
            Name
          </label>
          <input
            id="name"
            className="input"
            required
            placeholder="Episode 12"
            value={name}
            onChange={(e) => setName(e.target.value)}
          />
        </div>

        <div className="field">
          <label className="label" htmlFor="clips">
            Clips to find
          </label>
          <input
            id="clips"
            className="input"
            type="number"
            min={1}
            max={30}
            value={clips}
            onChange={(e) => setClips(Number(e.target.value))}
          />
        </div>

        <div className="field">
          <label className="label" htmlFor="template">
            Caption style
          </label>
          <select
            id="template"
            className="select"
            value={template}
            onChange={(e) => setTemplate(e.target.value)}
          >
            {TEMPLATES.map((t) => (
              <option key={t} value={t}>
                {t}
              </option>
            ))}
          </select>
        </div>
      </div>

      <div className="row">
        <button className="btn btn-primary" type="submit" disabled={busy || !name.trim()}>
          {busy ? (
            <>
              <span className="spinner" /> Creating…
            </>
          ) : (
            "Create project"
          )}
        </button>
        <span className="hint">You will upload the recording on the next screen.</span>
      </div>
    </form>
  );
}
