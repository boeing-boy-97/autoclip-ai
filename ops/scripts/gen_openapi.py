#!/usr/bin/env python3
"""Generate docs/openapi.json — the Phase 0 API contract.

In Phase 1 the FastAPI app becomes the source of truth and this generator is replaced by an
export step (`python -m app.scripts.export_openapi`). Until then this file is authoritative and
is validated in CI, so the frontend can be built against a real contract instead of guesses.

Run:  python3 ops/scripts/gen_openapi.py
"""
from __future__ import annotations

import json
import pathlib
import re as _re

OUT = pathlib.Path(__file__).resolve().parents[2] / "docs" / "openapi.json"

PROBLEM = {
    "type": "object",
    "required": ["type", "title", "status", "code", "trace_id"],
    "properties": {
        "type": {"type": "string", "example": "https://docs.autoclip.ai/errors/invalid_transition"},
        "title": {"type": "string", "example": "Invalid project state transition"},
        "status": {"type": "integer", "example": 409},
        "code": {"type": "string", "example": "invalid_transition"},
        "detail": {"type": "string"},
        "trace_id": {"type": "string"},
        "fields": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "message": {"type": "string"},
                    "code": {"type": "string"},
                },
            },
        },
    },
}

ERROR_CODES = [
    "invalid_credentials", "unauthenticated", "forbidden", "not_found",
    "validation_error", "invalid_transition", "rate_limited", "conflict",
    "clip_not_publishable", "publish_duplicate", "youtube_not_configured",
    "channel_token_expired", "quota_exhausted", "provider_not_configured",
    "budget_exceeded", "source_not_authorized", "unsupported_media",
    "job_not_retryable", "storage_unavailable", "internal_error",
]


def ref(name: str) -> dict:
    return {"$ref": f"#/components/schemas/{name}"}


def err(*statuses: int) -> dict:
    out = {}
    for s in statuses:
        out[str(s)] = {
            "description": {
                400: "Validation error", 401: "Unauthenticated", 403: "Forbidden",
                404: "Not found (also returned for cross-workspace access)",
                409: "Conflict / illegal state", 422: "Unprocessable",
                429: "Rate limited", 502: "Upstream provider failure",
                503: "Service unavailable / dependency down",
            }.get(s, "Error"),
            "content": {"application/problem+json": {"schema": ref("Problem")}},
        }
    return out


def op(summary: str, *, tag: str, body: str | None = None, query: list | None = None,
       response: tuple[int, object] | tuple[int, object, str] | None = None,
       errors: tuple[int, ...] = (400, 401, 403, 404),
       desc: str | None = None, sse: bool = False) -> dict:
    o: dict = {"summary": summary, "tags": [tag], "operationId": summary_to_id(summary)}
    if desc:
        o["description"] = desc
    params = []
    if query:
        params.extend(query)
    if params:
        o["parameters"] = params
    if body:
        o["requestBody"] = {
            "required": True,
            "content": {"application/json": {"schema": ref(body)}},
        }
    o["responses"] = {}
    if response:
        code, schema = response[0], response[1]
        ctype = response[2] if len(response) > 2 else "application/json"
        o["responses"][str(code)] = {
            "description": "OK",
            "content": {ctype: {"schema": schema if isinstance(schema, dict) else ref(schema)}},
        }
    if sse:
        o["responses"]["200"] = {
            "description": "text/event-stream of stage/clip/job events",
            "content": {"text/event-stream": {"schema": {"type": "string"}}},
        }
    o["responses"].update(err(*errors))
    o["security"] = [{"sessionCookie": []}, {"bearerAuth": []}]
    return o


def summary_to_id(summary: str) -> str:
    keep = "".join(c if c.isalnum() or c == " " else " " for c in summary)
    parts = keep.split()
    return parts[0].lower() + "".join(p.capitalize() for p in parts[1:])


PAGE_PARAMS = [
    {"name": "limit", "in": "query", "schema": {"type": "integer", "default": 20, "maximum": 100}},
    {"name": "cursor", "in": "query", "schema": {"type": "string"}},
]


def paged(item: str) -> dict:
    return {
        "type": "object",
        "required": ["items"],
        "properties": {
            "items": {"type": "array", "items": ref(item)},
            "next_cursor": {"type": ["string", "null"]},
            "total_estimate": {"type": ["integer", "null"]},
        },
    }


