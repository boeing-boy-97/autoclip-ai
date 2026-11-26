# Security, privacy and compliance

> Scope: Phase 0 design. Each control names where it is enforced so it can be audited.
> Status legend: **[design]** specified here, **[phase N]** implemented in that phase.

## 1. Authentication

| Control | Decision | Where |
|---|---|---|
| Password hashing | Argon2id, m=64 MiB, t=3, p=4, per-user salt, server-side pepper from env | **[phase 1]** `core/security.py` |
| Password policy | ≥ 10 chars, breached-list check optional, no forced composition rules | **[phase 1]** |
| Sessions | Opaque 256-bit token in `HttpOnly; Secure; SameSite=Lax` cookie; only SHA-256 stored server-side; sliding expiry (`SESSION_TTL_HOURS`) with absolute cap 30 d | **[phase 1]** `sessions` table |
| Session rotation | New session id on login and on privilege change; old id revoked | **[phase 1]** |
| Logout | Revokes server-side row (client cookie deletion alone is not logout) | **[phase 1]** |
| Login throttling | 5 failures/15 min per (email, IP) then exponential lockout; generic error text (no user enumeration) | **[phase 1]** |
| MFA | TOTP field reserved in the login contract | **[phase 8]** |

## 2. Authorization

* Roles: `owner > admin > editor > viewer`. Every route declares `min_role`.
* Resolution happens once (`require_workspace_role`), then **every** repository function requires a
  `workspace_id` keyword argument. There is no "get by id alone" helper — the unsafe call doesn't
  type-check, so it can't be reintroduced by accident.
* Cross-workspace access returns **404**, not 403 (no existence oracle).
* Object-level checks are not enough: list endpoints filter in SQL, never in Python after fetching.
* Owner-only: delete workspace, manage members, change budget, connect/disconnect channels.
* `viewer` may read and download; `editor` may create/edit/approve; publishing requires `editor`
  or above **and** the project's publishing mode allows it.

## 3. Upload and media safety

1. Size limit from `MAX_UPLOAD_MB`, enforced by streaming counter (not `Content-Length`, which lies).
2. Extension allowlist **and** declared MIME **and** magic-byte sniff **and** `ffprobe` must agree.
   Mismatch → `415 unsupported_media` with the specific reason.
3. `ffprobe` runs with `-hide_banner -v error`, a wall-clock timeout, and `-protocol_whitelist file`
   to prevent network-protocol SSRF from a crafted container.
4. Corrupt/truncated files rejected at ingest (probe fails or duration inconsistent).
5. SHA-256 computed while streaming; stored for dedupe and audit.
6. Stored under a generated key (`ws/<uuid>/sources/<uuid>.<ext>`); original filename kept only as
   a DB column. Never used to build a path.
7. Files are never served by the API process; only short-lived signed URLs (≤ 600 s) from object
   storage, with `Content-Disposition: attachment` for downloads.
8. Optional re-encode-on-ingest to normalise hostile containers.

## 4. SSRF and remote fetch

Applies to the YouTube-URL path and any metadata (oEmbed) lookup:

* Hostname allowlist: `youtube.com`, `www.youtube.com`, `m.youtube.com`, `youtu.be`, `music.youtube.com`.
* Reject non-HTTPS, userinfo in URL, unusual ports, IP literals, and `localhost`/`.internal`.
* Resolve DNS and reject private/link-local/loopback/metadata ranges (169.254.169.254).
* `max_redirects=0`, timeout 5 s, response size cap 512 KB.
* Video id extracted by regex before any request; only the extracted id is used downstream.

## 5. Content authorization and copyright

* A `rights_declaration` (`basis` + free-text `statement`) is **required** before any source is
  ingested, recorded immutably in `audit_logs` with actor, IP and timestamp.
* A YouTube URL is **not** a media grant. Auto-ingestion of a YouTube URL is only permitted when the
  URL's channel is OAuth-connected to the workspace **and** the video is owned by that channel
  (`owned_channel` basis is then verifiable). Otherwise the user is directed to upload the original.
* **No** downloader, no access-control circumvention, no `yt-dlp`-style indirect retrieval for
  third-party content. This is a product constraint, not a TODO.
