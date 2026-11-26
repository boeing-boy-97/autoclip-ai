# AutoClipper AI — Diagrams

Rendered with Mermaid (GitHub, VS Code, and most Markdown viewers render these natively).

---

## 1. Container / deployment view

```mermaid
graph TB
  subgraph Client
    B[Browser<br/>Next.js SSR + BFF proxy]
  end

  subgraph Edge
    P[Reverse proxy / TLS<br/>single origin]
  end

  subgraph Web
    N[Next.js server<br/>standalone]
  end

  subgraph App
    A[FastAPI<br/>API v1 + SSE + OpenAPI]
  end

  subgraph Workers
    W1[worker-media<br/>probe · audio · scenes]
    W2[worker-ai<br/>ASR · vision · analysis · moments]
    W3[worker-render<br/>FFmpeg · captions]
    W4[worker-qa<br/>technical · editorial · dupes]
    W5[worker-publish<br/>YouTube upload]
    W6[worker-analytics<br/>YT analytics sync]
    BT[Celery Beat<br/>scheduler · sweeper]
  end

  subgraph Data
    PG[(PostgreSQL 17)]
    RD[(Redis 8<br/>broker · results · cache · streams · locks)]
    S3[(S3-compatible<br/>R2 / S3 / MinIO)]
  end

  subgraph External
    YT[YouTube Data API v3<br/>+ Analytics API]
    LLM[LLM / Vision provider]
    STT[Hosted STT · optional]
  end

  B --> P --> N --> A
  A --> PG
  A --> RD
  A --> S3
  A --> YT
  W1 & W2 & W3 & W4 & W5 & W6 & BT --> RD
  W1 & W2 & W3 & W4 & W5 & W6 --> PG
  W1 & W2 & W3 & W4 & W5 & W6 --> S3
  W2 --> LLM
  W2 --> STT
  W5 --> YT
  W6 --> YT
```

---

## 2. Project lifecycle (state machine)

```mermaid
stateDiagram-v2
  [*] --> CREATED
  CREATED --> SOURCE_VALIDATION
  SOURCE_VALIDATION --> INGESTING
  INGESTING --> MEDIA_INSPECTION
  MEDIA_INSPECTION --> AUDIO_EXTRACTION
  AUDIO_EXTRACTION --> TRANSCRIPTION
  TRANSCRIPTION --> VISUAL_ANALYSIS
  VISUAL_ANALYSIS --> SEMANTIC_ANALYSIS
  SEMANTIC_ANALYSIS --> MOMENT_DETECTION
  MOMENT_DETECTION --> CLIP_RANKING
  CLIP_RANKING --> CLIP_GENERATION
  CLIP_GENERATION --> VIDEO_RENDERING
  VIDEO_RENDERING --> CAPTION_RENDERING
  CAPTION_RENDERING --> AUDIO_ENHANCEMENT
  AUDIO_ENHANCEMENT --> QUALITY_CHECK
  QUALITY_CHECK --> AUTO_REPAIR: failed & repairable
  AUTO_REPAIR --> QUALITY_CHECK: attempts < 2
  QUALITY_CHECK --> AWAITING_REVIEW: needs human
  QUALITY_CHECK --> AWAITING_REVIEW: passed
  AWAITING_REVIEW --> APPROVED: human approves
  APPROVED --> PUBLISHING
  PUBLISHING --> SCHEDULED: publishAt set
  PUBLISHING --> PUBLISHED: immediate
  SCHEDULED --> PUBLISHED
  PUBLISHED --> COMPLETED
  AWAITING_REVIEW --> CLIP_GENERATION: reject / regenerate

  note right of AUTO_REPAIR
    Bounded: max 2 attempts per check,
    max 3 versions. Unresolved → manual review.
  end note

  ANY --> FAILED
  ANY --> CANCELLED
```

Illegal transitions are rejected with `409 invalid_transition` by a single declarative graph
(`STAGE_GRAPH`) shared by API, orchestrator and tests.

---

## 3. Intelligence pipeline (data + checkpoints)

```mermaid
flowchart LR
  SRC[(source_video<br/>sha256 · probe)] --> AUD[audio.wav<br/>16k mono]
  SRC --> SC[scenes + black/freeze]
  AUD --> ASR[transcript<br/>word timings]
  ASR --> SPK[speakers<br/>diarization or heuristic]
  SC --> FS[salience frame sampler]
  ASR --> FS
  FS --> VIS[vision captions<br/>per selected frame]
  ASR --> SEM[chunked semantic analysis<br/>schema-validated]
  SPK --> SEM
  VIS --> SEM
  SEM --> MOM[idea proposals<br/>+ deterministic boundary assembly]
  ASR --> MOM
  MOM --> SCORE[scoring: 4 sub-scores<br/>+ criterion vector]
  SCORE --> DIV[diversity / dedupe<br/>IoU + embeddings + topic quota]
  DIV --> CAND[(candidate_moments)]

  classDef ck fill:#fff4e6,stroke:#c98a3b;
  class AUD,ASR,SC,VIS,SEM,SCORE ck;
```