SCH = {
    "Problem": PROBLEM,
    "Health": {
        "type": "object",
        "required": ["status", "version", "dependencies"],
        "properties": {
            "status": {"type": "string", "enum": ["ok", "degraded", "down"]},
            "version": {"type": "string"},
            "dependencies": {
                "type": "object",
                "additionalProperties": {
                    "type": "object",
                    "properties": {
                        "status": {"type": "string", "enum": ["ok", "degraded", "down"]},
                        "detail": {"type": "string"},
                    },
                },
            },
        },
    },
    "RegisterRequest": {
        "type": "object",
        "required": ["email", "password", "display_name"],
        "properties": {
            "email": {"type": "string", "format": "email"},
            "password": {"type": "string", "minLength": 10, "maxLength": 128},
            "display_name": {"type": "string", "minLength": 1, "maxLength": 80},
            "workspace_name": {"type": "string", "maxLength": 120},
        },
    },
    "LoginRequest": {
        "type": "object",
        "required": ["email", "password"],
        "properties": {
            "email": {"type": "string", "format": "email"},
            "password": {"type": "string"},
            "totp_code": {"type": "string"},
        },
    },
    "User": {
        "type": "object",
        "required": ["id", "email", "display_name"],
        "properties": {
            "id": {"type": "string", "format": "uuid"},
            "email": {"type": "string", "format": "email"},
            "display_name": {"type": "string"},
            "locale": {"type": "string"},
            "workspaces": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string", "format": "uuid"},
                        "name": {"type": "string"},
                        "role": {"type": "string", "enum": ["owner", "admin", "editor", "viewer"]},
                    },
                },
            },
            "created_at": {"type": "string", "format": "date-time"},
        },
    },
    "SourceVideo": {
        "type": "object",
        "required": ["id", "kind", "state"],
        "properties": {
            "id": {"type": "string", "format": "uuid"},
            "kind": {"type": "string", "enum": ["upload", "youtube", "cloud_storage"]},
            "state": {"type": "string", "enum": ["pending", "validating", "ready", "rejected", "deleted"]},
            "original_filename": {"type": ["string", "null"]},
            "duration_ms": {"type": ["integer", "null"]},
            "width": {"type": ["integer", "null"]},
            "height": {"type": ["integer", "null"]},
            "fps": {"type": ["number", "null"]},
            "video_codec": {"type": ["string", "null"]},
            "audio_codec": {"type": ["string", "null"]},
            "bytes": {"type": ["integer", "null"]},
            "sha256": {"type": ["string", "null"]},
            "rights_confirmed_at": {"type": ["string", "null"], "format": "date-time"},
            "probe": {"type": ["object", "null"], "additionalProperties": True},
        },
    },
    "ProjectCreate": {
        "type": "object",
        "required": ["name", "source"],
        "properties": {
            "name": {"type": "string", "minLength": 1, "maxLength": 200},
            "source": {
                "oneOf": [
                    {
                        "type": "object",
                        "required": ["kind", "source_id"],
                        "properties": {
                            "kind": {"const": "existing_source"},
                            "source_id": {"type": "string", "format": "uuid"},
                        },
                    },
                    {
                        "type": "object",
                        "required": ["kind", "youtube_url", "rights"],
                        "properties": {
                            "kind": {"const": "youtube_url"},
                            "youtube_url": {"type": "string", "format": "uri"},
                            "rights": ref("RightsDeclaration"),
                        },
                    },
                ]
            },
            "target_platform": {"type": "string", "default": "youtube_shorts"},
            "language": {"type": "string", "default": "en"},
            "clips_requested": {"type": "integer", "minimum": 1, "maximum": 30, "default": 5},
            "min_duration_ms": {"type": "integer", "minimum": 3000, "default": 15000},
            "max_duration_ms": {"type": "integer", "minimum": 5000, "maximum": 180000, "default": 65000},
            "preferred_durations_ms": {
                "type": "array", "items": {"type": "integer"}, "default": [15000, 30000, 45000]
            },
            "template_key": {"type": "string", "default": "podcast"},
            "caption_style_key": {"type": "string", "default": "bold_center"},
            "publishing_mode": {
                "type": "string", "enum": ["auto", "manual_approval", "private_review"],
                "default": "manual_approval",
            },
            "channel_id": {"type": ["string", "null"], "format": "uuid"},
            "budget_usd": {"type": ["number", "null"], "minimum": 0},
        },
    },
    "RightsDeclaration": {
        "type": "object",
        "required": ["basis", "statement"],
        "properties": {
            "basis": {
                "type": "string",
                "enum": ["original", "licensed", "permission", "creative_commons", "owned_channel"],
            },
            "statement": {"type": "string", "minLength": 10, "maxLength": 2000},
            "licence_reference": {"type": ["string", "null"]},
        },
        "description": "Recorded immutably in audit_logs. A YouTube URL alone is never treated as a grant.",
    },
    "Project": {
        "type": "object",
        "required": ["id", "name", "status"],
        "properties": {
            "id": {"type": "string", "format": "uuid"},
            "workspace_id": {"type": "string", "format": "uuid"},
            "name": {"type": "string"},
            "status": {"type": "string"},
            "progress": {"type": ["integer", "null"], "minimum": 0, "maximum": 100},
            "language": {"type": "string"},
            "clips_requested": {"type": "integer"},
            "min_duration_ms": {"type": "integer"},
            "max_duration_ms": {"type": "integer"},
            "template_key": {"type": "string"},
            "caption_style_key": {"type": "string"},
            "publishing_mode": {"type": "string"},
            "budget_usd": {"type": ["number", "null"]},
            "cost_usd": {"type": "number"},
            "error_code": {"type": ["string", "null"]},
            "error_detail": {"type": ["string", "null"]},
            "counts": {
                "type": "object",
                "properties": {
                    "candidates": {"type": "integer"},
                    "clips": {"type": "integer"},
                    "clips_passed_qa": {"type": "integer"},
                    "clips_approved": {"type": "integer"},
                    "clips_published": {"type": "integer"},
                },
            },
            "created_at": {"type": "string", "format": "date-time"},
            "updated_at": {"type": "string", "format": "date-time"},
        },
    },
    "ProjectUpdate": {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "clips_requested": {"type": "integer", "minimum": 1, "maximum": 30},
            "min_duration_ms": {"type": "integer"},
            "max_duration_ms": {"type": "integer"},
            "template_key": {"type": "string"},
            "caption_style_key": {"type": "string"},
            "publishing_mode": {"type": "string", "enum": ["auto", "manual_approval", "private_review"]},
            "channel_id": {"type": ["string", "null"], "format": "uuid"},
            "budget_usd": {"type": ["number", "null"]},
            "schedule_config": {"type": "object", "additionalProperties": True},
        },
        "description": "Changing settings never mutates the source file or existing clip versions.",
    },
    "StageEvent": {
        "type": "object",
        "properties": {
            "from_status": {"type": ["string", "null"]},
            "to_status": {"type": "string"},
            "task_name": {"type": ["string", "null"]},
            "job_id": {"type": ["string", "null"], "format": "uuid"},
            "progress": {"type": ["integer", "null"]},
            "error_code": {"type": ["string", "null"]},
            "detail": {"type": ["string", "null"]},
            "at": {"type": "string", "format": "date-time"},
        },
    },
    "TranscriptSegment": {
        "type": "object",
        "properties": {
            "idx": {"type": "integer"},
            "start_ms": {"type": "integer"},
            "end_ms": {"type": "integer"},
            "text": {"type": "string"},
            "speaker": {"type": ["string", "null"]},
            "words": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "w": {"type": "string"},
                        "s": {"type": "integer"},
                        "e": {"type": "integer"},
                        "p": {"type": ["number", "null"]},
                    },
                },
            },
        },
    },
    "Transcript": {
        "type": "object",
        "properties": {
            "id": {"type": "string", "format": "uuid"},
            "provider": {"type": "string"},
            "model": {"type": "string"},
            "language": {"type": "string"},
            "is_corrected": {"type": "boolean"},
            "word_timestamps": {"type": "boolean"},
            "confidence": {"type": ["number", "null"]},
            "segments": {"type": "array", "items": ref("TranscriptSegment")},
        },
    },
    "Topic": {
        "type": "object",
        "properties": {
            "label": {"type": "string"},
            "start_ms": {"type": "integer"},
            "end_ms": {"type": "integer"},
            "confidence": {"type": "number"},
        },
    },
    "Analysis": {
        "type": "object",
        "properties": {
            "schema_version": {"type": "integer"},
            "video_summary": {"type": ["string", "null"]},
            "language": {"type": ["string", "null"]},
            "topics": {"type": "array", "items": ref("Topic")},
            "speakers": {"type": "array", "items": {"type": "object", "additionalProperties": True}},
            "scenes": {"type": "array", "items": {"type": "object", "additionalProperties": True}},
            "important_moments": {"type": "array", "items": {"type": "object", "additionalProperties": True}},
            "key_statements": {"type": "array", "items": {"type": "object", "additionalProperties": True}},
            "candidate_segments": {"type": "array", "items": {"type": "object", "additionalProperties": True}},
            "analysis_confidence": {"type": ["number", "null"]},
        },
    },
    "Scores": {
        "type": "object",
        "required": ["content_potential", "technical_quality", "context_completeness",
                     "audience_relevance", "overall"],
        "description": "Decomposed on purpose: technical quality is never folded into engagement.",
        "properties": {
            "weights_version": {"type": "string"},
            "criteria": {
                "type": "object",
                "additionalProperties": {"type": "number", "minimum": 0, "maximum": 100},
            },
            "content_potential": {"type": "number", "minimum": 0, "maximum": 100},
            "technical_quality": {"type": "number", "minimum": 0, "maximum": 100},
            "context_completeness": {"type": "number", "minimum": 0, "maximum": 100},
            "audience_relevance": {"type": "number", "minimum": 0, "maximum": 100},
            "overall": {"type": "number", "minimum": 0, "maximum": 100},
        },
    },
    "Moment": {
        "type": "object",
        "properties": {
            "id": {"type": "string", "format": "uuid"},
            "start_ms": {"type": "integer"},
            "end_ms": {"type": "integer"},
            "required_duration_ms": {"type": "integer"},
            "category": {"type": "string"},
            "hook_text": {"type": ["string", "null"]},
            "context_text": {"type": ["string", "null"]},
            "payoff_text": {"type": ["string", "null"]},
            "ending_text": {"type": ["string", "null"]},
            "rationale": {"type": ["string", "null"]},
            "standalone_ok": {"type": "boolean"},
            "selected": {"type": "boolean"},
            "suppressed_reason": {"type": ["string", "null"]},
            "scores": ref("Scores"),
        },
    },
    "ClipEditSettings": {
        "type": "object",
        "additionalProperties": True,
        "description": (
            "Declarative render inputs: trim, crop/layout, caption style+position, overlays, "
            "music, branding. Compiled to FFmpeg; never mutates source media."
        ),
    },
    "MetadataSuggestion": {
        "type": "object",
        "properties": {
            "title_options": {"type": "array", "items": {"type": "string"}},
            "recommended_title": {"type": "string"},
            "description": {"type": "string"},
            "hashtags": {"type": "array", "items": {"type": "string"}},
            "tags": {"type": "array", "items": {"type": "string"}},
            "category_suggestion": {"type": "string"},
            "language": {"type": "string"},
            "target_audience": {"type": "string"},
            "thumbnail_frame_ms": {"type": ["integer", "null"]},
        },
    },
    "Clip": {
        "type": "object",
        "required": ["id", "state", "start_ms", "end_ms"],
        "properties": {
            "id": {"type": "string", "format": "uuid"},
            "project_id": {"type": "string", "format": "uuid"},
            "title": {"type": "string"},
            "state": {"type": "string", "enum": [
                "DRAFT", "RENDERING", "QA_FAILED", "QA_PASSED", "AWAITING_REVIEW",
                "APPROVED", "REJECTED", "PUBLISHED", "ARCHIVED"]},
            "start_ms": {"type": "integer"},
            "end_ms": {"type": "integer"},
            "duration_ms": {"type": "integer"},
            "layout": {"type": "string"},
            "template_key": {"type": "string"},
            "publishable": {"type": "boolean"},
            "current_version": ref("ClipVersion"),
            "scores": ref("Scores"),
            "metadata": ref("MetadataSuggestion"),
            "thumbnail_url": {"type": ["string", "null"], "description": "short-lived signed URL"},
            "created_at": {"type": "string", "format": "date-time"},
        },
    },
    "ClipVersion": {
        "type": "object",
        "properties": {
            "id": {"type": "string", "format": "uuid"},
            "version_no": {"type": "integer"},
            "source": {"type": "string", "enum": ["initial", "user_edit", "repair"]},
            "width": {"type": ["integer", "null"]},
            "height": {"type": ["integer", "null"]},
            "duration_ms": {"type": ["integer", "null"]},
            "bytes": {"type": ["integer", "null"]},
            "sha256": {"type": ["string", "null"]},
            "video_url": {"type": ["string", "null"], "description": "short-lived signed URL"},
            "rendered_at": {"type": ["string", "null"], "format": "date-time"},
            "created_at": {"type": "string", "format": "date-time"},
        },
    },
    "ClipPatch": {
        "type": "object",
        "properties": {
            "title": {"type": "string", "maxLength": 200},
            "metadata": ref("MetadataSuggestion"),
            "rejection_reason": {"type": "string"},
        },
    },
    "RegenerateRequest": {
        "type": "object",
        "properties": {
            "variant": {"type": "string", "enum": ["same", "shorter", "longer", "different_hook"]},
            "duration_ms": {"type": "integer", "minimum": 3000, "maximum": 180000},
            "reason": {"type": "string"},
        },
    },
    "RenderRequest": {
        "type": "object",
        "properties": {
            "settings": ref("ClipEditSettings"),
            "preview": {"type": "boolean", "default": False},
        },
    },
    "QualityCheck": {
        "type": "object",
        "properties": {
            "key": {"type": "string"},
            "verdict": {"type": "string", "enum": ["pass", "warn", "fail"]},
            "blocking": {"type": "boolean"},
            "measured": {"type": "string"},
            "expected": {"type": "string"},
            "detail": {"type": "string"},
        },
    },
    "QualityReport": {
        "type": "object",
        "properties": {
            "id": {"type": "string", "format": "uuid"},
            "clip_version_id": {"type": "string", "format": "uuid"},
            "kind": {"type": "string", "enum": ["technical", "editorial", "duplicate", "safety"]},
            "verdict": {"type": "string", "enum": ["pass", "warn", "fail"]},
            "score": {"type": ["number", "null"]},
            "checks": {"type": "array", "items": ref("QualityCheck")},
            "blocking_failures": {"type": "array", "items": {"type": "string"}},
            "created_at": {"type": "string", "format": "date-time"},
        },
        "description": "Safety and copyright checks are risk indicators, not legal determinations.",
    },
    "YouTubeChannel": {
        "type": "object",
        "properties": {
            "id": {"type": "string", "format": "uuid"},
            "channel_id": {"type": "string"},
            "channel_title": {"type": "string"},
            "channel_handle": {"type": ["string", "null"]},
            "avatar_url": {"type": ["string", "null"]},
            "state": {"type": "string", "enum": ["connected", "token_expired", "revoked", "error"]},
            "scopes": {"type": "array", "items": {"type": "string"}},
            "analytics_granted": {"type": "boolean"},
            "default_language": {"type": "string"},
            "timezone": {"type": "string"},
            "last_synced_at": {"type": ["string", "null"], "format": "date-time"},
        },
    },
    "OAuthStart": {
        "type": "object",
        "properties": {
            "authorization_url": {"type": "string", "format": "uri"},
            "state": {"type": "string"},
            "scopes": {"type": "array", "items": {"type": "string"}},
        },
    },
    "PublishRequest": {
        "type": "object",
        "required": ["channel_id"],
        "properties": {
            "channel_id": {"type": "string", "format": "uuid"},
            "clip_version_id": {"type": ["string", "null"], "format": "uuid"},
            "privacy": {"type": "string", "enum": ["private", "unlisted", "public"], "default": "private"},
            "metadata": ref("MetadataSuggestion"),
            "publish_at": {"type": ["string", "null"], "format": "date-time"},
            "make_for_kids": {"type": "boolean", "default": False},
            "category_id": {"type": ["string", "null"]},
            "upload_thumbnail": {"type": "boolean", "default": False},
        },
        "description": "Requires a QA-passing clip version. Returns 409 clip_not_publishable otherwise.",
    },
    "ScheduleRequest": {
        "type": "object",
        "required": ["channel_id", "scheduled_at"],
        "properties": {
            "channel_id": {"type": "string", "format": "uuid"},
            "scheduled_at": {"type": "string", "format": "date-time", "description": "UTC"},
            "privacy": {"type": "string", "enum": ["private", "unlisted"], "default": "private"},
            "metadata": ref("MetadataSuggestion"),
        },
    },
    "PublishingJob": {
        "type": "object",
        "properties": {
            "id": {"type": "string", "format": "uuid"},
            "clip_id": {"type": "string", "format": "uuid"},
            "channel_id": {"type": "string", "format": "uuid"},
            "state": {"type": "string", "enum": [
                "queued", "scheduled", "uploading", "processing", "published", "failed", "cancelled"]},
            "privacy": {"type": "string"},
            "scheduled_at": {"type": ["string", "null"], "format": "date-time"},
            "progress": {"type": ["integer", "null"]},
            "bytes_uploaded": {"type": ["integer", "null"]},
            "total_bytes": {"type": ["integer", "null"]},
            "youtube_video_id": {"type": ["string", "null"]},
            "youtube_url": {"type": ["string", "null"]},
            "attempts": {"type": "integer"},
            "error_code": {"type": ["string", "null"]},
            "error_detail": {"type": ["string", "null"]},
            "created_at": {"type": "string", "format": "date-time"},
            "finished_at": {"type": ["string", "null"], "format": "date-time"},
        },
    },
    "Job": {
        "type": "object",
        "properties": {
            "id": {"type": "string", "format": "uuid"},
            "project_id": {"type": ["string", "null"], "format": "uuid"},
            "clip_id": {"type": ["string", "null"], "format": "uuid"},
            "stage": {"type": "string"},
            "queue": {"type": "string"},
            "task_name": {"type": "string"},
            "status": {"type": "string", "enum": [
                "PENDING", "RUNNING", "SUCCEEDED", "FAILED", "RETRYING", "CANCELLED", "DEAD_LETTER"]},
            "progress": {"type": ["integer", "null"]},
            "progress_note": {"type": ["string", "null"]},
            "attempts": {"type": "integer"},
            "max_attempts": {"type": "integer"},
            "error_code": {"type": ["string", "null"]},
            "error_detail": {"type": ["string", "null"]},
            "trace_id": {"type": ["string", "null"]},
            "started_at": {"type": ["string", "null"], "format": "date-time"},
            "finished_at": {"type": ["string", "null"], "format": "date-time"},
            "next_retry_at": {"type": ["string", "null"], "format": "date-time"},
        },
    },
    "AnalyticsOverview": {
        "type": "object",
        "properties": {
            "period": {
                "type": "object",
                "properties": {
                    "start": {"type": "string", "format": "date"},
                    "end": {"type": "string", "format": "date"},
                },
            },
            "totals": {
                "type": "object",
                "properties": {
                    "views": {"type": "integer"},
                    "watch_time_minutes": {"type": "number"},
                    "avg_view_percentage": {"type": ["number", "null"]},
                    "clips_published": {"type": "integer"},
                },
            },
            "series": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "date": {"type": "string", "format": "date"},
                        "views": {"type": "integer"},
                        "watch_time_minutes": {"type": "number"},
                    },
                },
            },
            "insights": {
                "type": "array",
                "description": "Only emitted when sample size is sufficient; states uncertainty.",
                "items": {
                    "type": "object",
                    "properties": {
                        "statement": {"type": "string"},
                        "sample_size": {"type": "integer"},
                        "confidence": {"type": "string", "enum": ["low", "moderate", "high"]},
                        "caveat": {"type": "string"},
                    },
                },
            },
        },
    },
    "UsageSummary": {
        "type": "object",
        "properties": {
            "period": {"type": "object", "additionalProperties": {"type": "string"}},
            "total_usd": {"type": "number"},
            "budget_usd": {"type": ["number", "null"]},
            "by_category": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "category": {"type": "string"},
                        "usd": {"type": "number"},
                        "quantity": {"type": "number"},
                        "unit": {"type": "string"},
                    },
                },
            },
            "storage_mb": {"type": "number"},
        },
    },
    "Notification": {
        "type": "object",
        "properties": {
            "id": {"type": "string", "format": "uuid"},
            "kind": {"type": "string"},
            "title": {"type": "string"},
            "body": {"type": ["string", "null"]},
            "project_id": {"type": ["string", "null"], "format": "uuid"},
            "clip_id": {"type": ["string", "null"], "format": "uuid"},
            "read_at": {"type": ["string", "null"], "format": "date-time"},
            "created_at": {"type": "string", "format": "date-time"},
        },
    },
}

