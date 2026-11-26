# AutoClipper AI — Architecture (Phase 0)

> Status: **Phase 0 deliverable.** This document is the contract the rest of the build follows.
> No application logic exists yet. Where a decision is a *verification* rather than a design
> choice, it is marked **[VERIFY]** and must be confirmed against the live API before Phase 6
> code is written. No behaviour is assumed to work because it "should".

---

## 1. Scope and non-goals

**In scope:** one authorized long-form video → multimodal understanding → ranked moment
discovery → multiple rendered vertical clips (9:16) → captions → independent QA + auto-repair →
human review → publish to YouTube (official Data API) → analytics feedback → cost accounting.

**Explicit non-goals (v1):** bypassing access controls on any platform; downloading YouTube media
without an authorized source; claims of guaranteed virality; multi-tenant billing/payments
beyond usage accounting; a general-purpose NLE; training our own models.

**Compliance posture.** Every source video carries an explicit *authorization declaration*
(who owns it / what licence / uploader attestation) recorded immutably in `audit_logs`. A YouTube
URL alone is **never** treated as a media grant — the pipeline only auto-ingests a YouTube URL when
the channel is OAuth-connected to the workspace *and* the video is owned by that channel, or when a
rights declaration is supplied and an authorized source is available. Otherwise the user is offered
the upload-original-video path. Copyright/risk signals produced by QA are described as *indicators*,
never legal determinations.

---

## 2. System context

```
┌──────────────┐        HTTPS / WSS          ┌─────────────────────────────────────────────┐
│  Browser     │ ──────────────────────────► │  Next.js (SSR + BFF proxy)                  │
│  (Next.js)   │ ◄────────────────────────── │  • session cookie (HttpOnly, SameSite=Lax)  │
└──────────────┘                             │  • /api/* rewritten server-side to FastAPI  │
                                             └───────────────────┬─────────────────────────┘
                                                                 │ internal HTTP (no CORS in prod)
                                                                 ▼
                     ┌───────────────────────────────────────────────────────────────┐
                     │  FastAPI (uvicorn, gunicorn workers in prod)                  │
                     │  API v1 · SSE stream · signed-URL issuing · OpenAPI           │
                     └───┬───────────────┬───────────────┬───────────────┬───────────┘
                         │               │               │               │
                 ┌───────▼──────┐ ┌──────▼──────┐ ┌──────▼──────┐ ┌─────▼────────┐
                 │ PostgreSQL   │ │ Redis       │ │ S3-compatible│ │ AI providers │
                 │ (17)         │ │ • broker    │ │ object store │ │ LLM / STT /  │
                 │ state+audit  │ │ • results   │ │ (R2/S3/MinIO)│ │ vision       │
                 └───────▲──────┘ │ • locks     │ └──────▲──────┘ └─────▲────────┘
                         │        │ • SSE pubsub│        │              │
                         │        └──────▲──────┘        │              │
                         │               │               │              │
                 ┌───────┴───────────────┴───────────────┴──────────────┴────────┐
                 │  Celery workers (queues: media, transcribe, analyze, render,   │
                 │  qa, publish, analytics) + Celery Beat (scheduler)             │
                 │  FFmpeg · OpenCV · PySceneDetect · MediaPipe · faster-whisper  │
                 └────────────────────────────────────────────────────────────────┘
                                        │
                                        ▼
                             Official YouTube Data API + Analytics API
```

### Deployment topology (v1, deliberately boring)

| Unit | What it runs | Scale axis |
|---|---|---|
| `web` | Next.js standalone server | horizontal, stateless |
| `api` | FastAPI (N workers) | horizontal, stateless |
| `worker-media` | Celery, `media`/`render` queues | CPU-bound → horizontal per host |
| `worker-ai` | Celery, `transcribe`/`analyze`/`qa`/`analytics` | GPU-optional |
| `worker-publish` | Celery, `publish` queue (low concurrency) | 1–2 (quota safety) |
| `beat` | Celery Beat, single instance, `--lock` guarded | exactly 1 |
| `postgres`, `redis`, `minio` (dev) / managed (prod) | state | managed in prod |