Every box is a checkpointed stage writing an artifact + `input_fingerprint`; a retry short-circuits
when the artifact already exists. Orange = paid/AI stages, so a failure downstream never re-pays them.

---

## 4. Render + QA loop

```mermaid
flowchart TB
  PLAN[RenderPlan JSON<br/>from clip_edit_settings] --> COMP[compile filtergraph]
  COMP --> RT[trim] --> CR[crop / scale 9:16<br/>smoothed keyframes]
  CR --> OV[overlays · branding · zoom]
  OV --> SUB[burn ASS captions]
  SUB --> AU[audio: loudnorm · denoise · duck]
  AU --> ENC[encode 1080x1920 H.264/AAC]
  ENC --> QA{Tech checks}
  QA -- pass --> EQ{Editorial checks}
  QA -- fail --> REP{Repairable?}
  EQ -- pass --> DUP{Duplicate checks}
  EQ -- fail --> REP
  DUP -- unique --> VER[(clip_version<br/>publishable=true)]
  DUP -- duplicate --> REP
  REP -- yes & attempts<2 --> PATCH[regenerate affected component only]
  PATCH --> ENC
  REP -- no --> MAN[(AWAITING_REVIEW<br/>failing check named)]

  style VER fill:#e8f5e9
  style MAN fill:#fff4e6
```

QA re-inspects the produced **file** and never trusts the RenderPlan. Only the latest passing report
makes a version publishable; failed versions are retained for comparison.

---

## 5. Publish path (duplicate-safe)

```mermaid
sequenceDiagram
  autonumber
  participant U as User
  participant API as FastAPI
  participant DB as Postgres
  participant Q as publish queue
  participant W as worker-publish
  participant G as Google APIs

  U->>API: POST /clips/{id}/publish (channel, mode, Idempotency-Key)
  API->>DB: guard: version.publishable? quota? existing live job?
  alt not publishable
    API-->>U: 409 clip_not_publishable + failing checks
  else already queued/publishing/published
    API-->>U: 200 existing job (idempotent)
  else ok
    API->>DB: INSERT publishing_jobs (unique clip+channel where live)
    API->>Q: enqueue publish_clip(job_id) with idempotency key
    API-->>U: 202 {job_id, stream_url}
  end

  W->>DB: claim job FOR UPDATE SKIP LOCKED
  W->>G: ensure fresh access token (refresh if needed)
  W->>G: POST videos.insert uploadType=resumable
  loop chunked, resumable
    W->>G: PUT chunk @ offset
    alt 5xx / rateLimitExceeded
      W->>G: resume from last committed offset (backoff+jitter)
    end
  end
  G-->>W: 200 video resource {id}
  W->>DB: persist youtube_video_id BEFORE any retry decision
  W->>G: videos.list(part=status,processingDetails)
  G-->>W: status
  W->>DB: state=published, url, record analytics seed
  W-->>API: SSE publish progress events
```

Failure after upload but before commit is recovered by looking up the recorded `youtube_video_id`
(or the `autoclip-id` correlation tag) — never by re-uploading.

---

## 6. Real-time event flow

```mermaid
sequenceDiagram
  participant B as Browser (EventSource)
  participant API as FastAPI /events
  participant R as Redis Stream
  participant W as Celery worker

  B->>API: GET /projects/{id}/events (Last-Event-ID?)
  API->>R: XRANGE replay from last id
  R-->>API: buffered transitions
  API-->>B: event: stage (replayed)
  W->>R: XADD stage/progress/clip events
  R-->>API: XREAD block
  API-->>B: event: stage|clip|job (streamed)
  Note over API,B: progress is null when no measurable denominator;<br/>ETA only after ≥2 comparable stages
```

---

## 7. Workspace isolation (authorization path)

```mermaid
flowchart LR
  REQ[Request] --> AUTH{session or bearer?}
  AUTH -- no --> E401[401]
  AUTH -- yes --> MEM{workspace member?}
  MEM -- no --> E404[404 — existence not leaked]
  MEM -- yes --> ROLE{role ≥ min_role?}
  ROLE -- no --> E403[403]
  ROLE -- yes --> SCOPE[repository call<br/>forced workspace_id filter]
  SCOPE --> RES[(row or 404)]
```

Every repository function takes `workspace_id` as a required keyword argument — there is no
"fetch by id alone" helper, which makes cross-workspace access a type error rather than a code review
miss.