for name, item in [
    ("ProjectPage", paged("Project")), ("ClipPage", paged("Clip")),
    ("JobPage", paged("Job")), ("PublishingJobPage", paged("PublishingJob")),
    ("NotificationPage", paged("Notification")), ("ChannelPage", paged("YouTubeChannel")),
    ("MomentPage", paged("Moment")), ("QualityReportPage", paged("QualityReport")),
]:
    SCH[name] = item

PATHS: dict = {}


_PATH_PARAM_TYPES = {
    "project_id": "uuid", "clip_id": "uuid", "source_id": "uuid", "job_id": "uuid",
    "channel_id": "uuid", "version_id": "uuid", "notification_id": "uuid",
}


def add(path: str, method: str, o: dict) -> None:
    """Register an operation, auto-declaring any {path} parameters it references."""
    declared = {p["name"] for p in o.get("parameters", []) if p.get("in") == "path"}
    for name in _re.findall(r"\{([a-z_]+)\}", path):
        if name in declared:
            continue
        schema = {"type": "string"}
        if _PATH_PARAM_TYPES.get(name) == "uuid":
            schema["format"] = "uuid"
        o.setdefault("parameters", []).insert(
            0, {"name": name, "in": "path", "required": True, "schema": schema}
        )
    PATHS.setdefault(path, {})[method] = o


