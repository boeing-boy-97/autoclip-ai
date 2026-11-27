"use client";

import { useCallback, useEffect, useState } from "react";

import { ApiError, api } from "@/lib/api";
import { bytes as fmtBytes, duration, percent, timecode } from "@/lib/format";
import type {
  CaptionCue,
  Captions,
  Clip,
  ClipRender,
  ClipVersion,
  RenderPlan,
  RenderPlanResponse,
} from "@/lib/types";
import { Banner, Empty, KeyValue, SkeletonRows, StateBadge } from "@/components/ui";

type Props = { clip: Clip; workspaceId: string; onChange: () => void };

export function ClipCard({ clip, workspaceId, onChange }: Props) {
  const base = `/api/v1/workspaces/${workspaceId}/clips/${clip.id}`;
  const [versions, setVersions] = useState<ClipVersion[] | null>(null);
  const [captions, setCaptions] = useState<Captions | null>(null);
  const [plan, setPlan] = useState<RenderPlan | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [rejecting, setRejecting] = useState(false);
  const [reason, setReason] = useState("");
  const [showCues, setShowCues] = useState(false);

  const load = useCallback(async () => {
    const [v, c] = await Promise.all([
      api<ClipVersion[]>(`${base}/versions`),
      api<Captions>(`${base}/captions`).catch(() => null),
    ]);
    setVersions(v);
    setCaptions(c);
    if (clip.current_version_id) {
      void api<RenderPlanResponse>(`${base}/render-plan`)
        .then((r) => setPlan(r.render_plan))
        .catch(() => setPlan(null));
    } else {
      setPlan(null);
    }
  }, [base, clip.current_version_id]);

  useEffect(() => {
    void load();
  }, [load]);

  async function act(label: string, fn: () => Promise<unknown>) {
    setBusy(label);
    setError(null);
    try {
      await fn();
      await load();
      onChange();
    } catch (err) {
      setError(err instanceof ApiError ? `${err.detail} (${err.code})` : "That did not work.");
    } finally {
      setBusy(null);
    }
  }

  const renderable = ["DRAFT", "QA_FAILED", "REJECTED"].includes(clip.state);
  const current = versions?.find((v) => v.id === clip.current_version_id) ?? null;
  const caps = (clip.metadata?.captions ?? null) as Record<string, unknown> | null;

  return (
    <div className="card">
      <div className="row-between wrap" style={{ marginBottom: 12 }}>
        <div style={{ minWidth: 0 }}>
          <div className="truncate" style={{ fontWeight: 600 }}>
            {clip.title}
          </div>
          <div className="faint">
            {timecode(clip.start_ms)}–{timecode(clip.end_ms)} · {duration(clip.duration_ms)} ·{" "}
            {clip.layout.replace(/_/g, " ")} · {clip.template_key}
          </div>
        </div>
        <div className="row" style={{ gap: 8 }}>
          <StateBadge state={clip.state} />
          {clip.publishable && (
            <span className="badge" data-tone="good">
              <span className="dot" /> publishable
            </span>
          )}
        </div>
      </div>

      {error && <Banner tone="bad">{error}</Banner>}
      {clip.rejection_reason && (
        <div style={{ marginBottom: 12 }}>
          <Banner tone="warn">
            <strong>Rejected:</strong> {clip.rejection_reason}
          </Banner>
        </div>
      )}

      <div className="clip-preview">
        <div style={{ flex: "none" }}>
          {current ? (
            <div className="clip-frame" style={{ display: "grid", placeItems: "center", padding: 14 }}>
              <div style={{ textAlign: "center" }}>
                <div className="mono dim" style={{ marginBottom: 6 }}>
                  {current.width}×{current.height}
                </div>
                <div className="faint" style={{ marginBottom: 10 }}>
                  version {current.version_no} · {fmtBytes(current.bytes)}
                </div>
                <div className="hint">
                  The rendered file is on the storage backend. This preview has no signed URL under
                  local storage, so it is described rather than shown.
                </div>
              </div>
            </div>
          ) : (
            <div className="clip-frame" style={{ display: "grid", placeItems: "center" }}>
              <span className="faint">Not rendered</span>
            </div>
          )}
        </div>

        <div className="stack" style={{ flex: 1, minWidth: 0 }}>
          {caps && (
            <KeyValue
              items={[
                ["Captions", caps.burned ? "burned in" : "none"],
                ["Cues", String(caps.cue_count ?? 0)],
                ["Coverage", percent(caps.coverage as number)],
                [
                  "Words without timing",
                  String(caps.words_without_timing ?? 0),
                ],
              ]}
            />
          )}

          {plan && plan.canvas && plan.window && (
            <details>
              <summary className="faint" style={{ cursor: "pointer" }}>
                Render plan (why this file looks the way it does)
              </summary>
              <div className="stack" style={{ marginTop: 8, gap: 6 }}>
                <KeyValue
                  items={[
                    ["Canvas", `${plan.canvas.width}×${plan.canvas.height} @ ${plan.canvas.fps}fps`],
                    [
                      "Source",
                      `${plan.source_dimensions.width}×${plan.source_dimensions.height}`,
                    ],
                    ["Caption style", plan.template_key],
                    ["Encoder", `${plan.encoder.codec} · ${plan.encoder.preset} · crf ${plan.encoder.crf}`],
                    ["Start nudge", `${plan.adjustments.start_delta_ms} ms`],
                    ["End nudge", `${plan.adjustments.end_delta_ms} ms`],
                  ]}
                />
                {plan.adjustments?.reason?.length > 0 && (
                  <ul className="faint" style={{ margin: 0, paddingLeft: 18 }}>
                    {plan.adjustments.reason.map((r: string) => (
                      <li key={r}>{r}</li>
                    ))}
                  </ul>
                )}
                {plan.warnings?.map((w) => (
                  <div className="error-text" key={w}>
                    {w}
                  </div>
                ))}
              </div>
            </details>
          )}

          <div className="row wrap" style={{ gap: 8 }}>
            {renderable && (
              <button
                className="btn btn-primary btn-sm"
                disabled={busy !== null}
                onClick={() =>
                  void act("render", () =>
                    api<ClipRender>(`${base}/render`, { method: "POST" }),
                  )
                }
              >
                {busy === "render" ? (
                  <>
                    <span className="spinner" /> Rendering…
                  </>
                ) : clip.state === "DRAFT" ? (
                  "Render clip"
                ) : (
                  "Render again"
                )}
              </button>
            )}

            {clip.state === "QA_PASSED" && !rejecting && (
              <button className="btn btn-sm btn-danger" onClick={() => setRejecting(true)}>
                Reject
              </button>
            )}

            {captions && captions.cues.length > 0 && (
              <button className="btn btn-sm btn-ghost" onClick={() => setShowCues((v) => !v)}>
                {showCues ? "Hide captions" : `Captions (${captions.cue_count})`}
              </button>
            )}
          </div>

          {rejecting && (
            <div className="stack" style={{ gap: 8, marginTop: 4 }}>
              <Banner tone="warn">
                A rejected clip can be rendered again. The version you are looking at now is kept,
                never overwritten.
              </Banner>
              <textarea
                className="textarea"
                placeholder="What is wrong with it? This is required."
                value={reason}
                onChange={(e) => setReason(e.target.value)}
              />
              <div className="row" style={{ gap: 8 }}>
                <button
                  className="btn btn-sm btn-danger"
                  disabled={reason.trim().length < 3 || busy !== null}
                  onClick={() =>
                    void act("reject", async () => {
                      await api(`${base}/reject`, {
                        method: "POST",
                        body: { reason: reason.trim() },
                      });
                      setRejecting(false);
                      setReason("");
                    })
                  }
                >
                  Confirm rejection
                </button>
                <button className="btn btn-sm btn-ghost" onClick={() => setRejecting(false)}>
                  Cancel
                </button>
              </div>
            </div>
          )}
        </div>
      </div>

      {showCues && captions && <CueList cues={captions.cues} duration={clip.duration_ms} />}

      {versions && versions.length > 1 && (
        <div style={{ marginTop: 14, borderTop: "1px solid var(--line)", paddingTop: 12 }}>
          <div className="eyebrow" style={{ marginBottom: 8 }}>
            Versions
          </div>
          <div className="list">
            {versions.map((version) => (
              <div className="list-row" key={version.id}>
                <span className="mono" style={{ width: 30, flex: "none" }}>
                  v{version.version_no}
                </span>
                <div className="list-link faint">
                  {version.width}×{version.height} · {fmtBytes(version.bytes)} ·{" "}
                  {version.render_seconds?.toFixed(1) ?? "?"}s · {version.source}
                </div>
                {version.id === clip.current_version_id ? (
                  <span className="badge" data-tone="good">
                    <span className="dot" /> current
                  </span>
                ) : (
                  <button
                    className="btn btn-sm btn-ghost"
                    disabled={busy !== null}
                    onClick={() =>
                      void act("restore", () =>
                        api(`${base}/versions/${version.id}/restore`, { method: "POST" }),
                      )
                    }
                  >
                    Restore
                  </button>
                )}
              </div>
            ))}
          </div>
          <p className="hint" style={{ marginBottom: 0 }}>
            Restoring points the clip back at an earlier render. Nothing is re-encoded or deleted.
          </p>
        </div>
      )}

      {!versions && <SkeletonRows rows={1} />}
    </div>
  );
}

function CueList({ cues, duration: clipDuration }: { cues: CaptionCue[]; duration: number }) {
  if (cues.length === 0) return <Empty>No cues were produced for this clip.</Empty>;
  const last = cues[cues.length - 1];
  return (
    <div style={{ marginTop: 14, borderTop: "1px solid var(--line)", paddingTop: 12 }}>
      <div className="eyebrow" style={{ marginBottom: 8 }}>
        Captions, from stored word timings
      </div>
      <div style={{ maxHeight: 260, overflowY: "auto" }}>
        {cues.map((cue) => (
          <div className="cue-row" key={cue.idx}>
            <span className="mono faint">
              {timecode(cue.start_ms)}–{timecode(cue.end_ms)}
            </span>
            <span className="cue-text">{cue.text}</span>
            <span className="mono faint" style={{ textAlign: "right" }}>
              {cue.word_count}w
            </span>
          </div>
        ))}
      </div>
      {last.end_ms > clipDuration && (
        <Banner tone="bad">A cue runs past the end of the clip. This should never happen.</Banner>
      )}
    </div>
  );
}
