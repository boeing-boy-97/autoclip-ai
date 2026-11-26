-- =====================================================================================
-- AutoClipper AI — Phase 0 schema (PostgreSQL 17)
-- This is the authoritative logical schema. Alembic revisions in Phase 1 are generated to
-- match it exactly; a CI check diffs `alembic upgrade head` against this file so drift fails.
--
-- Conventions
--   * ids:            uuid v4, PK, default gen_random_uuid()
--   * timestamps:     timestamptz, UTC; created_at/updated_at on every mutable table
--   * media intervals: integer MILLISECONDS, half-open [start_ms, end_ms)
--   * money:          numeric(12,6) USD
--   * soft delete:    deleted_at timestamptz NULL on user-facing aggregates
--   * secrets:        bytea (AES-256-GCM ciphertext) + key_version int; never text
-- =====================================================================================

CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- ------------------------------------------------------------------ enums
CREATE TYPE user_status          AS ENUM ('active','suspended','deleted');
CREATE TYPE workspace_role       AS ENUM ('owner','admin','editor','viewer');
CREATE TYPE project_status       AS ENUM (
  'CREATED','SOURCE_VALIDATION','INGESTING','MEDIA_INSPECTION','AUDIO_EXTRACTION',
  'TRANSCRIPTION','VISUAL_ANALYSIS','SEMANTIC_ANALYSIS','MOMENT_DETECTION','CLIP_RANKING',
  'CLIP_GENERATION','VIDEO_RENDERING','CAPTION_RENDERING','AUDIO_ENHANCEMENT','QUALITY_CHECK',
  'AUTO_REPAIR','AWAITING_REVIEW','APPROVED','PUBLISHING','SCHEDULED','PUBLISHED','COMPLETED',
  'FAILED','CANCELLED');
CREATE TYPE source_kind          AS ENUM ('upload','youtube','cloud_storage');
CREATE TYPE source_state         AS ENUM ('pending','validating','ready','rejected','deleted');
CREATE TYPE media_asset_kind     AS ENUM ('source_video','audio_master','audio_asr','proxy_preview',
                                          'scene_thumbnail','frame_sample','clip_video','clip_thumbnail',
                                          'caption_ass','caption_vtt','thumbnail_image','qa_frame','render_log');
CREATE TYPE job_status           AS ENUM ('PENDING','RUNNING','SUCCEEDED','FAILED','RETRYING',
                                          'CANCELLED','DEAD_LETTER');
CREATE TYPE clip_state           AS ENUM ('DRAFT','RENDERING','QA_FAILED','QA_PASSED','AWAITING_REVIEW',
                                          'APPROVED','REJECTED','PUBLISHED','ARCHIVED');
CREATE TYPE qa_verdict           AS ENUM ('pass','warn','fail');
CREATE TYPE qa_kind              AS ENUM ('technical','editorial','duplicate','safety');
CREATE TYPE publish_state        AS ENUM ('queued','scheduled','uploading','processing','published',
                                          'failed','cancelled');
CREATE TYPE publish_mode         AS ENUM ('auto','manual_approval','private_review');
CREATE TYPE privacy_status       AS ENUM ('private','unlisted','public');
CREATE TYPE channel_state        AS ENUM ('connected','token_expired','revoked','error');
CREATE TYPE notification_kind    AS ENUM ('analysis_complete','clips_ready','qa_failed','approval_needed',
                                          'publish_success','publish_failed','auth_expired','budget_warning',
                                          'storage_warning','system');
CREATE TYPE moment_category      AS ENUM ('hook','surprising_fact','educational','business_insight',
                                          'motivational','funny','emotional_story','debate','opinion',
                                          'important_question','unexpected_answer','tutorial',
                                          'memorable_conclusion','reaction','other');