Frontend and backend deploy separately (per spec). In production the Next.js server proxies
`/api/*` to the API service so the browser never talks cross-origin to FastAPI — this removes CORS
as a failure mode and keeps session cookies first-party. **Dev caveat:** the preview host used in
this workspace is a single proxied origin, so the dev server must bind `0.0.0.0` and accept the
proxied Host/Origin; that is configured in `frontend/next.config.mjs` and covered in `RUNBOOK.md`.

---

## 3. Data model

### 3.1 Entity map

```
users ─┬─< workspace_members >─┬─ workspaces ─┬─< projects ─┬─< source_videos ─< media_assets
       │                       │              │             ├─< transcripts ─< transcript_segments
       │                       │              │             │                  └─< speakers
       │                       │              │             ├─< scenes
       │                       │              │             ├─< video_analysis ─< topics
       │                       │              │             ├─< candidate_moments ─< candidate_scores
       │                       │              │             ├─< processing_jobs
       │                       │              │             └─< clips ─┬─< clip_versions
       │                       │              │                       ├─< clip_edit_settings
       │                       │              │                       ├─< captions
       │                       │              │                       ├─< quality_reports
       │                       │              │                       ├─< repair_attempts
       │                       │              │                       └─< publishing_jobs >─ publishing_records
       │                       │              ├─< youtube_channels ─< oauth_credentials
       │                       │              ├─< analytics_snapshots
       │                       │              ├─< notifications
       │                       │              ├─< usage_records
       │                       │              └─< audit_logs
```

### 3.2 Invariants (enforced in DB where possible, not just in app code)

1. **Workspace scoping.** Every domain row reachable from user input carries `workspace_id`
   (directly or via a non-null FK chain). Authorization is resolved *once* in a dependency
   (`require_workspace_role`), then every query is workspace-filtered. No row is ever fetched by
   bare id from a user-supplied parameter.
2. **Timestamps are half-open intervals** `[start_ms, end_ms)` in **integer milliseconds**,
   never floats. Every interval must satisfy `0 ≤ start < end ≤ source.duration_ms` and
   `end - start ≥ 300ms`. Validated on write by a DB `CHECK` constraint *and* by Pydantic.
3. **Clip ↔ source linkage.** `clips.source_video_id` + `clips.candidate_moment_id` must belong to
   the same project; enforced by composite FK `(project_id, source_video_id)`.
4. **Immutability.** `clip_versions` rows are append-only (`rendered_at` set once, no UPDATE
   grants at the DB role level in prod). Editing settings creates a new version; the previous
   version file is retained (soft-deleted) so nothing is destroyed. `audit_logs` and
   `analytics_snapshots` are append-only.
5. **One live publish per clip per channel.** Partial unique index
   `UNIQUE (clip_id, channel_id) WHERE state IN ('queued','uploading','processing','published')`
   — this is the primary defence against duplicate posts, reinforced by an idempotency key on the
   upload job derived from `(clip_version_id, channel_id)`.
6. **Soft deletion** via `deleted_at` on user-facing aggregates (projects, clips, source_videos);
   storage objects get a lifecycle policy, and deletion of a `source_video` is refused while
   non-terminal jobs reference it.
7. **Secrets never plaintext.** OAuth refresh/access tokens and any provider keys are encrypted
   with `AES-GCM` (`cryptography`), key from `SECRET_ENCRYPTION_KEY` (32 bytes, base64), stored as
   `bytearray` columns with a `key_version` column for rotation. Passwords use **Argon2id**.
8. **Money/cost** is stored as `NUMERIC(12,6)` in USD micro-units-friendly form, never float.

### 3.3 Processing state machine

Project-level (`projects.status`) and job-level (`processing_jobs.status`) are separate: a project
status is a *projection* of its stage rows, whereas jobs are the unit of retry.

