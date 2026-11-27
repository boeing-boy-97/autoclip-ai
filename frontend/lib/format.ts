/** Presentation helpers. Time formatting is deliberately shared by the transcript view, the moment
 *  list and the caption editor so a timestamp never means two different things on one screen. */

const pad = (n: number): string => String(Math.floor(n)).padStart(2, "0");

/** 11800 -> "00:11.8"  (mm:ss.d) */
export function timecode(ms: number): string {
  const safe = Math.max(0, Math.round(ms));
  const minutes = Math.floor(safe / 60_000);
  const seconds = Math.floor((safe % 60_000) / 1000);
  const tenths = Math.floor((safe % 1000) / 100);
  return `${pad(minutes)}:${pad(seconds)}.${tenths}`;
}

/** 11800 -> "11.8s" */
export function duration(ms: number | null | undefined): string {
  if (ms == null) return "—";
  return `${(ms / 1000).toFixed(1)}s`;
}

export function bytes(n: number | null | undefined): string {
  if (n == null) return "—";
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(0)} KB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}

export function percent(fraction: number | null | undefined): string {
  if (fraction == null) return "—";
  return `${Math.round(fraction * 100)}%`;
}

const STATE_LABELS: Record<string, string> = {
  DRAFT: "Draft",
  RENDERING: "Rendering",
  QA_FAILED: "Render failed",
  QA_PASSED: "Rendered",
  AWAITING_REVIEW: "Awaiting review",
  APPROVED: "Approved",
  REJECTED: "Rejected",
  PUBLISHED: "Published",
  ARCHIVED: "Archived",
};

export function stateLabel(state: string): string {
  return STATE_LABELS[state] ?? state.toLowerCase().replace(/_/g, " ");
}

/** Human wording for the states a clip can be acted on. Colour alone must not carry meaning. */
export function stateTone(state: string): "neutral" | "good" | "warn" | "bad" | "busy" {
  switch (state) {
    case "QA_PASSED":
    case "APPROVED":
    case "PUBLISHED":
      return "good";
    case "REJECTED":
    case "QA_FAILED":
      return "bad";
    case "RENDERING":
      return "busy";
    case "AWAITING_REVIEW":
      return "warn";
    default:
      return "neutral";
  }
}

export function relative(iso: string | null | undefined): string {
  if (!iso) return "—";
  const then = new Date(iso).getTime();
  const seconds = Math.round((Date.now() - then) / 1000);
  if (Number.isNaN(seconds)) return "—";
  if (seconds < 45) return "just now";
  if (seconds < 3600) return `${Math.round(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.round(seconds / 3600)}h ago`;
  return `${Math.round(seconds / 86400)}d ago`;
}