* QA "copyright" and "reused content" signals are **risk indicators**. The UI wording is fixed:
  *"Risk indicator — not a legal determination. Adding captions, cropping, music or transitions does
  not change ownership or guarantee platform eligibility."* A copy-lint test enforces this string.
* A report/flag path exists for users to flag content (`POST /api/v1/clips/{id}/report`) and every
  flag is audited.
* YouTube API Services Terms are respected: no scraping, no circumvention, data minimisation, and
  the required data-deletion/revocation handling on disconnect.

## 6. Secrets at rest

| Secret | Storage | Rotation |
|---|---|---|
| OAuth access/refresh tokens | AES-256-GCM ciphertext in `bytea` + `key_version` | re-encrypt on key rotation; revoke + re-consent on suspected compromise |
| `SECRET_KEY` / `SECRET_ENCRYPTION_KEY` / `PASSWORD_PEPPER` | env/secret manager only; boot fails if missing or placeholder-in-prod | documented rotation runbook |
| Provider API keys | env only; never persisted, never logged | operator |
| Signed URLs | derived, expire ≤ 600 s | n/a |

`.env` is gitignored; `.env.example` contains no real values; a CI check greps for high-entropy
strings and known key prefixes (`sk-`, `ya29.`, `AIza`) in tracked files.

## 7. Web hardening

`Content-Security-Policy` (nonce-based scripts, `frame-ancestors 'none'`), `Strict-Transport-Security`,
`X-Content-Type-Options: nosniff`, `Referrer-Policy: strict-origin-when-cross-origin`,
`Permissions-Policy` denying camera/mic/geolocation, no `X-Powered-By`.
CSRF: double-submit token for cookie-authenticated mutating requests, verified server-side;
`SameSite=Lax` as defence in depth. CORS: exact-origin allowlist, credentials on, no wildcards.
Rate limits per IP and per workspace (Redis sliding window) on auth, upload, render and publish.

## 8. Injection and FFmpeg specifics

The highest-risk surface in a video product is the filtergraph.

* User text (captions, overlays, titles) is **never** interpolated into a filter string. Subtitles go
  through a generated `.ass` file referenced by the `subtitles` filter (`filename` escaped); overlay
  text uses `textfile=` with a written temp file. Escaping helpers are unit-tested against payloads
  containing `'`, `\`, `:`, `;`, `[`, `]`, `,`, newlines, `%{}`.
* Input paths are generated UUIDs; never user-controlled.
* `-nostdin`, explicit `-t` timeout, and no `-f concat` with user-supplied lists.
* Prompt injection: transcript content is placed in a delimited data block and the system prompt
  states it is data, never instructions. Model output is schema-validated and never executed, never
  used to construct a filtergraph directly (it produces numbers/enums only).

## 9. Data protection and retention

* Private media is private by default; every object key is workspace-scoped.
* Retention: derived assets (frames, proxies, QA frames) expire by `STORAGE_LIFECYCLE_DAYS_DERIVED`
  (default 30 d). Source video and published outputs follow workspace policy; deleting a workspace
  hard-deletes objects after a 30-day grace period.
* PII: email, IP, user agent, and OAuth identity. Export and delete endpoints are planned for
  Phase 8 (`GET /api/v1/account/export`, `DELETE /api/v1/account`) — documented, not silently absent.
* Transcripts are derived from user content and treated as confidential; never logged at INFO.

## 10. Auditing

`audit_logs` records: register/login/logout (success and failure), channel connect/disconnect, token
refresh failure, project create/delete, rights declaration, clip approve/reject, publish, cancel,
budget change, member changes, download of a source. Append-only (no UPDATE/DELETE grants in prod).

## 11. Known limitations (stated, not hidden)

1. MFA, account export/delete, and full RBAC UI arrive in Phase 8.
2. Safety/copyright detection is best-effort and advisory; it cannot guarantee platform approval.
3. Diarization accuracy depends on optionally-licensed models and may fall back to heuristics.
4. Local filesystem storage is a development convenience only; production must use S3-compatible
   storage with versioning and encryption at rest, or the app refuses to start with
   `ENVIRONMENT=production` and `STORAGE_BACKEND=local`.