```
CREATED → SOURCE_VALIDATION → INGESTING → MEDIA_INSPECTION → AUDIO_EXTRACTION
   → TRANSCRIPTION → VISUAL_ANALYSIS → SEMANTIC_ANALYSIS → MOMENT_DETECTION
   → CLIP_RANKING → CLIP_GENERATION → VIDEO_RENDERING → CAPTION_RENDERING
   → AUDIO_ENHANCEMENT → QUALITY_CHECK ⇄ AUTO_REPAIR (max 2 attempts)
   → AWAITING_REVIEW → APPROVED → PUBLISHING → SCHEDULED → PUBLISHED → COMPLETED
```

Terminal-any **FAILED** / **CANCELLED**; every transition writes `project_stage_events`
(`from_status`, `to_status`, `task`, `progress`, `error`, `at`). Transition legality is a single
declarative table (`STAGE_GRAPH`) — one source of truth used by API validation, the orchestrator and
tests. Illegal transitions raise `409 invalid_transition`, never silently coerce.

**Resumability:** each stage persists its output artifact (transcript, scenes, analysis, moments,
clip files) and a `stage_checkpoint` row. On failure the orchestrator resumes from the first
non-`SUCCEEDED` checkpoint stage; idempotency is keyed by
`(project_id, stage, input_fingerprint)` so a retried task that already produced its artifact
short-circuits instead of redoing paid work.

---

## 4. AI intelligence pipeline

### 4.1 Stages and cost control

| Stage | Input | Technique | Cost shape |
|---|---|---|---|
| Probe | file | `ffprobe` JSON, SHA-256 checksum | free |
| Audio extract | file | FFmpeg → 16 kHz mono WAV (+ optional 48 kHz for master) | free |
| ASR | audio | `faster-whisper` (word timestamps, VAD); provider STT as fallback | local GPU/CPU |
| Diarization | audio | `pyannote.audio` **if** HF token + licence accepted, else heuristic speaker turns from pause lengths + ASR | optional |
| Scene detect | video | PySceneDetect (content detector) + FFmpeg `blackdetect`/`freezedetect` | free |
| Frame sampling | video | Composite "salience" sampler (§4.2) | free |
| Vision | ~20–80 frames/video | configurable multimodal model, batched, cached by frame hash | bounded, per-frame |
| Semantic analysis | transcript chunks | map-reduce over time-windowed chunks with overlap; strict JSON schema | bounded per chunk |
| Moment detection | analysis + transcript | LLM over *chunk-wise candidates*, then deterministic assembly | moderate |
| Scoring | candidates | deterministic weighted model (+ optional embedding similarity) | free |

Rules: an entire video is **never** re-sent to a model; every LLM call is chunked, cached in Redis
by `sha256(model+prompt+input)` with a TTL, and capped by a per-project budget guard that fails the
job *early* with `BUDGET_EXCEEDED` rather than mid-render.

### 4.2 Frame salience sampling (avoid expensive per-frame vision)

Score every 1 s candidate frame: `w1·scene_boundary + w2·speaker_change + w3·speech_energy +
w4·motion_magnitude + w5·candidate_boundary_proximity + w6·on_screen_text_probability`, then
non-max-suppress to a budget (default 48 frames). Weights live in `config/analysis.yaml` so they can
be tuned without code changes. This is the mechanism that keeps vision cost O(1) in video length.

### 4.3 Structured output contract

```jsonc
{
  "schema_version": 1,
  "video_summary": "string, 1–3 sentences",
  "language": "BCP-47",
  "topics": [{ "label": "...", "start_ms": 0, "end_ms": 0, "confidence": 0.0 }],
  "speakers": [{ "label": "...", "first_seen_ms": 0, "segments": [[0,0]] }],
  "scenes": [{ "index": 0, "start_ms": 0, "end_ms": 0, "description": "...", "has_on_screen_text": true }],
  "important_moments": [{ "start_ms": 0, "end_ms": 0, "kind": "hook|fact|story|...", "why": "..." }],
  "key_statements": [{ "start_ms": 0, "end_ms": 0, "quote": "...", "speaker": "..." }],
  "candidate_segments": [{ "start_ms": 0, "end_ms": 0, "rationale": "...", "categories": ["..."] }],
  "analysis_confidence": 0.0
}
```