# ------------------------------------------------------------------ health
add("/api/v1/health", "get", op(
    "Get Health", tag="System", response=(200, "Health"), errors=(503,),
    desc="Dependency-aware: reports DB, Redis, storage and provider configuration status. "
         "Missing configuration is reported explicitly, never masked as OK.",
))
add("/api/v1/health/livez", "get", op("Get Livez", tag="System", response=(200, {"type": "object"}), errors=()))
add("/api/v1/health/readyz", "get", op("Get Readyz", tag="System", response=(200, {"type": "object"}), errors=(503,)))

# ------------------------------------------------------------------ auth
add("/api/v1/auth/register", "post", op(
    "Register", tag="Auth", body="RegisterRequest", response=(201, "User"), errors=(400, 409, 429)))
add("/api/v1/auth/login", "post", op(
    "Login", tag="Auth", body="LoginRequest", response=(200, "User"), errors=(400, 401, 429),
    desc="Sets HttpOnly session cookie and returns a CSRF token for mutating requests."))
add("/api/v1/auth/logout", "post", op(
    "Logout", tag="Auth", response=(204, {"type": "object"}), errors=(401,)))
add("/api/v1/auth/me", "get", op("Get Me", tag="Auth", response=(200, "User"), errors=(401,)))

# ------------------------------------------------------------------ projects
add("/api/v1/projects", "post", op(
    "Create Project", tag="Projects", body="ProjectCreate", response=(201, "Project"),
    errors=(400, 401, 403, 404, 409, 429),
    desc="Creates a project in SOURCE_VALIDATION. A YouTube source requires a rights declaration; "
         "if no authorized media source exists the response explains the situation and offers upload."))
