"""Project workflow state machine — single declarative source of truth.

Used by: Pydantic validation, the orchestrator, the API, and the unit tests. There is exactly one
definition of what transition is legal, so the database journal, the API guard and the resume
logic can never disagree.

Invariants encoded here
-----------------------
* Every stage belongs to the enum declared in the database (`project_status`).
* Illegal transitions raise; nothing is silently coerced.
* FAILED and CANCELLED are absorbing *for automation*: nothing advances them on its own. A
  project resumes by creating a new job, and the one edge that leaves these states
  (-> MEDIA_INSPECTION) exists solely so an explicit, user-requested retry can record the work it
  redoes. Without that edge a retry would re-run the pipeline while the journal silently skipped
  every stage, leaving a project marked FAILED with a fresh transcript under it.
* PUBLISHED is only reachable from PUBLISHING or SCHEDULED, and PUBLISHING only from APPROVED —
  so nothing can be published that was not explicitly approved.
"""

from __future__ import annotations

from enum import StrEnum


class ProjectStage(StrEnum):
    CREATED = "CREATED"
    SOURCE_VALIDATION = "SOURCE_VALIDATION"
    INGESTING = "INGESTING"
    MEDIA_INSPECTION = "MEDIA_INSPECTION"
    AUDIO_EXTRACTION = "AUDIO_EXTRACTION"
    TRANSCRIPTION = "TRANSCRIPTION"
    VISUAL_ANALYSIS = "VISUAL_ANALYSIS"
    SEMANTIC_ANALYSIS = "SEMANTIC_ANALYSIS"
    MOMENT_DETECTION = "MOMENT_DETECTION"
    CLIP_RANKING = "CLIP_RANKING"
    CLIP_GENERATION = "CLIP_GENERATION"
    VIDEO_RENDERING = "VIDEO_RENDERING"
    CAPTION_RENDERING = "CAPTION_RENDERING"
    AUDIO_ENHANCEMENT = "AUDIO_ENHANCEMENT"
    QUALITY_CHECK = "QUALITY_CHECK"
    AUTO_REPAIR = "AUTO_REPAIR"
    AWAITING_REVIEW = "AWAITING_REVIEW"
    APPROVED = "APPROVED"
    PUBLISHING = "PUBLISHING"
    SCHEDULED = "SCHEDULED"
    PUBLISHED = "PUBLISHED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


TERMINAL_STAGES: frozenset[ProjectStage] = frozenset(
    {ProjectStage.COMPLETED, ProjectStage.FAILED, ProjectStage.CANCELLED}
)

# The linear processing spine, in order. Resume logic walks this list.
PIPELINE_ORDER: tuple[ProjectStage, ...] = (
    ProjectStage.CREATED,
    ProjectStage.SOURCE_VALIDATION,
    ProjectStage.INGESTING,
    ProjectStage.MEDIA_INSPECTION,
    ProjectStage.AUDIO_EXTRACTION,
    ProjectStage.TRANSCRIPTION,
    ProjectStage.VISUAL_ANALYSIS,
    ProjectStage.SEMANTIC_ANALYSIS,
    ProjectStage.MOMENT_DETECTION,
    ProjectStage.CLIP_RANKING,
    ProjectStage.CLIP_GENERATION,
    ProjectStage.VIDEO_RENDERING,
    ProjectStage.CAPTION_RENDERING,
    ProjectStage.AUDIO_ENHANCEMENT,
    ProjectStage.QUALITY_CHECK,
    ProjectStage.AWAITING_REVIEW,
    ProjectStage.APPROVED,
    ProjectStage.PUBLISHING,
    ProjectStage.PUBLISHED,
    ProjectStage.COMPLETED,
)