Validation (`pydantic` + custom validators): every timestamp integer, `start < end`, within
`[0, duration_ms]`; unknown enum members rejected; `confidence ∈ [0,1]`; unknown keys forbidden.
**Repair loop:** one schema-repair retry with the validation error text echoed back; a second
failure → stage `FAILED` with a human-readable diff, never a partial write. Timestamps are
additionally re-anchored to real ASR word boundaries (snap to nearest word onset/offset within
±400 ms) so the LLM can never invent a boundary that cuts a word in half.

### 4.4 Moment detection + duration optimisation

Two-phase: (a) the model proposes *ideas* (start hint, end hint, category, hook sentence, needed
context, payoff, natural ending, standalone-ok flag); (b) deterministic code expands boundaries
outward to the nearest sentence boundary from the transcript, enforces min/max duration, and
computes `required_duration_ms`. If the complete idea is 38 s, we render 38 s — the 15/30/45 presets
are *preferences*, not clamps, and a requested preset that would truncate the payoff is rejected with
a recorded reason.

### 4.5 Scoring (configurable, decomposed, non-misleading)

Default weights (in `config/scoring.yaml`): hook 15, content value 15, narrative completeness 15,
standalone context 15, audience relevance 10, emotional engagement 10, visual quality 5, audio
quality 5, ending strength 5, clarity 5.

Stored as **four separate 0–100 sub-scores** — `content_potential`, `technical_quality`,
`context_completeness`, `audience_relevance` — plus `overall` and the per-criterion vector used to
compute it. The UI always shows the decomposition; `overall` is presentational only and is never the
sole basis for auto-approval (auto-approve additionally requires QA pass and
`context_completeness ≥ threshold`).

### 4.6 Diversity / duplicate suppression

1. Hard reject overlapping intervals with IoU > 0.15 unless the variant declares an intentional
   different-duration/different-template strategy (recorded, and capped at one).
2. Semantic dedupe: cosine similarity on sentence embeddings of the transcript span; > 0.88 →
   keep highest `overall`, mark the other `suppressed_duplicate` (retained for audit, not deleted).
3. Greedy + topic quota: iterative selection maximising marginal score penalised by topic/speaker
   repetition, so a rich source doesn't yield eight clips from one anecdote.

### 4.7 Virality language policy

The API field is `content_potential_score` and every surface labels it
"estimated content strength — not a prediction of views". Enforced by a copy lint test.

---

## 5. Production (video) engine

**Rendering is declarative.** A `RenderPlan` (JSON, versioned, stored in `clip_versions.render_plan`)
is compiled to an FFmpeg filtergraph; the renderer never mutates source media.
`trim → crop/scale (9:16) → overlays → subtitles → audio chain → encode`.

* **Vertical reframing.** Per-frame subject track → smoothed crop keyframes.
  * Landscape single speaker: subject-aware crop to 9:16 with **crop smoothing** (EMA over track +
    minimum-dwell constraint, so the camera never jumps between adjacent people).
  * Two speakers: choose among active-speaker crop, split-screen (two 9:16 panels stacked), and
    "landscape-in-vertical" letterbox-on-branded-canvas. The chosen layout is recorded in the plan.
  * Reframe transitions use a 400 ms eased interpolation; hard cuts only at scene boundaries.
  * Fallback chain: face track → motion centroid → centred crop. Never stretch; scale + pad instead.
  * Target 1080×1920 when `min(source_height, source_width·16/9) ≥ 810`; otherwise emit at the
    largest exact 9:16 multiple of the source and flag `below_target_resolution` in QA.
* **Captions.** ASS subtitles generated from word timings (k=2–4 words/card, grouped at
  punctuation), safe-area margins from template, keyword emphasis via colour/scale. Timing
  validator implements the spec's flags (too early/late, on-screen too long, too many words,
  mobile-safe overflow, occlusion of detected face boxes). Subtitles are stored as their own
  artifact → re-styling never re-runs AI.