add("/api/v1/projects", "get", op(
    "List Projects", tag="Projects", query=[*PAGE_PARAMS,
        {"name": "status", "in": "query", "schema": {"type": "string"}},
        {"name": "q", "in": "query", "schema": {"type": "string"}},
        {"name": "channel_id", "in": "query", "schema": {"type": "string", "format": "uuid"}},
        {"name": "from", "in": "query", "schema": {"type": "string", "format": "date"}},
        {"name": "to", "in": "query", "schema": {"type": "string", "format": "date"}},
    ], response=(200, "ProjectPage"), errors=(401, 403)))
add("/api/v1/projects/{project_id}", "get", op(
    "Get Project", tag="Projects", response=(200, "Project")))
add("/api/v1/projects/{project_id}", "patch", op(
    "Update Project", tag="Projects", body="ProjectUpdate", response=(200, "Project"), errors=(400, 401, 403, 404, 409)))
add("/api/v1/projects/{project_id}", "delete", op(
    "Delete Project", tag="Projects", response=(204, {"type": "object"}),
    errors=(401, 403, 404, 409),
    desc="Soft delete; refused while non-terminal jobs reference the source."))
add("/api/v1/projects/{project_id}/stages", "get", op(
    "List Project Stages", tag="Projects", response=(200, {
        "type": "object",
        "properties": {"items": {"type": "array", "items": ref("StageEvent")}},
    }), desc="Append-only state-machine journal for the project."))
add("/api/v1/projects/{project_id}/events", "get", op(
    "Stream Project Events", tag="Projects", sse=True, errors=(401, 403, 404),
    desc="Server-Sent Events. Supports Last-Event-ID replay from a Redis stream. "
         "progress is null when a stage has no measurable denominator; ETA only after 2 comparable stages."))

