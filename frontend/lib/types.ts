/** Wire types, mirroring backend/app/api/schemas.py.
 *
 *  Hand-written from real response bodies rather than generated, so the UI cannot quietly depend on
 *  a field the API does not actually send. Anything optional here was verified to be null in a live
 *  response before it was made optional.
 */

export type WorkspaceSummary = { id: string; name: string; slug: string; role: string };

export type User = {
  id: string;
  email: string;
  display_name: string;
  workspaces: WorkspaceSummary[];
};

export type Session = { user: User; csrf_token: string; expires_at: string };

export type Source = {
  id: string;
  kind: string;
  state: string;
  original_filename: string | null;
  duration_ms: number | null;
  width: number | null;
  height: number | null;
  fps: number | null;
  video_codec: string | null;
  audio_codec: string | null;
  byte_size: number | null;
  sha256: string | null;
  rights_basis: string | null;
  rights_confirmed_at: string | null;
  probe: Record<string, unknown> | null;
};

export type Project = {
  id: string;
  workspace_id: string;
  name: string;
  status: string;
  progress: number | null;
  language: string;
  target_platform: string;
  clips_requested: number;
  min_duration_ms: number;
  max_duration_ms: number;
  template_key: string;
  caption_style_key: string;
  publishing_mode: string;
  budget_usd: number | null;
  cost_usd: number;
  error_code: string | null;
  error_detail: string | null;
  created_at: string;
  updated_at: string;
  source: Source | null;
};

export type StageEvent = {
  from_status: string | null;
  to_status: string;
  task_name: string | null;
  job_id: string | null;
  progress: number | null;
  error_code: string | null;
  detail: string | null;
  at: string;
};

export type JobStatus =
  | "PENDING"
  | "RUNNING"
  | "SUCCEEDED"
  | "FAILED"
  | "CANCELLED"
  | "DEAD_LETTER";

export type Job = {
  id: string;
  project_id: string | null;
  clip_id: string | null;
  stage: string;
  queue: string;
  task_name: string;
  status: JobStatus;
  progress: number | null;
  attempts: number;
  max_attempts: number;
  error_code: string | null;
  error_detail: string | null;
  progress_note: string | null;
  started_at: string | null;
  finished_at: string | null;
  next_retry_at: string | null;
  created_at: string;
};

/** One stored score. The API returns a flat object of components, not a list. */
export type CandidateScore = {
  weights_version: string;
  hook: number;
  content_value: number;
  narrative: number;
  standalone: number;
  audience: number;
  emotion: number;
  visual_quality: number;
  audio_quality: number;
  ending_strength: number;
  clarity: number;
  content_potential: number;
  technical_quality: number;
  context_completeness: number;
  audience_relevance: number;
  overall: number;
};

export type Moment = {
  id: string;
  start_ms: number;
  end_ms: number;
  duration_ms: number;
  category: string;
  hook_text: string | null;
  context_text: string | null;
  payoff_text: string | null;
  rationale: string | null;
  standalone_ok: boolean;
  selected: boolean;
  suppressed_reason: string | null;
  score: CandidateScore | null;
};

export type Clip = {
  id: string;
  project_id: string;
  title: string;
  state: string;
  start_ms: number;
  end_ms: number;
  duration_ms: number;
  layout: string;
  template_key: string;
  publishable: boolean;
  candidate_moment_id: string | null;
  current_version_id: string | null;
  metadata: Record<string, unknown>;
  rejection_reason: string | null;
  created_at: string | null;
};

export type ClipVersion = {
  id: string;
  version_no: number;
  source: string;
  width: number | null;
  height: number | null;
  duration_ms: number | null;
  bytes: number | null;
  sha256: string | null;
  video_url: string | null;
  caption_url: string | null;
  render_seconds: number | null;
  rendered_at: string | null;
  created_at: string;
};

export type ClipRender = {
  clip_id: string;
  version_id: string;
  version_no: number;
  state: string;
  width: number;
  height: number;
  duration_ms: number;
  bytes: number;
  render_seconds: number;
  has_captions: boolean;
  cue_count: number;
  sha256: string | null;
};

export type CaptionCue = {
  idx: number;
  start_ms: number;
  end_ms: number;
  text: string;
  word_count: number;
};

export type Captions = {
  clip_id: string;
  source: string;
  clip_start_ms: number;
  clip_end_ms: number;
  cue_count: number;
  coverage: number;
  words_in_range: number;
  words_without_timing: number;
  cues: CaptionCue[];
  warnings: string[];
  vtt: string | null;
};

/** What the render actually did. Stored with the version so the file is explainable.
 *
 *  Field list copied from a live response, not from intent: there is no `layout` object and no
 *  `audio` block, and the caption style arrives as `template_key` next to `captions`. */
export type RenderPlan = {
  version: number;
  clip_id: string;
  template_key: string;
  window: { start_ms: number; end_ms: number; duration_ms: number };
  canvas: { width: number; height: number; fps: number };
  source_dimensions: { width: number | null; height: number | null };
  captions: { burned: boolean; cue_count: number };
  encoder: { codec: string; preset: string; crf: number };
  adjustments: { start_delta_ms: number; end_delta_ms: number; reason: string[] };
  warnings: string[];
};

/** `GET /clips/{id}/render-plan` wraps the plan with the version and the caption summary. */
export type RenderPlanResponse = {
  version_no: number;
  render_plan_version: number;
  render_plan: RenderPlan;
  captions: Record<string, unknown>;
};

export type SegmentWord = { w: string; s: number; e: number; p?: number | null };

export type Segment = {
  idx: number;
  start_ms: number;
  end_ms: number;
  text: string;
  speaker_id: string | null;
  avg_logprob: number | null;
  words: SegmentWord[] | null;
};

export type Transcript = {
  id: string;
  provider: string;
  model: string;
  language: string | null;
  word_timestamps: boolean;
  confidence: number | null;
  is_corrected: boolean;
  created_at: string;
  duration_ms: number | null;
  word_count: number;
  segments: Segment[];
};

export type Dashboard = {
  projects_total: number;
  projects_by_status: Record<string, number>;
  sources_processed: number;
  failed_jobs: number;
  queue_depth: number;
  storage_mb: number;
  unread_notifications: number;
  recent_projects: Project[];
};

export type Page<T> = { items: T[]; next_cursor?: string | null };