* **Audio.** Normalise to −14 LUFS (EBU R128, two-pass `loudnorm`), optional `afftdn` denoise,
  optional music bed with ducking under speech (`sidechaincompress`), true-peak limiter at −1 dBTP.
* **Templates** (8): podcast, interview, educational, motivational, comedy, business, tutorial,
  news. Templates are data (`config/templates/*.yaml`): caption style, zoom policy, silence-trim
  aggressiveness, overlay/branding slots, music policy. No template may alter pitch, add
  misleading cuts, or insert words the speaker didn't say.
* **Silence trimming / jump cuts** only at VAD-verified pauses and only when the removed span
  leaves the sentence grammatical (never inside a word; ASR word gaps are the gate).

**QA (§6) is a separate service** that re-inspects the *file*, not the plan — it re-probes the
output, decodes frames, checks hashes, and re-reads the ASR text of the clip against the burned
captions. It has no ability to "trust" the renderer.

---

## 6. Quality assurance and repair

**Technical:** container/codec, exact-ish duration (±150 ms), 9:16 or declared exception, resolution
floor, both streams present, frame-decode integrity (no decode errors), black-frame ratio
< 1.5 % per 10 s window, freeze detection (`freezedetect` n=-60 dB, d≥1 s) excluding intentional
still segments, audio clipping ratio, A/V sync via `astats`/silence-alignment probe, caption presence
and legibility (render a frame, measure text contrast), and file existence/hash.

**Editorial:** mandatory checks are deterministic where possible: opening sentence completeness
(starts on a word onset with a sentence-initial token), ending completeness (ends on terminal
punctuation or a detected pause), no leading/trailing dead air > 400 ms, caption-vs-ASR word overlap
≥ 98 % per segment, crop contains a face for ≥ 80 % of runtime when faces exist. A model-assisted
review adds: standalone intelligibility, context need, misrepresentation risk, and "worth publishing".
Model-assisted verdicts are advisory; only deterministic failures are *blocking*.

**Duplicates:** timestamp IoU, perceptual hash (dHash/pHash, Hamming ≤ 6), Chromaprint audio
fingerprint where available, transcript cosine similarity.

**Safety indicators** (not legal verdicts): provider moderation on transcript + sampled frames,
flagged as `risk: {category, severity, evidence}`. Severe flags block auto-publish and route to review.

**Repair engine:** failed check → classified cause → allowlisted correction
(`recrop`, `regenerate_captions(segment)`, `shift_start`, `extend_end`, `loudnorm`,
`swap_candidate`, `fallback_layout`) → re-render **only the affected component** (partial re-render;
captions are re-muxed, not re-encoded from scratch where possible) → QA again. Max 2 attempts per
check, max 3 versions; both original and repaired versions retained with `repair_attempts` history;
unresolved → `AWAITING_REVIEW` with the failing check named. Nothing failing a mandatory check is
ever publishable (`clips.publishable` is a generated column derived from the latest passing report).

---

## 7. Publishing (YouTube)

* **OAuth 2.0** authorization-code flow, `access_type=offline`, `prompt=consent` to obtain a refresh
  token; `state` is a signed, single-use nonce bound to the user + workspace (CSRF); PKCE used
  even though the app is confidential. Tokens AES-GCM encrypted (§3.2.7), `scope`s stored so we can
  tell the user exactly which permissions are missing. Upload requires
  `https://www.googleapis.com/auth/youtube.upload`; analytics requires
  `youtube.readonly` + `yt-analytics.readonly`. Disconnect revokes at Google
  (`/revoke`) then deletes locally.
* **Resumable upload** (`videos.insert` with `uploadType=resumable`): chunked, retried with
  exponential backoff on 5xx/`rateLimitExceeded`, resuming from the reported byte offset; never
  restarted from zero on a transient failure.
* **Duplicate prevention:** idempotency key `(clip_version_id, channel_id)` → `publishing_jobs`
  unique index; the YouTube `video_id` is persisted *before* any retry decision; if a job dies after
  the upload succeeded but before commit, the recovery path *queries the channel's recent uploads* for
  the recorded title/correlation tag (`autoclip-id:<job_id>` appended to description, then scrubbed)
  instead of re-uploading.