-- ------------------------------------------------------------------ identity
CREATE TABLE users (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  email           citext NOT NULL UNIQUE,
  password_hash   text NOT NULL,                          -- argon2id
  display_name    text NOT NULL,
  status          user_status NOT NULL DEFAULT 'active',
  email_verified_at timestamptz,
  locale          text NOT NULL DEFAULT 'en',
  last_login_at   timestamptz,
  created_at      timestamptz NOT NULL DEFAULT now(),
  updated_at      timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE workspaces (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  name            text NOT NULL,
  slug            citext NOT NULL UNIQUE,
  owner_user_id   uuid NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  default_language text NOT NULL DEFAULT 'en',
  target_audience text,
  budget_usd_monthly numeric(12,6),
  storage_quota_mb   integer,
  settings        jsonb NOT NULL DEFAULT '{}'::jsonb,     -- editing prefs, caption defaults, branding
  created_at      timestamptz NOT NULL DEFAULT now(),
  updated_at      timestamptz NOT NULL DEFAULT now(),
  deleted_at      timestamptz
);

CREATE TABLE workspace_members (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id    uuid NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
  user_id         uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  role            workspace_role NOT NULL DEFAULT 'editor',
  invited_by      uuid REFERENCES users(id),
  created_at      timestamptz NOT NULL DEFAULT now(),
  UNIQUE (workspace_id, user_id)
);
CREATE INDEX ix_wm_user ON workspace_members(user_id);

CREATE TABLE sessions (                                    -- opaque server-side sessions
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id         uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  token_hash      bytea NOT NULL UNIQUE,                   -- sha256 of cookie value
  csrf_hash       bytea NOT NULL,
  user_agent      text,
  ip              inet,
  expires_at      timestamptz NOT NULL,
  revoked_at      timestamptz,
  created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_sessions_user ON sessions(user_id) WHERE revoked_at IS NULL;

-- ------------------------------------------------------------------ projects & sources
CREATE TABLE projects (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id    uuid NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
  created_by      uuid NOT NULL REFERENCES users(id),
  name            text NOT NULL,
  status          project_status NOT NULL DEFAULT 'CREATED',
  target_platform text NOT NULL DEFAULT 'youtube_shorts',
  language        text NOT NULL DEFAULT 'en',
  clips_requested integer NOT NULL DEFAULT 5  CHECK (clips_requested BETWEEN 1 AND 30),
  min_duration_ms integer NOT NULL DEFAULT 15000 CHECK (min_duration_ms >= 3000),
  max_duration_ms integer NOT NULL DEFAULT 65000 CHECK (max_duration_ms <= 180000),
  preferred_durations_ms integer[] NOT NULL DEFAULT '{15000,30000,45000}',
  template_key    text NOT NULL DEFAULT 'podcast',
  caption_style_key text NOT NULL DEFAULT 'bold_center',
  branding        jsonb NOT NULL DEFAULT '{}'::jsonb,
  publishing_mode publish_mode NOT NULL DEFAULT 'manual_approval',
  channel_id      uuid,                                    -- FK added after youtube_channels
  schedule_config jsonb NOT NULL DEFAULT '{}'::jsonb,
  budget_usd      numeric(12,6),
  cost_usd        numeric(12,6) NOT NULL DEFAULT 0,
  progress        smallint CHECK (progress BETWEEN 0 AND 100),
  error_code      text,
  error_detail    text,
  created_at      timestamptz NOT NULL DEFAULT now(),
  updated_at      timestamptz NOT NULL DEFAULT now(),
  deleted_at      timestamptz,
  CONSTRAINT ck_project_durations CHECK (min_duration_ms < max_duration_ms)
);
CREATE INDEX ix_projects_ws_status ON projects(workspace_id, status) WHERE deleted_at IS NULL;
CREATE INDEX ix_projects_created  ON projects(workspace_id, created_at DESC) WHERE deleted_at IS NULL;

-- Append-only state machine journal.
CREATE TABLE project_stage_events (
  id              bigserial PRIMARY KEY,
  project_id      uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  from_status     project_status,
  to_status       project_status NOT NULL,
  task_name       text,
  job_id          uuid,
  progress        smallint CHECK (progress BETWEEN 0 AND 100),
  error_code      text,
  detail          text,
  at              timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_pse_project_at ON project_stage_events(project_id, at DESC);

CREATE TABLE source_videos (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  project_id      uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  workspace_id    uuid NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
  kind            source_kind NOT NULL,
  state           source_state NOT NULL DEFAULT 'pending',
  original_filename text,
  detected_mime   text,
  bytes           bigint CHECK (bytes > 0),
  sha256          bytea,
  storage_key     text,                                    -- object store key (never public)
  youtube_video_id text,
  youtube_metadata jsonb,                                  -- oEmbed/API public metadata only
  duration_ms     integer,
  width           integer,
  height          integer,
  fps             numeric(8,3),
  video_codec     text,
  audio_codec     text,
  audio_channels  smallint,
  audio_sample_rate integer,
  probe_json      jsonb,                                   -- raw ffprobe for auditability
  -- Compliance: an explicit authorization record is REQUIRED before ingestion proceeds.
  rights_basis    text,                                    -- 'original'|'licensed'|'permission'|'cc'|'owned_channel'
  rights_statement text,
  rights_confirmed_at timestamptz,
  rights_confirmed_by uuid REFERENCES users(id),
  created_at      timestamptz NOT NULL DEFAULT now(),
  updated_at      timestamptz NOT NULL DEFAULT now(),
  deleted_at      timestamptz,
  CONSTRAINT ck_source_youtube CHECK (kind <> 'youtube' OR youtube_video_id IS NOT NULL),
  CONSTRAINT ck_source_upload  CHECK (kind <> 'upload' OR storage_key IS NOT NULL)
);
CREATE INDEX ix_sources_project ON source_videos(project_id);
CREATE UNIQUE INDEX ux_sources_sha_project ON source_videos(project_id, sha256)
  WHERE sha256 IS NOT NULL AND deleted_at IS NULL;
-- Composite key target so clips can enforce "source belongs to the same project" at the DB level.
ALTER TABLE source_videos ADD CONSTRAINT uq_source_id_project UNIQUE (id, project_id);

CREATE TABLE media_assets (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id    uuid NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
  project_id      uuid REFERENCES projects(id) ON DELETE CASCADE,
  clip_id         uuid,                                    -- FK added later
  kind            media_asset_kind NOT NULL,
  storage_key     text NOT NULL,
  bytes           bigint,
  sha256          bytea,
  mime            text,
  duration_ms     integer,
  width           integer,
  height          integer,
  meta            jsonb NOT NULL DEFAULT '{}'::jsonb,
  expires_at      timestamptz,                             -- for ephemeral derived files
  created_at      timestamptz NOT NULL DEFAULT now(),
  deleted_at      timestamptz,
  UNIQUE (storage_key)
);
CREATE INDEX ix_assets_project_kind ON media_assets(project_id, kind);

-- ------------------------------------------------------------------ transcription
CREATE TABLE transcripts (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  project_id      uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  source_video_id uuid NOT NULL REFERENCES source_videos(id) ON DELETE CASCADE,
  provider        text NOT NULL,                           -- 'faster-whisper'|'provider-x'
  model           text NOT NULL,
  language        text,
  is_corrected    boolean NOT NULL DEFAULT false,          -- corrected copies are ADDITIONAL rows
  corrected_from  uuid REFERENCES transcripts(id),
  word_timestamps boolean NOT NULL DEFAULT true,
  confidence      numeric(4,3),
  cost_usd        numeric(12,6) NOT NULL DEFAULT 0,
  raw_json        jsonb,                                   -- provider payload, kept for audit
  created_at      timestamptz NOT NULL DEFAULT now(),
  UNIQUE (source_video_id, provider, model, is_corrected)
);

CREATE TABLE speakers (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  project_id      uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  label           text NOT NULL,
  display_name    text,
  method          text NOT NULL DEFAULT 'heuristic',       -- 'pyannote'|'heuristic'|'vision'
  confidence      numeric(4,3),
  color           text,
  created_at      timestamptz NOT NULL DEFAULT now(),
  UNIQUE (project_id, label)
);

CREATE TABLE transcript_segments (
  id              bigserial PRIMARY KEY,
  transcript_id   uuid NOT NULL REFERENCES transcripts(id) ON DELETE CASCADE,
  project_id      uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  idx             integer NOT NULL,
  start_ms        integer NOT NULL,
  end_ms          integer NOT NULL,
  text            text NOT NULL,
  speaker_id      uuid REFERENCES speakers(id),
  avg_logprob     numeric(6,4),
  no_speech_prob  numeric(6,4),
  words           jsonb,                                   -- [{w,s,e,p}]
  CONSTRAINT ck_seg_range CHECK (start_ms >= 0 AND end_ms > start_ms)
);
CREATE UNIQUE INDEX ux_seg_transcript_idx ON transcript_segments(transcript_id, idx);
CREATE INDEX ix_seg_project_time ON transcript_segments(project_id, start_ms);
-- Fast full-text lookup for quote grounding / QA cross-checks.
CREATE INDEX ix_seg_text_trgm ON transcript_segments USING gin (text gin_trgm_ops);

-- ------------------------------------------------------------------ visual & semantic analysis
CREATE TABLE scenes (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  project_id      uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  idx             integer NOT NULL,
  start_ms        integer NOT NULL,
  end_ms          integer NOT NULL,
  detector        text NOT NULL DEFAULT 'pyscenedetect',
  description     text,
  has_on_screen_text boolean,
  thumbnail_asset_id uuid REFERENCES media_assets(id),
  mean_motion     numeric(8,4),
  CONSTRAINT ck_scene_range CHECK (start_ms >= 0 AND end_ms > start_ms),
  UNIQUE (project_id, idx)
);

CREATE TABLE video_analysis (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  project_id      uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  transcript_id   uuid REFERENCES transcripts(id),
  schema_version  integer NOT NULL DEFAULT 1,
  video_summary   text,
  language        text,
  analysis_confidence numeric(4,3),
  payload         jsonb NOT NULL,                          -- fully validated AnalysisPayload
  provider        text,
  model           text,
  cost_usd        numeric(12,6) NOT NULL DEFAULT 0,
  created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX ux_analysis_project_schema ON video_analysis(project_id, schema_version);

CREATE TABLE topics (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  analysis_id     uuid NOT NULL REFERENCES video_analysis(id) ON DELETE CASCADE,
  project_id      uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  label           text NOT NULL,
  start_ms        integer,
  end_ms          integer,
  confidence      numeric(4,3),
  embedding       vector(1536)                            -- requires pgvector; optional extension
);

CREATE TABLE important_moments (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  analysis_id     uuid NOT NULL REFERENCES video_analysis(id) ON DELETE CASCADE,
  project_id      uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  start_ms        integer NOT NULL,
  end_ms          integer NOT NULL,
  kind            moment_category NOT NULL DEFAULT 'other',
  why             text,
  quote           text,
  speaker_label   text,
  CONSTRAINT ck_moment_range CHECK (start_ms >= 0 AND end_ms > start_ms)
);

-- ------------------------------------------------------------------ clip discovery
CREATE TABLE candidate_moments (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  project_id      uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  analysis_id     uuid REFERENCES video_analysis(id),
  start_ms        integer NOT NULL,
  end_ms          integer NOT NULL,
  required_duration_ms integer NOT NULL,
  category        moment_category NOT NULL DEFAULT 'other',
  hook_text       text,
  context_text    text,
  payoff_text     text,
  ending_text     text,
  rationale       text,
  standalone_ok   boolean NOT NULL DEFAULT false,
  selected        boolean NOT NULL DEFAULT false,
  suppressed_reason text,                                  -- 'overlap'|'duplicate'|'topic_quota'|null
  embedding       vector(1536),                            -- optional pgvector
  created_at      timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT ck_cand_range CHECK (start_ms >= 0 AND end_ms > start_ms),
  CONSTRAINT ck_cand_dur   CHECK (end_ms - start_ms = required_duration_ms)
);
CREATE INDEX ix_cand_project_time ON candidate_moments(project_id, start_ms);

CREATE TABLE candidate_scores (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  candidate_id    uuid NOT NULL UNIQUE REFERENCES candidate_moments(id) ON DELETE CASCADE,
  weights_version text NOT NULL,
  hook            numeric(5,2) NOT NULL,
  content_value   numeric(5,2) NOT NULL,
  narrative       numeric(5,2) NOT NULL,
  standalone      numeric(5,2) NOT NULL,
  audience        numeric(5,2) NOT NULL,
  emotion         numeric(5,2) NOT NULL,
  visual_quality  numeric(5,2) NOT NULL,
  audio_quality   numeric(5,2) NOT NULL,
  ending_strength numeric(5,2) NOT NULL,
  clarity         numeric(5,2) NOT NULL,
  -- Decomposed aggregates: never collapse technical quality into engagement.
  content_potential   numeric(5,2) NOT NULL,
  technical_quality   numeric(5,2) NOT NULL,
  context_completeness numeric(5,2) NOT NULL,
  audience_relevance  numeric(5,2) NOT NULL,
  overall         numeric(5,2) NOT NULL,
  computed_at     timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT ck_scores_unit CHECK (
    overall BETWEEN 0 AND 100 AND content_potential BETWEEN 0 AND 100 AND
    technical_quality BETWEEN 0 AND 100 AND context_completeness BETWEEN 0 AND 100 AND
    audience_relevance BETWEEN 0 AND 100)
);

-- ------------------------------------------------------------------ clips
CREATE TABLE clips (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  project_id      uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  workspace_id    uuid NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
  source_video_id uuid NOT NULL,
  candidate_moment_id uuid REFERENCES candidate_moments(id) ON DELETE SET NULL,
  title           text NOT NULL,
  start_ms        integer NOT NULL,
  end_ms          integer NOT NULL,
  duration_ms     integer NOT NULL,
  state           clip_state NOT NULL DEFAULT 'DRAFT',
  layout          text NOT NULL DEFAULT 'single_subject',  -- single_subject|split_screen|letterbox
  template_key    text NOT NULL DEFAULT 'podcast',
  current_version_id uuid,                                 -- FK added after clip_versions
  metadata        jsonb NOT NULL DEFAULT '{}'::jsonb,      -- titles/description/tags/hashtags
  thumbnail_asset_id uuid REFERENCES media_assets(id),
  publishable     boolean NOT NULL DEFAULT false,
  rejection_reason text,
  approved_by     uuid REFERENCES users(id),
  approved_at     timestamptz,
  created_at      timestamptz NOT NULL DEFAULT now(),
  updated_at      timestamptz NOT NULL DEFAULT now(),
  deleted_at      timestamptz,
  CONSTRAINT ck_clip_range CHECK (start_ms >= 0 AND end_ms > start_ms),
  CONSTRAINT ck_clip_dur   CHECK (duration_ms = end_ms - start_ms),
  -- Source must belong to the same project.
  FOREIGN KEY (source_video_id, project_id) REFERENCES source_videos(id, project_id)
);
CREATE INDEX ix_clips_project_state ON clips(project_id, state) WHERE deleted_at IS NULL;
CREATE INDEX ix_clips_ws_created ON clips(workspace_id, created_at DESC) WHERE deleted_at IS NULL;

CREATE TABLE clip_versions (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  clip_id         uuid NOT NULL REFERENCES clips(id) ON DELETE CASCADE,
  version_no      integer NOT NULL,
  render_plan     jsonb NOT NULL,                          -- declarative, compiled to FFmpeg
  render_plan_version integer NOT NULL DEFAULT 1,
  video_asset_id  uuid REFERENCES media_assets(id),
  caption_asset_id uuid REFERENCES media_assets(id),
  thumbnail_asset_id uuid REFERENCES media_assets(id),
  width           integer,
  height          integer,
  duration_ms     integer,
  bytes           bigint,
  sha256          bytea,
  render_seconds  numeric(10,3),
  encoder_log_ref text,
  source          text NOT NULL DEFAULT 'initial',          -- initial|user_edit|repair
  parent_version_id uuid REFERENCES clip_versions(id),
  created_by      uuid REFERENCES users(id),
  rendered_at     timestamptz,
  created_at      timestamptz NOT NULL DEFAULT now(),
  UNIQUE (clip_id, version_no)
);
ALTER TABLE clips ADD CONSTRAINT fk_clips_current_version
  FOREIGN KEY (current_version_id) REFERENCES clip_versions(id) DEFERRABLE INITIALLY DEFERRED;

CREATE TABLE clip_edit_settings (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  clip_id         uuid NOT NULL UNIQUE REFERENCES clips(id) ON DELETE CASCADE,
  settings        jsonb NOT NULL,                          -- trim, crop offsets, caption style/pos, overlays, music
  updated_by      uuid REFERENCES users(id),
  updated_at      timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE captions (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  clip_id         uuid NOT NULL REFERENCES clips(id) ON DELETE CASCADE,
  clip_version_id uuid REFERENCES clip_versions(id) ON DELETE CASCADE,
  language        text NOT NULL DEFAULT 'en',
  style_key       text NOT NULL DEFAULT 'bold_center',
  format          text NOT NULL DEFAULT 'ass',
  ass_text        text,                                    -- editable source of truth
  vtt_text        text,
  cues            jsonb NOT NULL,                           -- [{start_ms,end_ms,words[],text}]
  validation      jsonb NOT NULL DEFAULT '{}'::jsonb,       -- timing validator output
  created_at      timestamptz NOT NULL DEFAULT now(),
  updated_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_captions_clip ON captions(clip_id);

-- ------------------------------------------------------------------ QA
CREATE TABLE quality_reports (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  clip_id         uuid NOT NULL REFERENCES clips(id) ON DELETE CASCADE,
  clip_version_id uuid NOT NULL REFERENCES clip_versions(id) ON DELETE CASCADE,
  kind            qa_kind NOT NULL,
  verdict         qa_verdict NOT NULL,
  score           numeric(5,2),
  checks          jsonb NOT NULL,                           -- [{key,verdict,measured,expected,detail,blocking}]
  blocking_failures text[] NOT NULL DEFAULT '{}',
  inspector_version text NOT NULL,
  duration_ms     integer,
  created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_qr_version ON quality_reports(clip_version_id, kind);
-- A version is publishable only with a passing technical report.
CREATE UNIQUE INDEX ux_qr_one_pass_technical ON quality_reports(clip_version_id)
  WHERE kind = 'technical' AND verdict = 'pass';

CREATE TABLE repair_attempts (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  clip_id         uuid NOT NULL REFERENCES clips(id) ON DELETE CASCADE,
  from_version_id uuid REFERENCES clip_versions(id),
  to_version_id   uuid REFERENCES clip_versions(id),
  failed_check    text NOT NULL,
  action          text NOT NULL,                            -- recrop|regen_captions|shift_start|extend_end|loudnorm|swap_candidate|fallback_layout
  succeeded       boolean,
  detail          text,
  attempt_no      smallint NOT NULL,
  created_at      timestamptz NOT NULL DEFAULT now(),
  UNIQUE (clip_id, failed_check, attempt_no)
);

-- ------------------------------------------------------------------ YouTube
CREATE TABLE youtube_channels (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id    uuid NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
  connected_by    uuid NOT NULL REFERENCES users(id),
  channel_id      text NOT NULL,                            -- YouTube channel id (UC...)
  channel_title   text NOT NULL,
  channel_handle  text,
  avatar_url      text,
  state           channel_state NOT NULL DEFAULT 'connected',
  default_language text NOT NULL DEFAULT 'en',
  default_caption_style text NOT NULL DEFAULT 'bold_center',
  branding        jsonb NOT NULL DEFAULT '{}'::jsonb,
  publishing_prefs jsonb NOT NULL DEFAULT '{}'::jsonb,      -- cadence, timezone, default privacy
  timezone        text NOT NULL DEFAULT 'UTC',
  analytics_granted boolean NOT NULL DEFAULT false,
  scopes          text[] NOT NULL DEFAULT '{}',
  last_synced_at  timestamptz,
  created_at      timestamptz NOT NULL DEFAULT now(),
  updated_at      timestamptz NOT NULL DEFAULT now(),
  deleted_at      timestamptz,
  UNIQUE (workspace_id, channel_id)                          -- same channel may exist in 2 workspaces
);
ALTER TABLE projects ADD CONSTRAINT fk_projects_channel
  FOREIGN KEY (channel_id) REFERENCES youtube_channels(id) ON DELETE SET NULL;

CREATE TABLE oauth_credentials (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  channel_id      uuid NOT NULL UNIQUE REFERENCES youtube_channels(id) ON DELETE CASCADE,
  access_token_enc  bytea NOT NULL,
  refresh_token_enc bytea,
  token_type      text,
  scopes          text[] NOT NULL DEFAULT '{}',
  key_version     smallint NOT NULL DEFAULT 1,
  access_expires_at timestamptz,
  rotated_at      timestamptz,
  created_at      timestamptz NOT NULL DEFAULT now(),
  updated_at      timestamptz NOT NULL DEFAULT now()
);

-- OAuth CSRF state, single-use.
CREATE TABLE oauth_states (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  state_hash      bytea NOT NULL UNIQUE,
  pkce_verifier_enc bytea NOT NULL,
  workspace_id    uuid NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
  user_id         uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  redirect_path   text,
  expires_at      timestamptz NOT NULL,
  consumed_at     timestamptz,
  created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE publishing_jobs (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id    uuid NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
  clip_id         uuid NOT NULL REFERENCES clips(id) ON DELETE CASCADE,
  clip_version_id uuid NOT NULL REFERENCES clip_versions(id),
  channel_id      uuid NOT NULL REFERENCES youtube_channels(id) ON DELETE CASCADE,
  state           publish_state NOT NULL DEFAULT 'queued',
  mode            publish_mode NOT NULL DEFAULT 'manual_approval',
  privacy         privacy_status NOT NULL DEFAULT 'private',
  scheduled_at    timestamptz,                              -- UTC
  idempotency_key text NOT NULL,
  attempts        smallint NOT NULL DEFAULT 0,
  bytes_uploaded  bigint NOT NULL DEFAULT 0,
  total_bytes     bigint,
  progress        smallint CHECK (progress BETWEEN 0 AND 100),
  youtube_video_id text,
  youtube_url     text,
  error_code      text,
  error_detail    text,
  next_retry_at   timestamptz,
  started_at      timestamptz,
  finished_at     timestamptz,
  created_by      uuid REFERENCES users(id),
  created_at      timestamptz NOT NULL DEFAULT now(),
  updated_at      timestamptz NOT NULL DEFAULT now(),
  UNIQUE (idempotency_key),
  -- Duplicate-post defence #1: one live job per (clip, channel).
  CONSTRAINT ck_publish_requires_private_when_scheduled
    CHECK (scheduled_at IS NULL OR privacy <> 'public')
);
CREATE UNIQUE INDEX ux_publish_live ON publishing_jobs(clip_id, channel_id)
  WHERE state IN ('queued','uploading','processing','scheduled','published');
CREATE INDEX ix_publish_due ON publishing_jobs(scheduled_at) WHERE state = 'scheduled';
CREATE INDEX ix_publish_ws_state ON publishing_jobs(workspace_id, state, created_at DESC);

CREATE TABLE publishing_records (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  publishing_job_id uuid NOT NULL REFERENCES publishing_jobs(id) ON DELETE CASCADE,
  event           text NOT NULL,                             -- upload_started|chunk|uploaded|processing|published|failed
  at              timestamptz NOT NULL DEFAULT now(),
  detail          jsonb,
  api_request_id  text,
  units_consumed  integer
);
CREATE INDEX ix_prec_job ON publishing_records(publishing_job_id, at);

CREATE TABLE analytics_snapshots (
  id              bigserial PRIMARY KEY,
  workspace_id    uuid NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
  channel_id      uuid NOT NULL REFERENCES youtube_channels(id) ON DELETE CASCADE,
  youtube_video_id text NOT NULL,
  clip_id         uuid REFERENCES clips(id) ON DELETE SET NULL,
  captured_at     timestamptz NOT NULL DEFAULT now(),
  period_start    date NOT NULL,
  period_end      date NOT NULL,
  views           bigint,
  watch_time_minutes numeric(14,2),
  avg_view_duration_s numeric(10,2),
  avg_view_percentage numeric(6,3),
  likes           bigint,
  comments        bigint,
  shares          bigint,
  subscriber_delta integer,
  traffic_sources jsonb,
  raw             jsonb,
  CONSTRAINT ck_snap_period CHECK (period_end >= period_start)
);
CREATE UNIQUE INDEX ux_snap_unique ON analytics_snapshots(channel_id, youtube_video_id, period_start, period_end);
CREATE INDEX ix_snap_clip ON analytics_snapshots(clip_id, captured_at DESC);

-- ------------------------------------------------------------------ jobs, usage, notifications, audit
CREATE TABLE processing_jobs (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id    uuid NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
  project_id      uuid REFERENCES projects(id) ON DELETE CASCADE,
  clip_id         uuid REFERENCES clips(id) ON DELETE CASCADE,
  stage           text NOT NULL,                             -- 'transcription'|'render'|...
  queue           text NOT NULL,
  task_name       text NOT NULL,
  celery_task_id  text UNIQUE,
  status          job_status NOT NULL DEFAULT 'PENDING',
  progress        smallint CHECK (progress BETWEEN 0 AND 100),
  progress_note   text,
  input_fingerprint text,                                    -- idempotency
  attempts        smallint NOT NULL DEFAULT 0,
  max_attempts    smallint NOT NULL DEFAULT 3,
  timeout_seconds integer,
  heartbeat_at    timestamptz,
  next_retry_at   timestamptz,
  error_code      text,
  error_detail    text,
  trace_id        text,
  started_at      timestamptz,
  finished_at     timestamptz,
  created_at      timestamptz NOT NULL DEFAULT now(),
  updated_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_jobs_project ON processing_jobs(project_id, created_at DESC);
CREATE INDEX ix_jobs_status ON processing_jobs(status, next_retry_at);
CREATE UNIQUE INDEX ux_jobs_idempotent ON processing_jobs(project_id, stage, input_fingerprint)
  WHERE input_fingerprint IS NOT NULL AND status IN ('PENDING','RUNNING','RETRYING','SUCCEEDED');
CREATE INDEX ix_jobs_heartbeat ON processing_jobs(status, heartbeat_at) WHERE status = 'RUNNING';

CREATE TABLE notifications (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id    uuid NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
  user_id         uuid REFERENCES users(id) ON DELETE CASCADE,
  kind            notification_kind NOT NULL,
  title           text NOT NULL,
  body            text,
  project_id      uuid REFERENCES projects(id) ON DELETE CASCADE,
  clip_id         uuid REFERENCES clips(id) ON DELETE CASCADE,
  read_at         timestamptz,
  created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_notif_user_unread ON notifications(user_id, created_at DESC) WHERE read_at IS NULL;

CREATE TABLE usage_records (
  id              bigserial PRIMARY KEY,
  workspace_id    uuid NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
  project_id      uuid REFERENCES projects(id) ON DELETE SET NULL,
  clip_id         uuid REFERENCES clips(id) ON DELETE SET NULL,
  job_id          uuid REFERENCES processing_jobs(id) ON DELETE SET NULL,
  category        text NOT NULL,                             -- llm|stt|vision|render|storage|youtube
  provider        text,
  model           text,
  quantity        numeric(16,4) NOT NULL,
  unit            text NOT NULL,                             -- tokens|audio_seconds|frames|render_seconds|bytes|api_units
  unit_cost_usd   numeric(12,8) NOT NULL,
  cost_usd        numeric(12,6) NOT NULL,
  cached          boolean NOT NULL DEFAULT false,
  at              timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_usage_ws_at ON usage_records(workspace_id, at DESC);
CREATE INDEX ix_usage_project ON usage_records(project_id);

CREATE TABLE audit_logs (
  id              bigserial PRIMARY KEY,
  workspace_id    uuid REFERENCES workspaces(id) ON DELETE SET NULL,
  actor_user_id   uuid REFERENCES users(id) ON DELETE SET NULL,
  action          text NOT NULL,                             -- 'project.create','channel.connect','clip.publish',...
  target_type     text,
  target_id       uuid,
  ip              inet,
  user_agent      text,
  detail          jsonb NOT NULL DEFAULT '{}'::jsonb,
  at              timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_audit_ws_at ON audit_logs(workspace_id, at DESC);
CREATE INDEX ix_audit_action ON audit_logs(action);

-- ------------------------------------------------------------------ helper trigger
CREATE OR REPLACE FUNCTION set_updated_at() RETURNS trigger AS $$
BEGIN NEW.updated_at = now(); RETURN NEW; END;
$$ LANGUAGE plpgsql;

DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['users','workspaces','projects','source_videos','clips',
                           'youtube_channels','oauth_credentials','publishing_jobs',
                           'processing_jobs','clip_edit_settings']
  LOOP
    EXECUTE format('CREATE TRIGGER trg_%1$s_updated BEFORE UPDATE ON %1$s
                    FOR EACH ROW EXECUTE FUNCTION set_updated_at()', t);
  END LOOP;
END $$;

-- ------------------------------------------------------------------ notes
-- * `vector(1536)` requires the pgvector extension. It is OPTIONAL: if unavailable, the
--   columns are dropped by migration 0002 and semantic dedupe falls back to token-set cosine.
-- * `citext` requires the citext extension (created in migration 0001 alongside pgcrypto).
-- * Append-only tables (project_stage_events, publishing_records, analytics_snapshots,
--   audit_logs, usage_records) have no UPDATE/DELETE grants for the application DB role in
--   production; only INSERT/SELECT.