# ------------------------------------------------------------------ sources
add("/api/v1/sources/upload", "post", {
    **op("Upload Source", tag="Sources", response=(201, "SourceVideo"),
         errors=(400, 401, 403, 413, 415, 422, 429),
         desc="multipart/form-data. Validates extension + MIME + magic bytes + real probe before "
              "accepting; computes SHA-256; rejects corrupt files. Enforces size limits."),
    "requestBody": {
        "required": True,
        "content": {
            "multipart/form-data": {
                "schema": {
                    "type": "object",
                    "required": ["file", "rights_basis", "rights_statement"],
                    "properties": {
                        "file": {"type": "string", "format": "binary"},
                        "project_id": {"type": "string", "format": "uuid"},
                        "rights_basis": {"type": "string"},
                        "rights_statement": {"type": "string"},
                    },
                }
            }
        },
    },
})
add("/api/v1/sources/youtube", "post", op(
    "Add Youtube Source", tag="Sources", body="ProjectCreate", response=(201, "SourceVideo"),
    errors=(400, 401, 403, 409, 422),
    desc="Validates URL and video id, resolves permitted public metadata, and checks whether an "
         "authorized media source exists. Never assumes a public URL authorizes downloading."))
add("/api/v1/sources/{source_id}", "get", op(
    "Get Source", tag="Sources", response=(200, "SourceVideo")))
add("/api/v1/sources/{source_id}", "delete", op(
    "Delete Source", tag="Sources", response=(204, {"type": "object"}), errors=(401, 403, 404, 409)))

# ------------------------------------------------------------------ analysis
add("/api/v1/projects/{project_id}/analyze", "post", op(
    "Start Analysis", tag="Analysis", response=(202, {
        "type": "object",
        "properties": {
            "job_id": {"type": "string", "format": "uuid"},
            "resume_from": {"type": ["string", "null"]},
            "stream_url": {"type": "string"},
        },
    }), errors=(400, 401, 403, 404, 409, 402),
    desc="Idempotent. Resumes from the last successful checkpoint stage; never repeats paid stages."))
add("/api/v1/projects/{project_id}/analysis", "get", op(
    "Get Analysis", tag="Analysis", response=(200, "Analysis"), errors=(401, 403, 404, 409),
    desc="409 analysis_not_ready when the stage has not completed."))
add("/api/v1/projects/{project_id}/transcript", "get", op(
    "Get Transcript", tag="Analysis", response=(200, "Transcript"),
    query=[{"name": "include_words", "in": "query", "schema": {"type": "boolean", "default": True}}]))
add("/api/v1/projects/{project_id}/moments", "get", op(
    "List Moments", tag="Analysis", query=[*PAGE_PARAMS,
        {"name": "selected_only", "in": "query", "schema": {"type": "boolean", "default": False}},
        {"name": "category", "in": "query", "schema": {"type": "string"}},
    ], response=(200, "MomentPage")))

# ------------------------------------------------------------------ clips
add("/api/v1/projects/{project_id}/generate-clips", "post", op(
    "Generate Clips", tag="Clips", body="RenderRequest", response=(202, {
        "type": "object",
        "properties": {
            "job_id": {"type": "string", "format": "uuid"},
            "candidates_selected": {"type": "integer"},
            "stream_url": {"type": "string"},
        },
    }), errors=(400, 401, 403, 404, 409, 402)))
add("/api/v1/projects/{project_id}/clips", "get", op(
    "List Project Clips", tag="Clips", query=[*PAGE_PARAMS,
        {"name": "state", "in": "query", "schema": {"type": "string"}},
        {"name": "publishable", "in": "query", "schema": {"type": "boolean"}},
        {"name": "sort", "in": "query", "schema": {"type": "string", "enum": ["created_at", "overall", "duration_ms"]}},
    ], response=(200, "ClipPage")))
add("/api/v1/clips/{clip_id}", "get", op("Get Clip", tag="Clips", response=(200, "Clip")))
add("/api/v1/clips/{clip_id}", "patch", op(
    "Update Clip", tag="Clips", body="ClipPatch", response=(200, "Clip")))
add("/api/v1/clips/{clip_id}", "delete", op(
    "Delete Clip", tag="Clips", response=(204, {"type": "object"}), errors=(401, 403, 404, 409)))
add("/api/v1/clips/{clip_id}/regenerate", "post", op(
    "Regenerate Clip", tag="Clips", body="RegenerateRequest", response=(202, {
        "type": "object", "properties": {"job_id": {"type": "string", "format": "uuid"}},
    }), errors=(400, 401, 403, 404, 409, 402),
    desc="Creates a new version; the previous version is retained."))
add("/api/v1/clips/{clip_id}/approve", "post", op(
    "Approve Clip", tag="Clips", response=(200, "Clip"), errors=(401, 403, 404, 409),
    desc="409 clip_not_publishable when no QA-passing version exists."))
add("/api/v1/clips/{clip_id}/reject", "post", op(
    "Reject Clip", tag="Clips", body="ClipPatch", response=(200, "Clip"), errors=(400, 401, 403, 404)))
add("/api/v1/clips/{clip_id}/download", "get", op(
    "Get Clip Download", tag="Clips", response=(302, {"type": "object"}), errors=(401, 403, 404),
    desc="Redirects to a short-lived signed storage URL (≤10 min)."))

# ------------------------------------------------------------------ editing
add("/api/v1/clips/{clip_id}/versions", "get", op(
    "List Clip Versions", tag="Editing", response=(200, {
        "type": "object", "properties": {"items": {"type": "array", "items": ref("ClipVersion")}},
    })))
add("/api/v1/clips/{clip_id}/render", "post", op(
    "Render Clip", tag="Editing", body="RenderRequest", response=(202, {
        "type": "object", "properties": {"job_id": {"type": "string", "format": "uuid"}},
    }), errors=(400, 401, 403, 404, 409, 402)))
add("/api/v1/clips/{clip_id}/edit-settings", "patch", op(
    "Update Edit Settings", tag="Editing", body="ClipEditSettings", response=(200, {
        "type": "object", "properties": {"settings": ref("ClipEditSettings"), "updated_at": {"type": "string"}},
    }), desc="Saves editing instructions only; rendering is a separate explicit step."))
add("/api/v1/clips/{clip_id}/versions/{version_id}/restore", "post", op(
    "Restore Clip Version", tag="Editing", response=(202, {
        "type": "object", "properties": {"job_id": {"type": "string", "format": "uuid"}},
    }), errors=(401, 403, 404, 409)))

