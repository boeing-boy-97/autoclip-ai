"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useCallback, useEffect, useRef, useState } from "react";

import { ApiError, api, getCsrfToken } from "@/lib/api";
import { duration, relative, timecode } from "@/lib/format";
import { useSession } from "@/lib/session";
import type { Clip, Job, Moment, Page, Project, StageEvent, Transcript } from "@/lib/types";
import {
  Banner,
  Empty,
  JobProgress,
  KeyValue,
  Loading,
  OverallScore,
  ScoreRows,
  SkeletonRows,
  StateBadge,
  isTerminalJob,
} from "@/components/ui";
import { ClipCard } from "@/components/ClipCard";

const RIGHTS_STATEMENT =
  "I recorded this footage myself, or otherwise hold the rights needed to publish clips from it.";

export default function ProjectPage() {
  const params = useParams<{ projectId: string }>();
  const projectId = params.projectId;
  const { workspace } = useSession();

  const [project, setProject] = useState<Project | null>(null);
  const [moments, setMoments] = useState<Moment[] | null>(null);
  const [clips, setClips] = useState<Clip[] | null>(null);
  const [transcript, setTranscript] = useState<Transcript | null>(null);
  const [stages, setStages] = useState<StageEvent[] | null>(null);
  const [job, setJob] = useState<Job | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [pollVersion, setPollVersion] = useState(0);
  const [busy, setBusy] = useState<string | null>(null);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const base = workspace ? `/api/v1/workspaces/${workspace.id}` : null;

  const loadAll = useCallback(async () => {
    if (!base) return null;
    const [p, m, c] = await Promise.all([
      api<Project>(`${base}/projects/${projectId}`),
      api<Page<Moment>>(`${base}/projects/${projectId}/moments`).catch(() => ({ items: [] })),
      api<Page<Clip>>(`${base}/projects/${projectId}/clips`).catch(() => ({ items: [] })),
    ]);
    setProject(p);
    setMoments(m.items);
    setClips(c.items);
    void api<StageEvent[]>(`${base}/projects/${projectId}/stages`)
      .then(setStages)
      .catch(() => setStages([]));
    // A transcript only exists after transcription; a 404 here is the normal early state.
    void api<Transcript>(`${base}/projects/${projectId}/transcript?include_words=true`)
      .then(setTranscript)
      .catch(() => setTranscript(null));
    return p;
  }, [base, projectId]);

  const loadJob = useCallback(async () => {
    if (!base) return null;
    const page = await api<Page<Job>>(`${base}/jobs?limit=1&project_id=${projectId}`);
    const newest = page.items[0] ?? null;
    setJob(newest);
    return newest;
  }, [base, projectId]);

  useEffect(() => {
    if (!base) return;
    let cancelled = false;
    (async () => {
      try {
        await loadAll();
        if (cancelled) return;
        const current = await loadJob();
        if (cancelled) return;
        // Only keep polling when there is genuinely work in flight.
        if (current && !isTerminalJob(current)) {
          timer.current = setTimeout(poll, 1500);
        }
      } catch (err) {
        setError(err instanceof ApiError ? err.detail : "Could not load this project.");
      }
    })();

    async function poll() {
      if (cancelled) return;
      try {
        const current = await loadJob();
        if (cancelled) return;
        await loadAll();
        if (cancelled) return;
        if (current && !isTerminalJob(current)) {
          timer.current = setTimeout(poll, 1500);
        }
      } catch {
        /* keep the last good state rather than blanking the page on one failed poll */
      }
    }

    return () => {
      cancelled = true;
      if (timer.current) clearTimeout(timer.current);
    };
  }, [base, loadAll, loadJob, pollVersion]);

  async function run(action: () => Promise<unknown>, label: string) {
    setBusy(label);
    setError(null);
    setNotice(null);
    try {
      await action();
      await loadAll();
      // Restart the same bounded polling loop after a newly dispatched job.
      setPollVersion((version) => version + 1);
    } catch (err) {
      setError(err instanceof ApiError ? `${err.detail} (${err.code})` : "That did not work.");
    } finally {
      setBusy(null);
    }
  }

  if (!workspace || !base) return null;

  if (!project) {
    return error ? <Banner tone="bad">{error}</Banner> : <Loading label="Loading project…" />;
  }

  const hasSource = project.source !== null;
  const analysed = moments != null && moments.length > 0;

  return (
    <div className="stack" style={{ gap: 20 }}>
      <div className="row-between wrap">
        <div>
          <div className="row" style={{ gap: 10 }}>
            <Link href="/projects" className="faint">
              ← Projects
            </Link>
          </div>
          <h1 style={{ marginTop: 4 }}>{project.name}</h1>
          <div className="row" style={{ gap: 8 }}>
            <StateBadge state={project.status} />
            <span className="faint">
              {project.clips_requested} clips requested · {project.template_key} ·{" "}
              {project.target_platform.replace(/_/g, " ")}
            </span>
          </div>
        </div>
        <div className="row" style={{ gap: 8 }}>
          {hasSource && (
            <button
              className="btn"
              disabled={busy !== null}
              onClick={() =>
                void run(
                  () =>
                    api(`${base}/projects/${projectId}/analyze`, {
                      method: "POST",
                      body: {},
                    }),
                  "analyse",
                )
              }
            >
              {busy === "analyse" ? <><span className="spinner" /> Analysing…</> : "Analyse"}
            </button>
          )}
          {analysed && (
            <button
              className="btn btn-primary"
              disabled={busy !== null}
              onClick={() =>
                void run(async () => {
                  await api(`${base}/projects/${projectId}/generate-clips`, { method: "POST" });
                  setNotice("Clips generated from the selected moments.");
                }, "generate")
              }
            >
              {busy === "generate" ? <><span className="spinner" /> Generating…</> : "Generate clips"}
            </button>
          )}
        </div>
      </div>

      {error && <Banner tone="bad">{error}</Banner>}
      {notice && <Banner tone="good">{notice}</Banner>}

      {job && !isTerminalJob(job) && (
        <div className="card">
          <h2>Working</h2>
          <JobProgress job={job} />
        </div>
      )}
      {job && (job.status === "FAILED" || job.status === "DEAD_LETTER") && (
        <Banner tone="bad">
          <strong>{job.stage.replace(/_/g, " ")} failed.</strong>{" "}
          {job.error_detail ?? job.error_code ?? "No reason was recorded."}
          <div style={{ marginTop: 8 }}>
            <button
              className="btn btn-sm"
              disabled={busy !== null}
              onClick={() => void run(() => api(`${base}/jobs/${job.id}/retry`, { method: "POST" }), "retry")}
            >
              Retry this stage
            </button>
          </div>
        </Banner>
      )}

      {!hasSource ? (
        <UploadSource
          workspaceId={workspace.id}
          projectId={projectId}
          onUploaded={() => void loadAll()}
        />
      ) : (
        <div className="card">
          <div className="card-head">
            <h2 style={{ margin: 0 }}>Source</h2>
            <span className="badge" data-tone="good">
              <span className="dot" /> {project.source?.state}
            </span>
          </div>
          <div className="grid grid-2">
            <KeyValue
              items={[
                ["File", project.source?.original_filename ?? "—"],
                ["Duration", duration(project.source?.duration_ms)],
                ["Resolution", `${project.source?.width}×${project.source?.height}`],
                ["Codecs", `${project.source?.video_codec} / ${project.source?.audio_codec}`],
              ]}
            />
            <KeyValue
              items={[
                ["Rights basis", project.source?.rights_basis ?? "—"],
                ["SHA-256", project.source?.sha256?.slice(0, 16) ?? "—"],
                ["Uploaded", relative(project.source?.rights_confirmed_at)],
                ["Clips found", String(clips?.length ?? 0)],
              ]}
            />
          </div>
          <p className="hint" style={{ marginTop: 12, marginBottom: 0 }}>
            The SHA-256 is recorded so you can prove which file these clips came from.
          </p>
        </div>
      )}

      {clips && clips.length > 0 && (
        <div className="stack" style={{ gap: 12 }}>
          <h2 style={{ margin: 0 }}>Clips</h2>
          {clips.map((clip) => (
            <ClipCard
              key={clip.id}
              clip={clip}
              workspaceId={workspace.id}
              onChange={() => void loadAll()}
            />
          ))}
        </div>
      )}

      {analysed && (
        <div className="card">
          <div className="card-head">
            <h2 style={{ margin: 0 }}>Candidate moments</h2>
            <span className="faint">
              {moments?.filter((m) => m.selected).length ?? 0} selected of {moments?.length ?? 0}
            </span>
          </div>
          <p className="hint" style={{ marginTop: 0 }}>
            Scores estimate content characteristics. They are not predictions of how many people
            will watch.
          </p>
          <div className="stack" style={{ gap: 10 }}>
            {moments?.map((moment) => (
              <MomentRow key={moment.id} moment={moment} />
            ))}
          </div>
        </div>
      )}

      {transcript && <TranscriptPanel transcript={transcript} />}

      <div className="card">
        <div className="card-head">
          <h2 style={{ margin: 0 }}>Stage journal</h2>
          <span className="faint">every transition, in order</span>
        </div>
        {!stages ? (
          <SkeletonRows rows={4} />
        ) : stages.length === 0 ? (
          <Empty>No stage transitions recorded yet.</Empty>
        ) : (
          <div className="list">
            {stages.map((event, i) => (
              <div className="list-row" key={`${event.at}-${i}`}>
                <span
                  className="mono faint"
                  style={{ width: 88, flex: "none", whiteSpace: "nowrap" }}
                >
                  {new Date(event.at).toLocaleTimeString()}
                </span>
                <span className="mono" style={{ width: 170, flex: "none" }}>
                  {event.to_status}
                </span>
                <span className="faint truncate">{event.detail ?? event.task_name ?? ""}</span>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

function MomentRow({ moment }: { moment: Moment }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="card" style={{ background: "var(--bg-raised-2)", padding: 13 }}>
      <div className="row-between wrap">
        <div className="row" style={{ gap: 10, minWidth: 0 }}>
          <span className="mono faint">{timecode(moment.start_ms)}</span>
          <span className="badge">{moment.category}</span>
          {moment.selected ? (
            <span className="badge" data-tone="good">
              <span className="dot" /> selected
            </span>
          ) : (
            <span className="badge">not selected</span>
          )}
        </div>
        <div className="row" style={{ gap: 10 }}>
          <span className="faint">{duration(moment.duration_ms)}</span>
          <OverallScore score={moment.score} />
          <button className="btn btn-sm btn-ghost" onClick={() => setOpen((v) => !v)}>
            {open ? "Hide scores" : "Why?"}
          </button>
        </div>
      </div>
      {moment.hook_text && (
        <div className="truncate" style={{ marginTop: 7 }}>
          “{moment.hook_text}”
        </div>
      )}
      {open && (
        <div className="stack" style={{ marginTop: 12, gap: 10 }}>
          <ScoreRows score={moment.score} />
          {moment.rationale && (
            <div>
              <div className="eyebrow" style={{ marginBottom: 5 }}>
                How these were computed
              </div>
              <p className="faint" style={{ margin: 0, lineHeight: 1.6 }}>
                {moment.rationale}
              </p>
            </div>
          )}
          {moment.suppressed_reason && (
            <Banner tone="warn">{moment.suppressed_reason}</Banner>
          )}
        </div>
      )}
    </div>
  );
}

function TranscriptPanel({ transcript }: { transcript: Transcript }) {
  return (
    <div className="card">
      <div className="card-head">
        <h2 style={{ margin: 0 }}>Transcript</h2>
        <span className="faint">
          {transcript.provider} · {transcript.model} · {transcript.language} ·{" "}
          {transcript.word_count} words
        </span>
      </div>
      <p className="hint" style={{ marginTop: 0 }}>
        Word timings come from the speech model. A word highlighted in red was transcribed without a
        timestamp — the caption engine drops those rather than inventing a time for them.
      </p>
      <div className="transcript">
        {transcript.segments.map((segment) => (
          <div className="segment" key={segment.idx}>
            <span className="seg-time">{timecode(segment.start_ms)}</span>
            <span>
              {segment.words && segment.words.length > 0
                ? segment.words.map((word, i) => (
                    <span
                      className="word"
                      key={`${word.w}-${i}`}
                      data-untimed={word.s == null || word.e == null}
                      title={
                        word.s != null ? `${word.s}–${word.e} ms` : "no timing recorded"
                      }
                    >
                      {word.w}{" "}
                    </span>
                  ))
                : segment.text}
            </span>
          </div>
        ))}
      </div>
    </div>
  );
}

function UploadSource({
  workspaceId,
  projectId,
  onUploaded,
}: {
  workspaceId: string;
  projectId: string;
  onUploaded: () => void;
}) {
  const [file, setFile] = useState<File | null>(null);
  const [rights, setRights] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [progress, setProgress] = useState(0);

  const rightsOk = rights.trim().length >= 10;

  function submit(event: React.FormEvent) {
    event.preventDefault();
    if (!file) return;
    setBusy(true);
    setError(null);
    setProgress(0);

    const form = new FormData();
    form.append("file", file);
    form.append("rights_basis", "original");
    form.append("rights_statement", rights.trim());

    // XHR rather than fetch: upload progress is part of the promise this screen makes, and fetch
    // still cannot report request-body progress in a way this UI can use.
    const xhr = new XMLHttpRequest();
    xhr.open("POST", `/api/v1/workspaces/${workspaceId}/projects/${projectId}/sources`);
    // Same in-memory token the fetch wrapper uses; XHR is only chosen for its upload progress.
    const token = getCsrfToken();
    if (token) xhr.setRequestHeader("X-CSRF-Token", token);

    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable) setProgress(Math.round((e.loaded / e.total) * 100));
    };
    xhr.onload = () => {
      setBusy(false);
      if (xhr.status >= 200 && xhr.status < 300) {
        onUploaded();
        return;
      }
      try {
        const body = JSON.parse(xhr.responseText);
        setError(body.detail ?? body.title ?? `Upload failed (${xhr.status})`);
      } catch {
        setError(`Upload failed (${xhr.status})`);
      }
    };
    xhr.onerror = () => {
      setBusy(false);
      setError("The upload could not be completed.");
    };
    xhr.send(form);
  }

  return (
    <form className="card stack" onSubmit={submit}>
      <h2 style={{ margin: 0 }}>Upload a recording</h2>
      {error && <Banner tone="bad">{error}</Banner>}

      <div className="field">
        <label className="label" htmlFor="file">
          Video or audio file
        </label>
        <input
          id="file"
          className="input"
          type="file"
          accept="video/*,audio/*"
          onChange={(e) => setFile(e.target.files?.[0] ?? null)}
        />
        {file && (
          <span className="hint">
            {(file.size / (1024 * 1024)).toFixed(1)} MB · {file.type || "unknown type"}
          </span>
        )}
      </div>

      <div className="field">
        <label className="label" htmlFor="rights">
          Rights statement
        </label>
        <textarea
          id="rights"
          className="textarea"
          placeholder={RIGHTS_STATEMENT}
          value={rights}
          onChange={(e) => setRights(e.target.value)}
        />
        <span className="hint">
          Required, and stored with the file. Publishing clips of footage you do not hold rights to is
          how accounts get struck.
        </span>
      </div>

      {busy && (
        <div className="stack" style={{ gap: 6 }}>
          <div className="progress">
            <div className="progress-fill" style={{ width: `${progress}%` }} />
          </div>
          <span className="faint">Uploading… {progress}%</span>
        </div>
      )}

      <div className="row">
        <button className="btn btn-primary" type="submit" disabled={!file || !rightsOk || busy}>
          {busy ? <><span className="spinner" /> Uploading…</> : "Upload"}
        </button>
        {!rightsOk && <span className="hint">Write the rights statement to enable upload.</span>}
      </div>
    </form>
  );
}