STAGE_GRAPH: dict[ProjectStage, frozenset[ProjectStage]] = {
    ProjectStage.CREATED: frozenset({ProjectStage.SOURCE_VALIDATION}),
    ProjectStage.SOURCE_VALIDATION: frozenset({ProjectStage.INGESTING}),
    ProjectStage.INGESTING: frozenset({ProjectStage.MEDIA_INSPECTION}),
    ProjectStage.MEDIA_INSPECTION: frozenset({ProjectStage.AUDIO_EXTRACTION}),
    ProjectStage.AUDIO_EXTRACTION: frozenset({ProjectStage.TRANSCRIPTION}),
    ProjectStage.TRANSCRIPTION: frozenset({ProjectStage.VISUAL_ANALYSIS}),
    ProjectStage.VISUAL_ANALYSIS: frozenset({ProjectStage.SEMANTIC_ANALYSIS}),
    ProjectStage.SEMANTIC_ANALYSIS: frozenset({ProjectStage.MOMENT_DETECTION}),
    ProjectStage.MOMENT_DETECTION: frozenset({ProjectStage.CLIP_RANKING}),
    ProjectStage.CLIP_RANKING: frozenset({ProjectStage.CLIP_GENERATION}),
    ProjectStage.CLIP_GENERATION: frozenset({ProjectStage.VIDEO_RENDERING}),
    ProjectStage.VIDEO_RENDERING: frozenset({ProjectStage.CAPTION_RENDERING}),
    ProjectStage.CAPTION_RENDERING: frozenset({ProjectStage.AUDIO_ENHANCEMENT}),
    ProjectStage.AUDIO_ENHANCEMENT: frozenset({ProjectStage.QUALITY_CHECK}),
    # QA loops back into repair at most twice, or forwards for human review.
    ProjectStage.QUALITY_CHECK: frozenset({ProjectStage.AUTO_REPAIR, ProjectStage.AWAITING_REVIEW}),
    ProjectStage.AUTO_REPAIR: frozenset({ProjectStage.QUALITY_CHECK, ProjectStage.AWAITING_REVIEW}),
    ProjectStage.AWAITING_REVIEW: frozenset(
        {
            ProjectStage.APPROVED,
            ProjectStage.CLIP_GENERATION,  # reject/replace -> regenerate
            ProjectStage.COMPLETED,  # all clips rejected / nothing to publish
        }
    ),
    ProjectStage.APPROVED: frozenset({ProjectStage.PUBLISHING, ProjectStage.COMPLETED}),
    ProjectStage.PUBLISHING: frozenset(
        {
            ProjectStage.SCHEDULED,
            ProjectStage.PUBLISHED,
            ProjectStage.AWAITING_REVIEW,  # upload failed -> back to human
        }
    ),
    ProjectStage.SCHEDULED: frozenset({ProjectStage.PUBLISHED, ProjectStage.APPROVED}),
    ProjectStage.PUBLISHED: frozenset({ProjectStage.COMPLETED}),
    ProjectStage.COMPLETED: frozenset(),
    # Only an explicit retry takes these edges; no automated path advances out of a terminal state.
    ProjectStage.FAILED: frozenset({ProjectStage.MEDIA_INSPECTION}),
    ProjectStage.CANCELLED: frozenset({ProjectStage.MEDIA_INSPECTION}),
}

# Any non-terminal stage may fail or be cancelled.
for _stage in ProjectStage:
    if _stage not in TERMINAL_STAGES:
        STAGE_GRAPH[_stage] = STAGE_GRAPH[_stage] | {ProjectStage.FAILED, ProjectStage.CANCELLED}


class IllegalTransitionError(ValueError):
    def __init__(self, current: ProjectStage | str, target: ProjectStage | str) -> None:
        self.current = ProjectStage(current)
        self.target = ProjectStage(target)
        super().__init__(
            f"illegal project transition {self.current} -> {self.target}; "
            f"allowed: {sorted(s.value for s in STAGE_GRAPH[self.current]) or 'none (terminal)'}"
        )


def allowed_transitions(current: ProjectStage | str) -> frozenset[ProjectStage]:
    return STAGE_GRAPH[ProjectStage(current)]


def can_transition(current: ProjectStage | str, target: ProjectStage | str) -> bool:
    try:
        return ProjectStage(target) in allowed_transitions(current)
    except ValueError:  # unknown stage name
        return False


def assert_transition(current: ProjectStage | str, target: ProjectStage | str) -> None:
    """Raise IllegalTransitionError unless the move is legal. Callers translate to HTTP 409."""
    if not can_transition(current, target):
        raise IllegalTransitionError(current, target)


def is_terminal(stage: ProjectStage | str) -> bool:
    return ProjectStage(stage) in TERMINAL_STAGES


def next_stage(current: ProjectStage | str) -> ProjectStage | None:
    """Next stage on the linear spine, or None for branching/terminal stages.

    Branching stages (QUALITY_CHECK, AUTO_REPAIR, AWAITING_REVIEW, PUBLISHING, SCHEDULED) return
    None deliberately: choosing a branch requires evidence (QA results, user action), not order.
    """
    current = ProjectStage(current)
    if current not in PIPELINE_ORDER:
        return None
    idx = PIPELINE_ORDER.index(current)
    return None if idx + 1 >= len(PIPELINE_ORDER) else PIPELINE_ORDER[idx + 1]


def resume_stage(completed: set[ProjectStage | str]) -> ProjectStage:
    """First stage whose work is not yet done — the resume point after a failure.

    Walks the spine and returns the first stage not present in `completed`. If everything is
    done, returns COMPLETED. Stages off the spine are ignored here; branch points are resolved by
    the orchestrator with QA/user evidence.
    """
    done = {ProjectStage(s) for s in completed}
    for stage in PIPELINE_ORDER:
        if stage not in done:
            return stage
    return ProjectStage.COMPLETED


def progress_percent(stage: ProjectStage | str) -> int | None:
    """Coarse progress from spine position. None for branching stages with no denominator.

    This is a *stage-position* estimate, not a measured percentage. UI must present it as such;
    per-task progress (frames, bytes) is reported separately by the job itself.
    """
    stage = ProjectStage(stage)
    if stage in TERMINAL_STAGES or stage not in PIPELINE_ORDER:
        return None
    idx = PIPELINE_ORDER.index(stage)
    return round(100 * idx / (len(PIPELINE_ORDER) - 1))