# ------------------------------------------------------------------ captions
add("/api/v1/clips/{clip_id}/captions", "get", op(
    "Get Captions", tag="Editing", response=(200, {
        "type": "object",
        "properties": {
            "language": {"type": "string"},
            "style_key": {"type": "string"},
            "ass_text": {"type": "string"},
            "vtt_url": {"type": ["string", "null"]},
            "cues": {"type": "array", "items": {"type": "object", "additionalProperties": True}},
            "validation": {"type": "object", "additionalProperties": True},
        },
    })))
add("/api/v1/clips/{clip_id}/captions", "patch", op(
    "Update Captions", tag="Editing", body={
        "type": "object",
        "properties": {
            "style_key": {"type": "string"},
            "cues": {"type": "array", "items": {"type": "object", "additionalProperties": True}},
        },
    }, response=(200, {"type": "object"}), errors=(400, 401, 403, 404, 422),
    desc="Stores subtitle data separately from the rendered video so captions can be regenerated "
         "without re-running AI analysis."))

# ------------------------------------------------------------------ QA
add("/api/v1/clips/{clip_id}/quality-check", "post", op(
    "Run Quality Check", tag="Quality", body={
        "type": "object",
        "properties": {
            "clip_version_id": {"type": ["string", "null"], "format": "uuid"},
            "kinds": {"type": "array", "items": {"type": "string", "enum": [
                "technical", "editorial", "duplicate", "safety"]}},
            "auto_repair": {"type": "boolean", "default": True},
        },
    }, response=(202, {
        "type": "object", "properties": {"job_id": {"type": "string", "format": "uuid"}},
    }), errors=(400, 401, 403, 404, 409)))
add("/api/v1/clips/{clip_id}/quality-report", "get", op(
    "Get Quality Report", tag="Quality", query=[PAGE_PARAMS[0]], response=(200, "QualityReportPage")))

# ------------------------------------------------------------------ youtube
add("/api/v1/youtube/channels", "get", op(
    "List Youtube Channels", tag="YouTube", response=(200, "ChannelPage"), errors=(401, 403)))
add("/api/v1/youtube/channels/{channel_id}", "patch", op(
    "Update Youtube Channel", tag="YouTube", body={
        "type": "object",
        "properties": {
            "default_language": {"type": "string"},
            "default_caption_style": {"type": "string"},
            "timezone": {"type": "string"},
            "branding": {"type": "object", "additionalProperties": True},
            "publishing_prefs": {"type": "object", "additionalProperties": True},
        },
    }, response=(200, "YouTubeChannel")))
add("/api/v1/youtube/channels/{channel_id}", "delete", op(
    "Disconnect Youtube Channel", tag="YouTube", response=(204, {"type": "object"}),
    errors=(401, 403, 404),
    desc="Revokes the grant at Google, then removes tokens locally. Clips are not deleted."))
add("/api/v1/youtube/oauth/start", "get", op(
    "Start Youtube Oauth", tag="YouTube", query=[
        {"name": "return_to", "in": "query", "schema": {"type": "string"}},
    ], response=(200, "OAuthStart"), errors=(401, 403),
    desc="Returns the Google consent URL. State is signed, single-use and bound to user+workspace; "
         "PKCE is used. 409 youtube_not_configured when client credentials are absent."))
add("/api/v1/youtube/oauth/callback", "get", op(
    "Youtube Oauth Callback", tag="YouTube", query=[
        {"name": "code", "in": "query", "schema": {"type": "string"}},
        {"name": "state", "in": "query", "schema": {"type": "string"}},
        {"name": "error", "in": "query", "schema": {"type": "string"}},
    ], response=(302, {"type": "object"}), errors=(400, 401, 403, 409),
    desc="Exchanges the code, encrypts and stores tokens, verifies the channel via the API."))

# ------------------------------------------------------------------ publishing
add("/api/v1/clips/{clip_id}/publish", "post", op(
    "Publish Clip", tag="Publishing", body="PublishRequest", response=(202, "PublishingJob"),
    errors=(400, 401, 403, 404, 409, 429),
    desc="Idempotent per (clip_version, channel). 409 clip_not_publishable if QA has not passed. "
         "Only reports published after the API confirms and the video resource is recorded."))
add("/api/v1/clips/{clip_id}/schedule", "post", op(
    "Schedule Clip", tag="Publishing", body="ScheduleRequest", response=(201, "PublishingJob"),
    errors=(400, 401, 403, 404, 409),
    desc="Persistent scheduler (DB + Beat tick), not an in-memory timer. Stored in UTC; "
         "requires private privacy + publishAt per API semantics."))
add("/api/v1/publishing/history", "get", op(
    "Get Publishing History", tag="Publishing", query=[*PAGE_PARAMS,
        {"name": "channel_id", "in": "query", "schema": {"type": "string", "format": "uuid"}},
        {"name": "state", "in": "query", "schema": {"type": "string"}},
        {"name": "from", "in": "query", "schema": {"type": "string", "format": "date"}},
        {"name": "to", "in": "query", "schema": {"type": "string", "format": "date"}},
    ], response=(200, "PublishingJobPage")))
add("/api/v1/publishing/{job_id}", "get", op("Get Publishing Job", tag="Publishing", response=(200, "PublishingJob")))
add("/api/v1/publishing/{job_id}", "delete", op(
    "Cancel Publishing Job", tag="Publishing", response=(200, "PublishingJob"), errors=(401, 403, 404, 409),
    desc="Cancels a queued/scheduled job. Cannot cancel an in-flight upload; that is reported honestly."))
add("/api/v1/publishing/calendar", "get", op(
    "Get Publishing Calendar", tag="Publishing", query=[
        {"name": "from", "in": "query", "schema": {"type": "string", "format": "date"}, "required": True},
        {"name": "to", "in": "query", "schema": {"type": "string", "format": "date"}, "required": True},
        {"name": "channel_id", "in": "query", "schema": {"type": "string", "format": "uuid"}},
    ], response=(200, {
        "type": "object",
        "properties": {
            "items": {"type": "array", "items": ref("PublishingJob")},
            "timezone": {"type": "string"},
        },
    })))
add("/api/v1/webhooks/youtube", "post", op(
    "Youtube Webhook", tag="Publishing", response=(204, {"type": "object"}), errors=(400, 401),
    desc="PubSubHubbub verification + processing-status notifications."))