* **State machine:** `queued → uploading → processing → published | failed | cancelled`, plus
  `scheduled`. "Published" requires the API's returned `videos` resource containing an id **and** a
  successful follow-up `videos.list(part=status,processingDetails)`; until then it's `processing`.
* **Scheduling:** persistent `publishing_jobs.scheduled_at` (UTC) + Celery Beat tick that claims due
  rows with `SELECT … FOR UPDATE SKIP LOCKED`; requires `privacyStatus=private` +
  `publishAt` (ISO-8601, UTC) per API semantics. **[VERIFY]** thumbnail eligibility per upload path
  and channel verification requirements (custom thumbnails are not universally available) — must be
  confirmed against live API docs/response before the UI offers thumbnail upload.
* **Quota:** a Redis token-bucket per Google project mirroring the daily 10 000-unit quota, with
  upload costed at the documented unit price; when the remaining budget is insufficient the job is
  re-queued to the next reset with an explicit user-visible reason (no silent failure).

---

## 8. Orchestration, queues, real-time

| Queue | Tasks | Concurrency default | Notes |
|---|---|---|---|
| `media` | probe, audio extract, scene detect, thumbnails | 2/host | CPU + I/O |
| `transcribe` | ASR, diarization | 1/host | RAM/GPU heavy |
| `analyze` | chunk summarisation, vision, moments, scoring | 4 | network-bound |
| `render` | FFmpeg encode, caption burn, repair re-render | 1–2/host | memory ceiling |
| `qa` | technical/editorial/duplicate checks | 2 | CPU |
| `publish` | upload, schedule tick | 1 | quota safety |
| `analytics` | snapshot sync | 2 | rate-limit aware |

Every task: explicit `time_limit`/`soft_time_limit`, `acks_late=True`, `reject_on_worker_lost=True`,
`max_retries` with jittered exponential backoff, `Idempotency-Key` semantics via
`processing_jobs.stage + input_fingerprint`, structured JSON logs with `trace_id`/`job_id`, and a
row in `processing_jobs` written **before** the task is enqueued (so a lost broker message is
discoverable). Dead-letter: after final retry the job lands `DEAD_LETTER` and surfaces in
Processing Queue UI with a manual retry button. Stuck-job sweeper (Beat, every 60 s) fails jobs whose
`heartbeat_at` is older than `2×time_limit` and marks the worker unhealthy.

**Real-time:** SSE (not WebSocket) — one-way, proxy-friendly, auto-reconnecting. `GET
/api/v1/projects/{id}/events` streams `event: stage|clip|job|done` frames with `Last-Event-ID`
replay from a Redis stream (`XADD`/`XRANGE`), so a reconnect doesn't lose transitions. Progress
percentages are only emitted when a stage has a measurable denominator (frames rendered, bytes
uploaded, chunks analysed); otherwise the event carries `progress: null` and the UI shows an
indeterminate bar. ETA is shown only after ≥ 2 completed comparable stages.

---

## 9. Cost, resource and failure isolation

`usage_records` captures per-call provider usage (tokens, audio seconds, frames, render seconds),
priced from `config/pricing.yaml`; rollups power Billing/Usage and per-project cap enforcement
(`projects.budget_usd`) with warn at 80 % and hard stop at 100 % (`BUDGET_EXCEEDED`). Model routing:
small/cheap model for classification and metadata, stronger model for semantic analysis and moment
rationale, vision only on salience-selected frames, embeddings for dedupe. Everything cacheable is
cached. Rendering: temp files per job under `TMPDIR/<job_id>`, always cleaned in `finally`; disk
guard refuses new render tasks when free space < `MIN_FREE_DISK_GB`; per-render wall-clock timeout
scaled by clip duration; optional `-preset`/CRF downgrade for preview renders (proxy previews
generated at 540p, final at full quality on demand).

Failure isolation is a product requirement: publish failure never invalidates clips; caption failure
never invalidates transcription; render failure never invalidates analysis. This falls out of the
checkpoint model (§3.3) and is asserted by an integration test that force-fails each stage in turn.

