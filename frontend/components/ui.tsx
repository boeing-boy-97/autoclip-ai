"use client";

import type { ReactNode } from "react";

import { percent, stateLabel, stateTone } from "@/lib/format";
import type { CandidateScore, Job } from "@/lib/types";

export function StateBadge({ state }: { state: string }) {
  return (
    <span className="badge" data-tone={stateTone(state)}>
      <span className="dot" />
      {stateLabel(state)}
    </span>
  );
}

export function Banner({
  tone = "neutral",
  children,
}: {
  tone?: "neutral" | "good" | "warn" | "bad";
  children: ReactNode;
}) {
  return (
    <div className="banner" data-tone={tone} role={tone === "bad" ? "alert" : "status"}>
      <div>{children}</div>
    </div>
  );
}

export function Empty({ children }: { children: ReactNode }) {
  return <div className="empty">{children}</div>;
}

export function Loading({ label = "Loading…" }: { label?: string }) {
  return (
    <div className="row dim">
      <span className="spinner" /> {label}
    </div>
  );
}

export function SkeletonRows({ rows = 3 }: { rows?: number }) {
  return (
    <div className="stack">
      {Array.from({ length: rows }).map((_, i) => (
        <div key={i} className="skeleton" style={{ height: 38 }} />
      ))}
    </div>
  );
}

const SCORE_ORDER = [
  "hook",
  "content_value",
  "narrative",
  "standalone",
  "audience",
  "emotion",
  "visual_quality",
  "audio_quality",
  "ending_strength",
  "clarity",
  "technical_quality",
  "context_completeness",
  "audience_relevance",
  "content_potential",
] as const;

/** Scores arrive as a flat object of 0-100 components plus `overall`.
 *
 *  Every bar is labelled with its own number: a bar alone is a picture, not a measurement, and these
 *  are estimates of content characteristics, never view predictions.
 */
export function ScoreRows({ score }: { score: CandidateScore | null }) {
  if (!score) return <div className="faint">No component scores were stored for this moment.</div>;
  return (
    <div className="stack" style={{ gap: 6 }}>
      {SCORE_ORDER.map((key) => {
        const value = score[key];
        if (typeof value !== "number") return null;
        return (
          <div className="score-line" key={key}>
            <span className="score-label">{key.replace(/_/g, " ")}</span>
            <span className="score-track">
              <span className="score-fill" style={{ width: `${Math.min(100, value)}%` }} />
            </span>
            <span className="score-value">{value.toFixed(0)}</span>
          </div>
        );
      })}
    </div>
  );
}

export function OverallScore({ score }: { score: CandidateScore | null }) {
  if (!score) return <span className="mono faint">—</span>;
  return (
    <span className="row" style={{ gap: 8 }}>
      <span className="score-track" style={{ width: 74 }}>
        <span className="score-fill" style={{ width: `${Math.min(100, score.overall)}%` }} />
      </span>
      <span className="mono" style={{ fontSize: 12.5 }}>
        {score.overall.toFixed(1)}
      </span>
    </span>
  );
}

const TERMINAL = new Set(["SUCCEEDED", "FAILED", "CANCELLED", "DEAD_LETTER"]);

export function isTerminalJob(job: Job | null | undefined): boolean {
  return !job || TERMINAL.has(job.status);
}

export function JobProgress({ job }: { job: Job }) {
  const tone =
    job.status === "FAILED" || job.status === "DEAD_LETTER"
      ? "bad"
      : job.status === "SUCCEEDED"
        ? "good"
        : undefined;
  const progress = job.progress ?? 0;
  return (
    <div className="stack" style={{ gap: 6 }}>
      <div className="row-between">
        <span className="row" style={{ gap: 7 }}>
          {!TERMINAL.has(job.status) && <span className="spinner" />}
          <span style={{ fontSize: 13.5 }}>
            {job.stage.replace(/_/g, " ")} · {job.status.toLowerCase().replace(/_/g, " ")}
          </span>
        </span>
        <span className="mono dim">{progress}%</span>
      </div>
      <div className="progress">
        <div className="progress-fill" data-tone={tone} style={{ width: `${progress}%` }} />
      </div>
      {(job.progress_note || job.error_detail) && (
        <div className={job.error_detail ? "error-text" : "faint"}>
          {job.error_detail ?? job.progress_note}
        </div>
      )}
    </div>
  );
}

export function KeyValue({ items }: { items: [string, ReactNode][] }) {
  return (
    <div className="stack" style={{ gap: 6 }}>
      {items.map(([k, v]) => (
        <div className="row-between" key={k} style={{ fontSize: 13.5 }}>
          <span className="dim">{k}</span>
          <span className="mono">{v}</span>
        </div>
      ))}
    </div>
  );
}

export { percent };