# ------------------------------------------------------------------ analytics
add("/api/v1/analytics/overview", "get", op(
    "Get Analytics Overview", tag="Analytics", query=[
        {"name": "from", "in": "query", "schema": {"type": "string", "format": "date"}},
        {"name": "to", "in": "query", "schema": {"type": "string", "format": "date"}},
        {"name": "channel_id", "in": "query", "schema": {"type": "string", "format": "uuid"}},
    ], response=(200, "AnalyticsOverview"), errors=(401, 403),
    desc="Returns only measured data. Insights require a minimum sample and state uncertainty."))
add("/api/v1/analytics/channels/{channel_id}", "get", op(
    "Get Channel Analytics", tag="Analytics", query=[
        {"name": "from", "in": "query", "schema": {"type": "string", "format": "date"}},
        {"name": "to", "in": "query", "schema": {"type": "string", "format": "date"}},
    ], response=(200, "AnalyticsOverview"), errors=(401, 403, 404, 409)))
add("/api/v1/analytics/clips/{clip_id}", "get", op(
    "Get Clip Analytics", tag="Analytics", response=(200, {
        "type": "object",
        "properties": {
            "clip_id": {"type": "string", "format": "uuid"},
            "snapshots": {"type": "array", "items": {"type": "object", "additionalProperties": True}},
            "note": {"type": "string"},
        },
    }), errors=(401, 403, 404)))

# ------------------------------------------------------------------ jobs
add("/api/v1/jobs/{job_id}", "get", op("Get Job", tag="Jobs", response=(200, "Job")))
add("/api/v1/jobs/{job_id}/retry", "post", op(
    "Retry Job", tag="Jobs", response=(202, "Job"), errors=(401, 403, 404, 409),
    desc="409 job_not_retryable for terminal-but-unretryable states."))
add("/api/v1/jobs/{job_id}/cancel", "post", op(
    "Cancel Job", tag="Jobs", response=(200, "Job"), errors=(401, 403, 404, 409)))
add("/api/v1/jobs", "get", op(
    "List Jobs", tag="Jobs", query=[*PAGE_PARAMS,
        {"name": "status", "in": "query", "schema": {"type": "string"}},
        {"name": "queue", "in": "query", "schema": {"type": "string"}},
        {"name": "project_id", "in": "query", "schema": {"type": "string", "format": "uuid"}},
    ], response=(200, "JobPage")))

# ------------------------------------------------------------------ system extras
add("/api/v1/notifications", "get", op(
    "List Notifications", tag="Notifications", query=[*PAGE_PARAMS,
        {"name": "unread_only", "in": "query", "schema": {"type": "boolean", "default": False}},
    ], response=(200, "NotificationPage")))
add("/api/v1/notifications/{notification_id}/read", "post", op(
    "Mark Notification Read", tag="Notifications", response=(200, "Notification"), errors=(401, 403, 404)))
add("/api/v1/usage/summary", "get", op(
    "Get Usage Summary", tag="Billing", response=(200, "UsageSummary"), errors=(401, 403),
    desc="Actual metered usage; warns at 80% of budget and hard-stops at 100%."))
add("/api/v1/dashboard/summary", "get", op(
    "Get Dashboard Summary", tag="Dashboard", response=(200, {
        "type": "object",
        "properties": {
            "projects_total": {"type": "integer"},
            "sources_processed": {"type": "integer"},
            "clips_generated": {"type": "integer"},
            "clips_approved": {"type": "integer"},
            "clips_published": {"type": "integer"},
            "clips_scheduled": {"type": "integer"},
            "failed_jobs": {"type": "integer"},
            "queue_depth": {"type": "integer"},
            "storage_mb": {"type": "number"},
            "ai_cost_usd": {"type": "number"},
            "recent_projects": {"type": "array", "items": ref("Project")},
            "recent_activity": {"type": "array", "items": {"type": "object", "additionalProperties": True}},
        },
    }), errors=(401, 403),
    desc="Every value is database-backed. Empty workspace returns zeros plus an empty-state flag, "
         "never invented statistics."))

SPEC = {
    "openapi": "3.1.0",
    "info": {
        "title": "AutoClipper AI API",
        "version": "0.1.0-phase0",
        "description": (
            "AI video clipping and YouTube publishing platform. Phase 0 contract.\n\n"
            "Conventions: RFC 9457 problem+json errors with stable `code`; cursor pagination "
            "(limit ≤ 100); long-running operations return 202 + job_id and expose an SSE stream; "
            "mutating POSTs accept `Idempotency-Key`; timestamps are ISO-8601 UTC, media positions "
            "are integer milliseconds with half-open intervals.\n\n"
            "Cross-workspace access returns 404, never 403, to avoid existence leaks."
        ),
        "contact": {"name": "AutoClipper Engineering"},
        "license": {"name": "Proprietary"},
    },
    "servers": [
        {"url": "http://localhost:8000", "description": "local"},
        {"url": "https://api.autoclip.ai", "description": "production"},
    ],
    "tags": [
        {"name": "Auth"}, {"name": "Projects"}, {"name": "Sources"}, {"name": "Analysis"},
        {"name": "Clips"}, {"name": "Editing"}, {"name": "Quality"}, {"name": "YouTube"},
        {"name": "Publishing"}, {"name": "Analytics"}, {"name": "Jobs"}, {"name": "Notifications"},
        {"name": "Billing"}, {"name": "Dashboard"}, {"name": "System"},
    ],
    "paths": PATHS,
    "components": {
        "schemas": SCH,
        "securitySchemes": {
            "sessionCookie": {"type": "apiKey", "in": "cookie", "name": "ac_session"},
            "bearerAuth": {"type": "http", "scheme": "bearer"},
        },
    },
    "x-error-codes": ERROR_CODES,
}


def main() -> None:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(SPEC, indent=2) + "\n")
    n_ops = sum(len([m for m in v if m in {"get", "post", "patch", "put", "delete"}]) for v in PATHS.values())
    print(f"wrote {OUT} — {len(PATHS)} paths, {n_ops} operations, {len(SCH)} schemas")


if __name__ == "__main__":
    main()