---

## 10. API surface

Versioned `/api/v1`, OpenAPI 3.1 generated by FastAPI and committed to `docs/openapi.json` as a
build artifact check. Conventions:

* Errors: RFC 9457 `application/problem+json` with `type`, `title`, `status`, `detail`,
  `code` (stable machine string), `trace_id`, and `fields` for validation errors.
* Pagination: cursor-based (`?limit=&cursor=`), `{items, next_cursor, total_estimate}`; `limit` ≤ 100.
* AuthN: session cookie for the browser; `Authorization: Bearer` service tokens for tests/CI.
* AuthZ: `require_workspace_role(min_role)` per route; roles `owner|admin|editor|viewer`.
* Idempotency: mutating POSTs accept `Idempotency-Key`, echoed and enforced for 24 h.
* Every long-running route returns `202 + job_id` and links to its SSE stream.
* Rate limits per IP and per workspace (Redis sliding window); upload endpoints guarded separately.

Endpoint groups are exactly those in the spec (§35) plus additions the implementation requires —
currently anticipated: `GET /api/v1/projects/{id}/stages` (state-machine history),
`GET /api/v1/projects/{id}/events` (SSE), `POST /api/v1/clips/{id}/versions/{vid}/restore`,
`GET /api/v1/clips/{id}/download` (signed URL), `POST /api/v1/webhooks/youtube` (verification),
`GET /api/v1/usage/summary`, `GET /api/v1/notifications`. Refinements are documented in
`docs/API.md` with a changelog.

---

## 11. Security

Argon2id passwords (per-user salt, pepper from env); session cookies `HttpOnly; Secure;
SameSite=Lax`, opaque server-side session ids in Redis with rotation on privilege change; CSRF via
double-submit token on cookie-authenticated mutating routes; strict CSP (no `unsafe-inline` for
scripts; nonce-based), HSTS, `X-Content-Type-Options`, `Referrer-Policy`, frame-ancestors `none`.
Uploads: extension + MIME allowlist, magic-byte sniff, container probe before accepting, size cap,
re-encode-on-ingest option, stored outside webroot with generated names, served only via
short-lived (≤ 10 min) signed URLs. Secrets from env/secret manager only; `.env.example` documents
every variable and the app **fails fast** at boot if a required one is missing (no silent defaults
for secrets). Audit log for auth events, channel connect/disconnect, approval, publish, delete,
budget changes, rights declarations. Rate limits and per-workspace storage quotas. Dependency and
container scanning in CI. Cross-workspace access attempts return `404` (not `403`) to avoid
existence leaks.

**Threat model highlights:** (a) SSRF via YouTube URL/oEmbed fetch — allowlist hostnames, no
redirects, timeout, no internal IPs; (b) file-path traversal via project names — never use user
strings in paths (UUIDs only); (c) FFmpeg filter injection via captions/user text — all text is
escaped through a dedicated `ASS`/`drawtext` escaper and passed via `subtitles`/`textfile`, never
string-interpolated into a filtergraph; (d) OAuth token theft — encryption at rest + narrow scopes +
revocation; (e) prompt injection from transcript content into the LLM — system prompt isolates data
in delimited blocks, model output is schema-validated and never executed; (f) resource exhaustion —
queue concurrency caps, per-user concurrent project limit, render timeouts.

---

## 12. Testing

* **Unit** (fast, no I/O): URL/ID parsing, interval validation, stage graph legality, scoring
  maths + weight config loading, duration optimisation, duplicate detection, caption timing
  validator, metadata validation, prompt/schema validators, SSRF allowlist, FFmpeg filter escaping.
* **Integration** (docker services): DB migrations up/down, repository workspace isolation, storage
  signing, Celery eager-mode task contracts, real FFmpeg renders on generated fixtures, faster-whisper
  on a short synthetic clip, QA on deliberately-broken files (black frames, no audio, wrong ratio,
  frozen frames), YouTube client against a recorded-HTTP mock.
* **E2E** (Playwright): register → create project → upload fixture → analyse → generate → edit →
  QA → approve → (mocked) publish → history.
