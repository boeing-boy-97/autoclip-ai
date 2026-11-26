# Logging, tracing and error strategy

## 1. Goals

1. Any failure can be reconstructed from logs alone, without access to the user's session.
2**Every** paid or slow operation is attributable to a workspace, project, job and cost.
3. No secret, token, transcript of a private video, or media URL leaks into logs.
4. A log line is machine-parseable (JSON) in every environment except local dev console mode.

## 2. Record shape

`structlog` → JSON in `development`/`production` (console renderer when `LOG_FORMAT=console`).
Every record carries a **bound context**:

| Field | Source | Notes |
|---|---|---|
| `ts` | ISO-8601 UTC ms | always UTC, never local |
| `level`, `event` | call site | `event` is a stable dotted key, e.g. `stage.succeeded` |
| `trace_id` | request middleware → Celery task header → child tasks | 32-hex; propagated across the queue boundary |
| `span_id` | optional | present when tracing enabled |
| `workspace_id`, `user_id` | auth dependency | omit for unauthenticated routes |
| `project_id`, `clip_id` | route/task binding | |
| `job_id`, `task_name`, `queue`, `attempt` | worker binding | |
| `stage`, `duration_ms` | stage transitions | |
| `provider`, `model`, `cost_usd`, `tokens`/`audio_seconds` | provider wrapper | mirrors `usage_records` |
| `error_code`, `error_class` | error handler | stable machine code first |
| `http.method`, `http.path`, `http.status`, `http.duration_ms` | access log | path is the **route template**, not the raw URL (avoids leaking ids) |

Stable event names (exhaustive list kept in `app/core/log_events.py` so tests can assert on them):
`http.request`, `auth.login`, `auth.login_failed`, `auth.logout`, `stage.started`,
`stage.succeeded`, `stage.failed`, `stage.skipped_cached`, `job.enqueued`, `job.retry`,
`job.dead_letter`, `provider.call`, `provider.error`, `render.started`, `render.finished`,
`qa.check_failed`, `repair.applied`, `publish.started`, `publish.succeeded`, `publish.failed`,
`quota.exhausted`, `budget.warning`, `budget.exceeded`, `storage.low_disk`, `worker.heartbeat_missed`.

## 3. Redaction

A `RedactionFilter` runs on every record and on structured `detail` payloads:

* keys matching `(?i)(password|secret|token|api_key|authorization|cookie|refresh_token|access_token|client_secret|pepper)` → `"***"`.
* Signed URLs → scheme+host+path with query stripped (`?X-Amz-Signature=…` removed).
* Email addresses hashed in aggregate logs (`sha256(email)[:12]`), preserved in audit logs.
* Transcript text is **never** logged at INFO. At DEBUG it may appear truncated to 120 chars and
  only when `LOG_TRANSCRIPTS=true` (explicit opt-in, off by default, rejected in production).
* FFmpeg stderr is captured to the job's artifact (`render_log`) and only its tail (≤ 2 KB) is
  logged, because filtergraphs can contain user text.

## 4. Levels

| Level | Use |
|---|---|
| ERROR | operation failed and will not be retried automatically; needs a human |
| WARNING | degraded but recoverable (retry scheduled, fallback used, threshold crossed) |
| INFO | lifecycle events (stage succeeded, publish succeeded, channel connected) |
| DEBUG | per-step detail, off in production |

Third-party loggers (`urllib3`, `httpx`, `sqlalchemy.engine`, `googleapiclient`) are pinned to
WARNING unless `DB_ECHO`/debug is set. Celery's `celery.app.trace` stays at INFO.

## 5. Errors

All API errors are RFC 9457 `application/problem+json`:

```json
{
  "type": "https://docs.autoclip.ai/errors/clip_not_publishable",
  "title": "Clip is not publishable",
  "status": 409,
  "code": "clip_not_publishable",
  "detail": "Clip version 3 failed technical QA: black_frame_ratio 0.041 (limit 0.015).",
  "trace_id": "b1e4…",
  "fields": [{ "path": "clip_version_id", "message": "must reference a QA-passing version" }]
}
```

Rules:

* `code` is the contract; the UI switches on it. `detail` is for humans and may change.
* Internal exception text is mapped through an `ERROR_MAP`; unmapped exceptions become
  `internal_error` with the message **withheld** in production (trace_id lets support correlate).
* `4xx` logs at INFO/WARNING (validation at DEBUG for noisy fields), `5xx` at ERROR with stack.
* Client renderers: every screen implements `loading`, `empty`, `error`, and `success` states; a
  `code` with no specific handler gets a generic retry affordance plus the trace id.

## 6. Job-level correlation

`processing_jobs` is the durable index of work. A log record with `job_id` joins directly to the
row, which holds `status`, `attempts`, `error_code`, `trace_id`, `heartbeat_at`. The Processing Queue
UI shows the last N log lines for a job by reading its artifact — so support never needs shell access.

## 7. Metrics (Prometheus, `/metrics` on the API)

`http_requests_total{route,status}`, `http_request_duration_seconds` (histogram), `queue_depth{queue}`,
`jobs_completed_total{queue,stage,status}`, `job_duration_seconds{stage}`, `stage_failures_total{stage,error_code}`,
`render_seconds_total`, `provider_cost_usd_total{provider,model}`, `youtube_quota_units_remaining`,
`worker_heartbeat_age_seconds{worker}`, `db_pool_in_use`, `db_pool_overflow`, `disk_free_bytes`,
`storage_bytes{workspace}`. Labels are bounded (no ids) to keep cardinality sane.

## 8. Sampling

Access logs at 100 % (low volume, high diagnostic value). DEBUG traces sampled at 10 % via
`TRACE_SAMPLE_RATE`. Never sample ERROR/WARNING.
