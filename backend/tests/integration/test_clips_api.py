"""End-to-end clip tests: generate, caption, render, and every guard around them.

Rendering is real: real FFmpeg, real libass, real source file. The assertions check the produced
*bytes* where they can (dimensions via ffprobe, presence of an audio stream) rather than trusting
the plan, because a plan that was honoured in name only is exactly the failure worth catching.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from tests.integration.conftest import Actor

MEDIA = Path(__file__).resolve().parents[1] / "fixtures" / "generated"
SPEECH = MEDIA / "real_speech.mp4"
RIGHTS = "I recorded this footage myself and own all rights to it."

pytestmark = pytest.mark.integration


def need_speech() -> Path:
    if not SPEECH.exists():
        pytest.skip("speech fixture missing; run ops/scripts/make_speech_fixture.sh")
    return SPEECH


@pytest.fixture
def ready_project(alice: Actor) -> str:
    """A project that has been ingested, analysed, and has selected moments."""
    project_id = alice.client.post(
        f"/api/v1/workspaces/{alice.workspace_id}/projects",
        json={"name": "ClipSource", "clips_requested": 3},
        headers=alice.headers,
    ).json()["id"]
    with need_speech().open("rb") as fh:
        resp = alice.client.post(
            f"/api/v1/workspaces/{alice.workspace_id}/projects/{project_id}/sources",
            files={"file": ("real_speech.mp4", fh, "video/mp4")},
            data={"rights_basis": "original", "rights_statement": RIGHTS},
            headers=alice.headers,
        )
    assert resp.status_code == 201, resp.text
    analyse = alice.client.post(
        f"/api/v1/workspaces/{alice.workspace_id}/projects/{project_id}/analyze",
        headers=alice.headers,
    )
    assert analyse.status_code == 202, analyse.text
    return project_id


def generate(actor: Actor, project_id: str):
    return actor.client.post(
        f"/api/v1/workspaces/{actor.workspace_id}/projects/{project_id}/generate-clips",
        headers=actor.headers,
    )


def list_clips(actor: Actor, project_id: str) -> list[dict]:
    resp = actor.client.get(
        f"/api/v1/workspaces/{actor.workspace_id}/projects/{project_id}/clips",
        headers=actor.headers,
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["items"]


class TestGeneration:
    def test_generates_one_clip_per_selected_moment(self, alice: Actor, ready_project: str):
        resp = generate(alice, ready_project)
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["created"] >= 1
        assert body["candidates_selected"] >= 1

        clips = list_clips(alice, ready_project)
        assert len(clips) == body["created"]
        clip = clips[0]
        assert clip["state"] == "DRAFT"
        assert clip["duration_ms"] == clip["end_ms"] - clip["start_ms"]
        assert clip["title"], "a clip must have a label derived from its moment"

    def test_generation_is_idempotent(self, alice: Actor, ready_project: str):
        first = generate(alice, ready_project).json()
        second = generate(alice, ready_project).json()
        assert second["created"] == 0
        assert second["existing"] == first["created"]
        assert len(list_clips(alice, ready_project)) == first["created"]

    def test_generation_without_analysis_is_refused(self, alice: Actor):
        project_id = alice.client.post(
            f"/api/v1/workspaces/{alice.workspace_id}/projects",
            json={"name": "Unanalysed", "clips_requested": 1},
            headers=alice.headers,
        ).json()["id"]
        with need_speech().open("rb") as fh:
            alice.client.post(
                f"/api/v1/workspaces/{alice.workspace_id}/projects/{project_id}/sources",
                files={"file": ("real_speech.mp4", fh, "video/mp4")},
                data={"rights_basis": "original", "rights_statement": RIGHTS},
                headers=alice.headers,
            )
        resp = generate(alice, project_id)
        assert resp.status_code == 409
        assert resp.json()["code"] == "no_candidates"

    def test_generation_requires_csrf(self, alice: Actor, ready_project: str):
        resp = alice.client.post(
            f"/api/v1/workspaces/{alice.workspace_id}/projects/{ready_project}/generate-clips",
            headers={"X-CSRF-Token": "wrong-token"},
        )
        assert resp.status_code == 403


class TestCaptions:
    def test_captions_come_from_real_word_timings(self, alice: Actor, ready_project: str):
        generate(alice, ready_project)
        clip = list_clips(alice, ready_project)[0]
        resp = alice.client.get(
            f"/api/v1/workspaces/{alice.workspace_id}/clips/{clip['id']}/captions",
            headers=alice.headers,
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["source"] == "asr_word_timings"
        assert body["cue_count"] >= 1
        assert body["coverage"] == 1.0
        assert body["words_without_timing"] == 0
        assert body["cues"], "a clip with real speech must produce cues"

        # Cue text must be the words the model actually transcribed.
        joined = " ".join(c["text"] for c in body["cues"]).lower()
        assert ("learning to code" in joined) or ("started" in joined), joined[:200]

    def test_cue_times_are_clip_relative_and_inside_the_clip(
        self, alice: Actor, ready_project: str
    ):
        generate(alice, ready_project)
        clip = list_clips(alice, ready_project)[0]
        body = alice.client.get(
            f"/api/v1/workspaces/{alice.workspace_id}/clips/{clip['id']}/captions",
            headers=alice.headers,
        ).json()
        assert body["cues"][0]["start_ms"] == 0, "the first cue must start at the clip origin"
        for cue in body["cues"]:
            assert cue["end_ms"] > cue["start_ms"]
            assert cue["end_ms"] <= clip["duration_ms"]

    def test_vtt_is_served_and_parseable(self, alice: Actor, ready_project: str):
        generate(alice, ready_project)
        clip = list_clips(alice, ready_project)[0]
        body = alice.client.get(
            f"/api/v1/workspaces/{alice.workspace_id}/clips/{clip['id']}/captions",
            headers=alice.headers,
        ).json()
        vtt = body["vtt"]
        assert vtt.startswith("WEBVTT")
        assert "-->" in vtt

    def test_caption_word_budget_survives_the_round_trip(self, alice: Actor, ready_project: str):
        """The template's 4-word budget must reach the cue list, not be lost in the plumbing."""
        generate(alice, ready_project)
        clip = list_clips(alice, ready_project)[0]
        body = alice.client.get(
            f"/api/v1/workspaces/{alice.workspace_id}/clips/{clip['id']}/captions",
            headers=alice.headers,
        ).json()
        over = [c["word_count"] for c in body["cues"] if c["word_count"] > 4]
        assert not over, f"cues exceeded the 4-word budget: {over}"