* **Fixtures** are generated deterministically by `ops/scripts/make_fixtures.sh` (FFmpeg
  `testsrc`/`sine`, plus a tiny real speech clip produced locally) so tests never depend on
  downloads. External APIs are mocked **only** in tests, via `respx`/recorded cassettes; production
  code has no mock path (a test asserts no `if testing` branches exist in `app/`).
* Coverage gates: 85 % on `app/intelligence`, `app/production`, `app/qa`, `app/publishing`;
  70 % overall. CI runs ruff + mypy (strict on `app/`) + pytest + frontend typecheck/lint/vitest.

---

## 13. Observability & operations

Structured JSON logs (structlog) with `trace_id` propagated from request → task → subtask;
Prometheus `/metrics` (queue depth, job durations, stage failure rates, render seconds, worker
heartbeats, DB pool, disk free, quota remaining); health endpoints `livez`/`readyz`/`/health`
(dependency-aware). Sentry-optional. Backups: nightly `pg_dump` + WAL archiving guidance, object
store versioning; documented restore drill. Graceful shutdown: workers finish current job up to
`soft_time_limit` then ack/nack-safe exit; API drains. Runbook covers stuck queue, quota exhaustion,
token revocation, disk-full, and disaster restore.

---

## 14. Phase plan and what "done" means

| Phase | Deliverable | Exit criteria (verified, not claimed) |
|---|---|---|
| **0 (this)** | this doc, diagrams, schema, API spec, env, Docker, logging + testing strategy, scaffolding, `DEVELOPMENT_STATUS.md` | docs reviewed; compose file validates; schema DDL parses; no app logic yet |
| 1 | auth, workspaces, projects, uploads, source validation, dashboard skeleton | register/login/logout/me tested; upload rejects bad MIME/corrupt file; project CRUD workspace-isolated |
| 2 | probe, audio, ASR, scenes, semantic analysis | real transcript produced on fixture; schema validation + repair loop tested; checkpoints resume |
| 3 | candidates, scoring, diversity, duration optimisation | deterministic tests on crafted transcripts; overlap/dedupe assertions |
| 4 | render, reframe, captions, templates, versioning | real FFmpeg produces valid 1080×1920 with synced ASS captions; caption validator green |
| 5 | QA + repair | broken fixtures correctly fail specific checks; repair improves and is bounded |
| 6 | OAuth, channels, metadata, resumable upload, scheduling | OAuth e2e against real Google project (creds provided by operator); duplicate-upload test passes |
| 7 | analytics sync + insights | insights only emitted above sample-size threshold; uncertainty stated |
| 8 | hardening, load test, monitoring, docs | soak test at N concurrent projects; restore drill; security checklist signed off |

Phase 6 cannot be *demonstrated* without operator-supplied Google OAuth client credentials — the code
and mocked tests will exist, and the app will report the missing configuration explicitly rather than
pretending to publish. Same rule for any paid AI provider: an unconfigured provider yields a clear
`PROVIDER_NOT_CONFIGURED` error with the exact env var to set, never a fake success.

---

## 15. Risks and open questions

1. **[VERIFY] YouTube API specifics** — upload quota cost, thumbnail eligibility by channel status,
   `publishAt` semantics, and Analytics dimension availability must be confirmed against live docs +
   a real response before Phase 6 ships. Nothing in §7 is assumed.
2. **Diarization licensing** — `pyannote` weights require accepting HF terms; it is optional and the
   system degrades to heuristic speaker turns.
3. **ASR quality on Hinglish** — routed to a code-switch-tolerant model config; if quality is
   insufficient the caption engine must not "correct" meaning (English-only correction allowed,
   logged, original retained).
4. **Render determinism** — FFmpeg encoder output is not bit-reproducible across hosts; QA compares
   properties + perceptual hashes, not byte equality.
5. **Cost blowups** — mitigated by chunked calls, cache, budget guard; still needs per-provider spend
   alerts.
6. **Scope** — this is a large product; the phase gates above exist so each increment is verifiable
   rather than a large unverified drop.
