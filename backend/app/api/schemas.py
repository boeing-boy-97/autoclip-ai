"""Pydantic response/request schemas for API v1.

These are the wire contract. They are deliberately separate from the ORM models: an ORM change
must never silently alter the public API, and secrets (password_hash, token hashes) can never be
serialised by accident.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, model_validator

from app.db.models import User
from app.db.models import Workspace as WorkspaceModel


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# --------------------------------------------------------------------------- identity


class WorkspaceRef(BaseModel):
    id: uuid.UUID
    name: str
    role: str


def workspace_memberships(pairs: list[tuple[WorkspaceModel, str]]) -> list[WorkspaceRef]:
    return [WorkspaceRef(id=ws.id, name=ws.name, role=role) for ws, role in pairs]


class UserOut(ORMModel):
    id: uuid.UUID
    email: str
    display_name: str
    locale: str = "en"
    status: str = "active"
    created_at: datetime | None = None
    workspaces: list[WorkspaceRef] = Field(default_factory=list)

    @classmethod
    def from_model(cls, user: User, memberships: list[WorkspaceRef] | None = None) -> UserOut:
        out = cls.model_validate(user)
        out.workspaces = memberships or []
        return out


class WorkspaceOut(BaseModel):
    id: uuid.UUID
    name: str
    slug: str
    default_language: str
    role: str | None = None
    created_at: datetime


# --------------------------------------------------------------------------- sources


class RightsDeclaration(BaseModel):
    """Compliance gate. A YouTube URL alone is never treated as a media grant."""

    basis: str = Field(
        description="original | licensed | permission | creative_commons | owned_channel"
    )
    statement: str = Field(min_length=10, max_length=2000)
    licence_reference: str | None = None

    @model_validator(mode="after")
    def _valid_basis(self) -> RightsDeclaration:
        allowed = {"original", "licensed", "permission", "creative_commons", "owned_channel"}
        if self.basis not in allowed:
            raise ValueError(f"basis must be one of {sorted(allowed)}")
        if not self.statement.strip():
            raise ValueError("statement must describe your rights to this media")
        return self


class SourceOut(ORMModel):
    id: uuid.UUID
    kind: str
    state: str
    original_filename: str | None = None
    duration_ms: int | None = None
    width: int | None = None
    height: int | None = None
    fps: float | None = None
    video_codec: str | None = None
    audio_codec: str | None = None
    byte_size: int | None = None
    sha256: str | None = None
    rights_basis: str | None = None
    rights_confirmed_at: datetime | None = None
    probe: dict[str, Any] | None = None

    @classmethod
    def from_model(cls, source) -> SourceOut:  # noqa: ANN001
        return cls.model_validate(
            {
                **{
                    k: getattr(source, k)
                    for k in cls.model_fields
                    if k not in {"sha256", "probe"}
                },
                "sha256": source.sha256.hex() if source.sha256 else None,
                "probe": source.probe_json,
            }
        )


# --------------------------------------------------------------------------- projects


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    target_platform: str = "youtube_shorts"
    language: str = "en"
    clips_requested: int = Field(default=5, ge=1, le=30)
    min_duration_ms: int = Field(default=15000, ge=3000)
    max_duration_ms: int = Field(default=65000, le=180000)
    preferred_durations_ms: list[int] = Field(default_factory=lambda: [15000, 30000, 45000])
    template_key: str = "podcast"
    caption_style_key: str = "bold_center"
    publishing_mode: str = "manual_approval"
    budget_usd: float | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _check_durations(self) -> ProjectCreate:
        if self.min_duration_ms >= self.max_duration_ms:
            raise ValueError("min_duration_ms must be less than max_duration_ms")
        if self.publishing_mode not in {"auto", "manual_approval", "private_review"}:
            raise ValueError("publishing_mode must be auto, manual_approval or private_review")
        return self


class ProjectUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    clips_requested: int | None = Field(default=None, ge=1, le=30)
    min_duration_ms: int | None = Field(default=None, ge=3000)
    max_duration_ms: int | None = Field(default=None, le=180000)
    template_key: str | None = None
    caption_style_key: str | None = None
    publishing_mode: str | None = None
    budget_usd: float | None = Field(default=None, ge=0)


class ProjectCounts(BaseModel):
    sources: int = 0
    jobs_active: int = 0


class ProjectOut(ORMModel):
    id: uuid.UUID
    workspace_id: uuid.UUID
    name: str
    status: str
    progress: int | None = None
    language: str
    target_platform: str
    clips_requested: int
    min_duration_ms: int
    max_duration_ms: int
    template_key: str
    caption_style_key: str
    publishing_mode: str
    budget_usd: float | None = None
    cost_usd: float = 0
    error_code: str | None = None
    error_detail: str | None = None
    created_at: datetime
    updated_at: datetime
    source: SourceOut | None = None


class ProjectDetail(ProjectOut):
    stage_events: list[StageEventOut] = Field(default_factory=list)

    @classmethod
    def build(
        cls, project, events, source=None  # noqa: ANN001
    ) -> ProjectDetail:
        """Assemble from ORM objects, normalising the nested source.

        `ProjectOut.source` is a `SourceOut`, and the ORM's `sha256` is bytes. Validating the
        ORM object wholesale therefore fails at serialisation time — a bug that stays invisible
        until a project actually has a source. Construction goes through `SourceOut.from_model`.
        """
        detail = cls.model_validate(
            {
                **{
                    name: getattr(project, name)
                    for name in ProjectOut.model_fields
                    if name != "source"
                },
                "source": SourceOut.from_model(source) if source is not None else None,
            }
        )
        detail.stage_events = [StageEventOut.model_validate(e) for e in events]
        return detail


class StageEventOut(ORMModel):
    from_status: str | None = None
    to_status: str
    task_name: str | None = None
    job_id: uuid.UUID | None = None
    progress: int | None = None
    error_code: str | None = None
    detail: str | None = None
    at: datetime


class ProjectPage(BaseModel):
    items: list[ProjectOut]
    next_cursor: str | None = None
    total_estimate: int | None = None


# --------------------------------------------------------------------------- jobs


class JobOut(ORMModel):
    id: uuid.UUID
    project_id: uuid.UUID | None = None
    stage: str
    queue: str
    task_name: str
    status: str
    progress: int | None = None
    attempts: int = 0
    max_attempts: int = 3
    error_code: str | None = None
    error_detail: str | None = None
    progress_note: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    next_retry_at: datetime | None = None
    created_at: datetime | None = None


class JobPage(BaseModel):
    items: list[JobOut]
    next_cursor: str | None = None


# --------------------------------------------------------------------------- dashboard


class ActivityOut(ORMModel):
    action: str
    target_type: str | None = None
    at: datetime


class DashboardSummary(BaseModel):
    projects_total: int
    projects_by_status: dict[str, int]
    sources_processed: int
    failed_jobs: int
    queue_depth: int
    storage_mb: float
    unread_notifications: int
    recent_projects: list[ProjectOut]
    recent_activity: list[ActivityOut]
    is_empty: bool = False


# --------------------------------------------------------------------------- analysis


class WordOut(BaseModel):
    w: str
    s: int
    e: int
    p: float | None = None


class TranscriptSegmentOut(ORMModel):
    idx: int
    start_ms: int
    end_ms: int
    text: str
    speaker_id: uuid.UUID | None = None
    avg_logprob: float | None = None
    words: list[WordOut] | None = None


class TranscriptOut(BaseModel):
    id: uuid.UUID
    provider: str
    model: str
    language: str | None = None
    word_timestamps: bool = True
    confidence: float | None = None
    is_corrected: bool = False
    created_at: datetime
    duration_ms: int | None = None
    word_count: int = 0
    segments: list[TranscriptSegmentOut] = Field(default_factory=list)

    @classmethod
    def build(
        cls,
        transcript,  # noqa: ANN001
        segments,  # noqa: ANN001
        *,
        duration_ms: int | None = None,
        include_words: bool = False,
    ) -> TranscriptOut:
        return cls.model_validate(
            {
                "id": transcript.id,
                "provider": transcript.provider,
                "model": transcript.model,
                "language": transcript.language,
                "word_timestamps": transcript.word_timestamps,
                "confidence": float(transcript.confidence) if transcript.confidence else None,
                "is_corrected": transcript.is_corrected,
                "created_at": transcript.created_at,
                "duration_ms": duration_ms,
                "word_count": sum(
                    len(s.words or []) or len((s.text or "").split()) for s in segments
                ),
                "segments": [
                    TranscriptSegmentOut.model_validate(
                        {
                            "idx": s.idx,
                            "start_ms": s.start_ms,
                            "end_ms": s.end_ms,
                            "text": s.text,
                            "speaker_id": s.speaker_id,
                            "avg_logprob": (
                                float(s.avg_logprob) if s.avg_logprob is not None else None
                            ),
                            "words": s.words if include_words else None,
                        }
                    )
                    for s in segments
                ],
            }
        )


class CandidateScoreOut(ORMModel):
    weights_version: str
    hook: float
    content_value: float
    narrative: float
    standalone: float
    audience: float
    emotion: float
    visual_quality: float
    audio_quality: float
    ending_strength: float
    clarity: float
    content_potential: float
    technical_quality: float
    context_completeness: float
    audience_relevance: float
    overall: float


class MomentOut(BaseModel):
    id: uuid.UUID
    start_ms: int
    end_ms: int
    duration_ms: int
    category: str
    hook_text: str | None = None
    context_text: str | None = None
    payoff_text: str | None = None
    rationale: str | None = None
    standalone_ok: bool = False
    selected: bool = False
    suppressed_reason: str | None = None
    score: CandidateScoreOut | None = None

    @classmethod
    def build(cls, moment, score) -> MomentOut:  # noqa: ANN001
        return cls(
            id=moment.id,
            start_ms=moment.start_ms,
            end_ms=moment.end_ms,
            duration_ms=moment.end_ms - moment.start_ms,
            category=moment.category,
            hook_text=moment.hook_text,
            context_text=moment.context_text,
            payoff_text=moment.payoff_text,
            rationale=moment.rationale,
            standalone_ok=moment.standalone_ok,
            selected=moment.selected,
            suppressed_reason=moment.suppressed_reason,
            score=
            CandidateScoreOut.model_validate(score)
            if score is not None
            else None,
        )


class MomentPage(BaseModel):
    items: list[MomentOut]
    next_cursor: str | None = None
    total: int
    selected_count: int
    disclaimer: str = (
        "Scores estimate content characteristics (hook strength, narrative completeness, "
        "clarity) from the transcript. They are not predictions of views, reach or revenue."
    )


class AnalysisStatusOut(BaseModel):
    project_id: uuid.UUID
    status: str
    has_transcript: bool = False
    transcript_id: uuid.UUID | None = None
    provider: str | None = None
    model: str | None = None
    language: str | None = None
    segment_count: int = 0
    word_count: int = 0
    candidate_count: int = 0
    selected_count: int = 0
    weights_version: str | None = None
    method: str | None = None
    asr_configured: bool = True
    asr_note: str | None = None
    # Set by POST /analyze: which background job is doing the work, so the caller can poll
    # GET /jobs/{job_id} rather than being told nothing and guessing.
    job_id: uuid.UUID | None = None
    queued: bool = False


# --------------------------------------------------------------------------- clips


class ClipVersionOut(BaseModel):
    id: uuid.UUID
    version_no: int
    source: str
    width: int | None = None
    height: int | None = None
    duration_ms: int | None = None
    bytes: int | None = None
    sha256: str | None = None
    video_url: str | None = None
    caption_url: str | None = None
    render_seconds: float | None = None
    rendered_at: datetime | None = None
    created_at: datetime

    @classmethod
    def build(cls, version, *, video_url=None, caption_url=None) -> ClipVersionOut:  # noqa: ANN001
        return cls(
            id=version.id,
            version_no=version.version_no,
            source=version.source,
            width=version.width,
            height=version.height,
            duration_ms=version.duration_ms,
            bytes=version.bytes,
            sha256=version.sha256.hex() if version.sha256 else None,
            video_url=video_url,
            caption_url=caption_url,
            render_seconds=float(version.render_seconds) if version.render_seconds else None,
            rendered_at=version.rendered_at,
            created_at=version.created_at,
        )


class ClipOut(ORMModel):
    id: uuid.UUID
    project_id: uuid.UUID
    title: str
    state: str
    start_ms: int
    end_ms: int
    duration_ms: int
    layout: str
    template_key: str
    publishable: bool
    candidate_moment_id: uuid.UUID | None = None
    current_version_id: uuid.UUID | None = None
    metadata: dict = Field(default_factory=dict, validation_alias=AliasChoices("meta", "metadata"),
                           serialization_alias="metadata")
    rejection_reason: str | None = None
    created_at: datetime | None = None

    model_config = ConfigDict(populate_by_name=True, from_attributes=True)


class ClipPage(BaseModel):
    items: list[ClipOut]
    next_cursor: str | None = None


class ClipGenerationOut(BaseModel):
    created: int
    existing: int
    clip_ids: list[uuid.UUID]
    candidates_selected: int


class ClipRenderOut(BaseModel):
    clip_id: uuid.UUID
    version_id: uuid.UUID
    version_no: int
    state: str
    width: int
    height: int
    duration_ms: int
    bytes: int
    has_captions: bool
    cue_count: int
    render_seconds: float


class CaptionCueOut(BaseModel):
    start_ms: int
    end_ms: int
    text: str
    word_count: int


class CaptionsOut(BaseModel):
    language: str | None = None
    style_key: str | None = None
    source: str
    cue_count: int
    coverage: float
    words_in_range: int
    words_captioned: int
    words_without_timing: int
    truncated_at_clip_end: int
    warnings: list[str] = []
    cues: list[CaptionCueOut]
    vtt: str