class TestRendering:
    def test_render_produces_a_vertical_clip_with_audio(self, alice: Actor, ready_project: str):
        generate(alice, ready_project)
        clip = list_clips(alice, ready_project)[0]
        resp = alice.client.post(
            f"/api/v1/workspaces/{alice.workspace_id}/clips/{clip['id']}/render",
            headers=alice.headers,
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["version_no"] == 1
        assert (body["width"], body["height"]) == (1080, 1920), "must be a vertical 9:16 canvas"
        assert body["has_captions"] is True
        assert body["cue_count"] >= 1
        assert body["bytes"] > 100_000, "a real clip is a substantial file"

    def test_rendered_file_really_is_1080x1920(self, alice: Actor, ready_project: str, tmp_path):
        """Verify the bytes, not the plan: a plan honoured in name only is the failure to catch."""
        generate(alice, ready_project)
        clip = list_clips(alice, ready_project)[0]
        alice.client.post(
            f"/api/v1/workspaces/{alice.workspace_id}/clips/{clip['id']}/render",
            headers=alice.headers,
        )
        from app.core.config import get_settings

        settings = get_settings()
        stored = list(
            Path(settings.local_storage_root).rglob("*.mp4")
        )
        rendered = [p for p in stored if "clip_video" in str(p)]
        assert rendered, "the rendered file must exist in storage"
        newest = max(rendered, key=lambda p: p.stat().st_mtime)

        probe = subprocess.run(
            [
                "ffprobe", "-v", "error", "-show_entries",
                "stream=codec_type,width,height", "-of", "json", str(newest),
            ],
            capture_output=True, text=True, check=False,
        )
        streams = json.loads(probe.stdout)["streams"]
        video = next(s for s in streams if s["codec_type"] == "video")
        audio = [s for s in streams if s["codec_type"] == "audio"]
        assert (video["width"], video["height"]) == (1080, 1920)
        assert audio, "a clip with no audio is not a clip"

    def test_clip_becomes_reviewable_after_render(self, alice: Actor, ready_project: str):
        generate(alice, ready_project)
        clip = list_clips(alice, ready_project)[0]
        alice.client.post(
            f"/api/v1/workspaces/{alice.workspace_id}/clips/{clip['id']}/render",
            headers=alice.headers,
        )
        after = alice.client.get(
            f"/api/v1/workspaces/{alice.workspace_id}/clips/{clip['id']}", headers=alice.headers
        ).json()
        assert after["state"] == "QA_PASSED"
        assert after["publishable"] is True
        assert after["current_version_id"] is not None
        assert after["metadata"]["captions"]["cue_count"] >= 1

    def test_render_plan_is_retrievable_and_explains_the_file(
        self, alice: Actor, ready_project: str
    ):
        generate(alice, ready_project)
        clip = list_clips(alice, ready_project)[0]
        alice.client.post(
            f"/api/v1/workspaces/{alice.workspace_id}/clips/{clip['id']}/render",
            headers=alice.headers,
        )
        resp = alice.client.get(
            f"/api/v1/workspaces/{alice.workspace_id}/clips/{clip['id']}/render-plan",
            headers=alice.headers,
        )
        assert resp.status_code == 200, resp.text
        plan = resp.json()["render_plan"]
        assert plan["canvas"] == {"width": 1080, "height": 1920, "fps": 30}
        assert plan["window"]["duration_ms"] == clip["duration_ms"]
        assert plan["encoder"]["codec"] == "libx264"
        assert "adjustments" in plan

    def test_plan_is_unavailable_before_rendering(self, alice: Actor, ready_project: str):
        generate(alice, ready_project)
        clip = list_clips(alice, ready_project)[0]
        resp = alice.client.get(
            f"/api/v1/workspaces/{alice.workspace_id}/clips/{clip['id']}/render-plan",
            headers=alice.headers,
        )
        assert resp.status_code == 409
        assert resp.json()["code"] == "clip_not_rendered"

    def test_versions_are_append_only(self, alice: Actor, ready_project: str):
        generate(alice, ready_project)
        clip = list_clips(alice, ready_project)[0]
        url = f"/api/v1/workspaces/{alice.workspace_id}/clips/{clip['id']}/render"
        assert alice.client.post(url, headers=alice.headers).status_code == 201

        # A reviewed clip must not be silently re-rendered over the top.
        assert alice.client.post(url, headers=alice.headers).status_code == 409

        versions = alice.client.get(
            f"/api/v1/workspaces/{alice.workspace_id}/clips/{clip['id']}/versions",
            headers=alice.headers,
        ).json()
        assert len(versions) == 1
        assert versions[0]["sha256"], "a version must record the hash of what it produced"

    def test_download_is_honest_about_local_storage(self, alice: Actor, ready_project: str):
        """The local backend cannot mint a signed URL and must not pretend to."""
        generate(alice, ready_project)
        clip = list_clips(alice, ready_project)[0]
        alice.client.post(
            f"/api/v1/workspaces/{alice.workspace_id}/clips/{clip['id']}/render",
            headers=alice.headers,
        )
        body = alice.client.get(
            f"/api/v1/workspaces/{alice.workspace_id}/clips/{clip['id']}/download",
            headers=alice.headers,
        ).json()
        assert body["storage_key"]
        assert body["download_url"] is None
        assert "no signed URL" in body["note"]


class TestBoundaryNudge:
    def test_a_cut_inside_a_word_is_moved_and_recorded(self, alice: Actor, ready_project: str):
        """The fixture's real moment happens to start on a clean boundary, so the nudge path is
        exercised by moving the stored window into the middle of a spoken word first."""
        generate(alice, ready_project)
        clip = list_clips(alice, ready_project)[0]
        original_start, original_end = clip["start_ms"], clip["end_ms"]

        from sqlalchemy import update

        from app.db.models import Clip
        from app.db.session import session_scope

        # 40 ms in lands mid-word ("When" spans 7880-8000 in the fixture's transcript); 120 ms
        # would land exactly on the next word's start and be a clean cut by accident.
        # `ck_clip_dur` requires duration_ms = end_ms - start_ms, so move all three together.
        moved_start = original_start + 40
        with session_scope() as db:
            db.execute(
                update(Clip)
                .where(Clip.id == clip["id"])
                .values(
                    start_ms=moved_start,
                    end_ms=original_end,
                    duration_ms=original_end - moved_start,
                )
            )

        resp = alice.client.post(
            f"/api/v1/workspaces/{alice.workspace_id}/clips/{clip['id']}/render",
            headers=alice.headers,
        )
        assert resp.status_code == 201, resp.text

        plan = alice.client.get(
            f"/api/v1/workspaces/{alice.workspace_id}/clips/{clip['id']}/render-plan",
            headers=alice.headers,
        ).json()["render_plan"]
        assert plan["window"]["start_ms"] != moved_start, "the cut must not sit inside a word"
        assert plan["adjustments"]["start_delta_ms"] == -40
        assert plan["adjustments"]["reason"], "a move must be explained, never silent"
        # The word must now be whole on the picture as well as in the plan.
        assert plan["window"]["start_ms"] % 40 == 0

    def test_captions_follow_the_nudged_window(self, alice: Actor, ready_project: str):
        """Captions and picture must describe the same interval: no cue may start before frame 0."""
        generate(alice, ready_project)
        clip = list_clips(alice, ready_project)[0]

        from sqlalchemy import update

        from app.db.models import Clip
        from app.db.session import session_scope

        nudged_start = clip["start_ms"] + 40
        with session_scope() as db:
            db.execute(
                update(Clip)
                .where(Clip.id == clip["id"])
                .values(
                    start_ms=nudged_start,
                    duration_ms=clip["end_ms"] - nudged_start,
                )
            )

        body = alice.client.get(
            f"/api/v1/workspaces/{alice.workspace_id}/clips/{clip['id']}/captions",
            headers=alice.headers,
        ).json()
        assert body["cues"][0]["start_ms"] == 0


class TestClipAuthorization:
    def test_bob_cannot_see_or_render_alices_clips(
        self, alice: Actor, bob: Actor, ready_project: str
    ):
        generate(alice, ready_project)
        clip = list_clips(alice, ready_project)[0]

        assert bob.client.get(
            f"/api/v1/workspaces/{alice.workspace_id}/clips/{clip['id']}", headers=bob.headers
        ).status_code == 404
        assert bob.client.get(
            f"/api/v1/workspaces/{alice.workspace_id}/projects/{ready_project}/clips",
            headers=bob.headers,
        ).status_code == 404
        assert bob.client.post(
            f"/api/v1/workspaces/{alice.workspace_id}/clips/{clip['id']}/render",
            headers=bob.headers,
        ).status_code == 404
        assert bob.client.get(
            f"/api/v1/workspaces/{alice.workspace_id}/clips/{clip['id']}/captions",
            headers=bob.headers,
        ).status_code == 404

    def test_anonymous_is_rejected(self, alice: Actor, ready_project: str):
        generate(alice, ready_project)
        clip = list_clips(alice, ready_project)[0]
        from tests.integration.conftest import new_client

        anon = new_client(alice.client.app)
        resp = anon.get(f"/api/v1/workspaces/{alice.workspace_id}/clips/{clip['id']}")
        assert resp.status_code == 401

    def test_render_requires_csrf(self, alice: Actor, ready_project: str):
        generate(alice, ready_project)
        clip = list_clips(alice, ready_project)[0]
        resp = alice.client.post(
            f"/api/v1/workspaces/{alice.workspace_id}/clips/{clip['id']}/render",
            headers={"X-CSRF-Token": "wrong"},
        )
        assert resp.status_code == 403
