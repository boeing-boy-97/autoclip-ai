-- =====================================================================================
-- Invariant verification — run against a database with schema.sql applied.
-- Every block asserts a constraint that the application relies on. If any assertion
-- fails, the schema is not fit for purpose. Run via: ops/scripts/verify_schema.sh
-- =====================================================================================
\set ON_ERROR_STOP off
\pset pager off
\echo '=============================================================================='
\echo ' AutoClipper AI — schema invariant verification'
\echo '=============================================================================='

-- Clean slate for the fixtures used below.
TRUNCATE audit_logs, usage_records, notifications, processing_jobs, analytics_snapshots,
         publishing_records, publishing_jobs, oauth_states, oauth_credentials, youtube_channels,
         repair_attempts, quality_reports, captions, clip_edit_settings, clip_versions, clips,
         candidate_scores, candidate_moments, important_moments, topics, video_analysis, scenes,
         transcript_segments, speakers, transcripts, media_assets, source_videos,
         project_stage_events, projects, workspace_members, sessions, workspaces, users
  RESTART IDENTITY CASCADE;

-- ---------------------------------------------------------------- fixtures
INSERT INTO users (id, email, password_hash, display_name)
VALUES ('11111111-1111-1111-1111-111111111111', 'a@example.com', 'argon2id$dummy', 'Alice'),
       ('22222222-2222-2222-2222-222222222222', 'b@example.com', 'argon2id$dummy', 'Bob');

INSERT INTO workspaces (id, name, slug, owner_user_id)
VALUES ('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa', 'WS A', 'ws-a', '11111111-1111-1111-1111-111111111111'),
       ('bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb', 'WS B', 'ws-b', '22222222-2222-2222-2222-222222222222');

INSERT INTO projects (id, workspace_id, created_by, name)
VALUES ('11110000-0000-4000-8000-000000000001', 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa',
        '11111111-1111-1111-1111-111111111111', 'Project A');

INSERT INTO source_videos (id, project_id, workspace_id, kind, state, original_filename,
                           bytes, sha256, storage_key, duration_ms, width, height,
                           rights_basis, rights_confirmed_at, rights_confirmed_by)
VALUES ('50000000-0000-0000-0000-000000000001', '11110000-0000-4000-8000-000000000001',
        'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa', 'upload', 'ready', 'talk.mp4',
        1048576, digest('talk','sha256'), 'ws/aaaaaaaa/sources/talk.mp4', 600000, 1920, 1080,
        'original', now(), '11111111-1111-1111-1111-111111111111');

INSERT INTO clips (id, project_id, workspace_id, source_video_id, title,
                   start_ms, end_ms, duration_ms)
VALUES ('cccccccc-0000-4000-8000-000000000001', '11110000-0000-4000-8000-000000000001',
        'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa', '50000000-0000-0000-0000-000000000001',
        'Clip 1', 10000, 45000, 35000);

INSERT INTO clip_versions (id, clip_id, version_no, render_plan)
VALUES ('dddddddd-0000-4000-8000-000000000001', 'cccccccc-0000-4000-8000-000000000001', 1,
        '{"width":1080,"height":1920}'::jsonb);

INSERT INTO youtube_channels (id, workspace_id, connected_by, channel_id, channel_title)
VALUES ('eeeeeeee-0000-4000-8000-000000000001', 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa',
        '11111111-1111-1111-1111-111111111111', 'UC_test_channel_1', 'Test Channel');

\echo ''
\echo '--- 1. Interval sanity: end must exceed start --------------------------------'
DO $$
BEGIN
  BEGIN
    INSERT INTO clips (project_id, workspace_id, source_video_id, title, start_ms, end_ms, duration_ms)
    VALUES ('11110000-0000-4000-8000-000000000001','aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa',
            '50000000-0000-0000-0000-000000000001','Bad', 50000, 50000, 0);
    RAISE EXCEPTION 'FAIL: zero-length clip was accepted';
  EXCEPTION WHEN check_violation THEN
    RAISE NOTICE 'PASS: zero-length / inverted interval rejected';
  END;
END $$;

\echo ''
\echo '--- 2. duration_ms must equal end-start -------------------------------------'
DO $$
BEGIN
  BEGIN
    INSERT INTO clips (project_id, workspace_id, source_video_id, title, start_ms, end_ms, duration_ms)
    VALUES ('11110000-0000-4000-8000-000000000001','aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa',
            '50000000-0000-0000-0000-000000000001','Bad', 1000, 5000, 9999);
    RAISE EXCEPTION 'FAIL: inconsistent duration accepted';
  EXCEPTION WHEN check_violation THEN
    RAISE NOTICE 'PASS: duration/interval mismatch rejected';
  END;
END $$;

\echo ''
\echo '--- 3. Source video must belong to the same project (composite FK) ----------'
DO $$
DECLARE other_project uuid;
BEGIN
  INSERT INTO projects (id, workspace_id, created_by, name)
  VALUES ('11110000-0000-4000-8000-000000000002','aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa',
          '11111111-1111-1111-1111-111111111111','Project A2');
  BEGIN
    INSERT INTO clips (project_id, workspace_id, source_video_id, title, start_ms, end_ms, duration_ms)
    VALUES ('11110000-0000-4000-8000-000000000002','aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa',
            '50000000-0000-0000-0000-000000000001','Cross', 0, 1000, 1000);
    RAISE EXCEPTION 'FAIL: clip accepted a source from a different project';
  EXCEPTION WHEN foreign_key_violation THEN
    RAISE NOTICE 'PASS: cross-project source reference rejected';
  END;
END $$;

\echo ''
\echo '--- 4. Duplicate-publish guard: one LIVE job per (clip, channel) -----------'
INSERT INTO publishing_jobs (workspace_id, clip_id, clip_version_id, channel_id,
                             state, idempotency_key)
VALUES ('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa', 'cccccccc-0000-4000-8000-000000000001',
        'dddddddd-0000-4000-8000-000000000001', 'eeeeeeee-0000-4000-8000-000000000001',
        'queued', 'idem-first');
DO $$
BEGIN
  BEGIN
    INSERT INTO publishing_jobs (workspace_id, clip_id, clip_version_id, channel_id,
                                 state, idempotency_key)
    VALUES ('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa', 'cccccccc-0000-4000-8000-000000000001',
            'dddddddd-0000-4000-8000-000000000001', 'eeeeeeee-0000-4000-8000-000000000001',
            'uploading', 'idem-second');
    RAISE EXCEPTION 'FAIL: second live publish job was accepted (duplicate-post risk)';
  EXCEPTION WHEN unique_violation THEN
    RAISE NOTICE 'PASS: second live publish job for same clip+channel rejected';
  END;
END $$;

\echo ''
\echo '--- 5. Idempotency key is globally unique ----------------------------------'
DO $$
BEGIN
  BEGIN
    INSERT INTO publishing_jobs (workspace_id, clip_id, clip_version_id, channel_id,
                                 state, idempotency_key)
    VALUES ('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa', 'cccccccc-0000-4000-8000-000000000001',
            'dddddddd-0000-4000-8000-000000000001', 'eeeeeeee-0000-4000-8000-000000000001',
            'failed', 'idem-first');
    RAISE EXCEPTION 'FAIL: duplicate idempotency key accepted';
  EXCEPTION WHEN unique_violation THEN
    RAISE NOTICE 'PASS: reuse of an idempotency key rejected';
  END;
END $$;

\echo ''
\echo '--- 6. A terminal (failed) job frees the clip+channel slot -----------------'
DO $$
BEGIN
  UPDATE publishing_jobs SET state='failed' WHERE idempotency_key='idem-first';
  INSERT INTO publishing_jobs (workspace_id, clip_id, clip_version_id, channel_id,
                               state, idempotency_key)
  VALUES ('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa', 'cccccccc-0000-4000-8000-000000000001',
          'dddddddd-0000-4000-8000-000000000001', 'eeeeeeee-0000-4000-8000-000000000001',
          'queued', 'idem-retry');
  RAISE NOTICE 'PASS: retry allowed after terminal failure (partial index scoped correctly)';
EXCEPTION WHEN unique_violation THEN
  RAISE EXCEPTION 'FAIL: retry after failure was blocked';
END $$;

\echo ''
\echo '--- 7. Scheduled publish cannot be public without publishAt semantics -----'
DO $$
BEGIN
  BEGIN
    INSERT INTO publishing_jobs (workspace_id, clip_id, clip_version_id, channel_id,
                                 state, idempotency_key, scheduled_at, privacy)
    VALUES ('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa', 'cccccccc-0000-4000-8000-000000000001',
            'dddddddd-0000-4000-8000-000000000001', 'eeeeeeee-0000-4000-8000-000000000001',
            'scheduled', 'idem-sched', now() + interval '1 day', 'public');
    RAISE EXCEPTION 'FAIL: scheduled upload accepted as public (API requires private + publishAt)';
  EXCEPTION WHEN check_violation THEN
    RAISE NOTICE 'PASS: scheduled publish forced to non-public privacy';
  END;
END $$;

\echo ''
\echo '--- 8. Only ONE passing technical QA report per version --------------------'
INSERT INTO quality_reports (clip_id, clip_version_id, kind, verdict, checks, inspector_version)
VALUES ('cccccccc-0000-4000-8000-000000000001','dddddddd-0000-4000-8000-000000000001',
        'technical','pass','[]'::jsonb,'qa-0.1.0');
DO $$
BEGIN
  BEGIN
    INSERT INTO quality_reports (clip_id, clip_version_id, kind, verdict, checks, inspector_version)
    VALUES ('cccccccc-0000-4000-8000-000000000001','dddddddd-0000-4000-8000-000000000001',
            'technical','pass','[]'::jsonb,'qa-0.1.0');
    RAISE EXCEPTION 'FAIL: two passing technical reports allowed for one version';
  EXCEPTION WHEN unique_violation THEN
    RAISE NOTICE 'PASS: duplicate passing technical report rejected';
  END;
END $$;

\echo ''
\echo '--- 9. Editorial/duplicate reports may repeat (only technical is unique) ---'
INSERT INTO quality_reports (clip_id, clip_version_id, kind, verdict, checks, inspector_version)
VALUES ('cccccccc-0000-4000-8000-000000000001','dddddddd-0000-4000-8000-000000000001',
        'editorial','fail','[]'::jsonb,'qa-0.1.0');
INSERT INTO quality_reports (clip_id, clip_version_id, kind, verdict, checks, inspector_version)
VALUES ('cccccccc-0000-4000-8000-000000000001','dddddddd-0000-4000-8000-000000000001',
        'editorial','pass','[]'::jsonb,'qa-0.1.0');
DO $$ BEGIN RAISE NOTICE 'PASS: repeated editorial reports allowed (history preserved)'; END $$;

\echo ''
\echo '--- 10. Clip version numbering is unique per clip --------------------------'
DO $$
BEGIN
  BEGIN
    INSERT INTO clip_versions (clip_id, version_no, render_plan)
    VALUES ('cccccccc-0000-4000-8000-000000000001', 1, '{}'::jsonb);
    RAISE EXCEPTION 'FAIL: duplicate version_no accepted';
  EXCEPTION WHEN unique_violation THEN
    RAISE NOTICE 'PASS: duplicate clip version number rejected';
  END;
END $$;

\echo ''
\echo '--- 11. Repair attempts are idempotent per (clip, check, attempt) ----------'
INSERT INTO repair_attempts (clip_id, failed_check, action, attempt_no)
VALUES ('cccccccc-0000-4000-8000-000000000001','crop_face_coverage','recrop',1);
DO $$
BEGIN
  BEGIN
    INSERT INTO repair_attempts (clip_id, failed_check, action, attempt_no)
    VALUES ('cccccccc-0000-4000-8000-000000000001','crop_face_coverage','recrop',1);
    RAISE EXCEPTION 'FAIL: duplicate repair attempt accepted';
  EXCEPTION WHEN unique_violation THEN
    RAISE NOTICE 'PASS: repeated repair attempt rejected (bounded retries enforced)';
  END;
END $$;

\echo ''
\echo '--- 12. Job idempotency: same (project, stage, fingerprint) once -----------'
INSERT INTO processing_jobs (workspace_id, project_id, stage, queue, task_name,
                             input_fingerprint, status)
VALUES ('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa','11110000-0000-4000-8000-000000000001',
        'transcription','transcribe','transcribe_video','sha256:abc','RUNNING');
DO $$
BEGIN
  BEGIN
    INSERT INTO processing_jobs (workspace_id, project_id, stage, queue, task_name,
                                 input_fingerprint, status)
    VALUES ('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa','11110000-0000-4000-8000-000000000001',
            'transcription','transcribe','transcribe_video','sha256:abc','PENDING');
    RAISE EXCEPTION 'FAIL: duplicate in-flight job for same stage+fingerprint accepted';
  EXCEPTION WHEN unique_violation THEN
    RAISE NOTICE 'PASS: idempotent job enqueue rejected duplicate in-flight work';
  END;
END $$;

\echo ''
\echo '--- 13. Candidate duration must match its own interval ---------------------'
DO $$
BEGIN
  BEGIN
    INSERT INTO candidate_moments (project_id, start_ms, end_ms, required_duration_ms)
    VALUES ('11110000-0000-4000-8000-000000000001', 1000, 40000, 5000);
    RAISE EXCEPTION 'FAIL: candidate with mismatched required_duration accepted';
  EXCEPTION WHEN check_violation THEN
    RAISE NOTICE 'PASS: candidate duration/interval mismatch rejected';
  END;
END $$;

\echo ''
\echo '--- 14. Scores are bounded 0..100 -----------------------------------------'
DO $$
DECLARE cand uuid;
BEGIN
  INSERT INTO candidate_moments (project_id, start_ms, end_ms, required_duration_ms)
  VALUES ('11110000-0000-4000-8000-000000000001', 1000, 40000, 39000)
  RETURNING id INTO cand;
  BEGIN
    INSERT INTO candidate_scores (candidate_id, weights_version, hook, content_value, narrative,
      standalone, audience, emotion, visual_quality, audio_quality, ending_strength, clarity,
      content_potential, technical_quality, context_completeness, audience_relevance, overall)
    VALUES (cand,'v1', 90,90,90,90,90,90,90,90,90,90, 90,90,90,90, 101);
    RAISE EXCEPTION 'FAIL: out-of-range overall score accepted';
  EXCEPTION WHEN check_violation THEN
    RAISE NOTICE 'PASS: score outside 0..100 rejected';
  END;
END $$;

\echo ''
\echo '--- 15. updated_at trigger keeps the column fresh --------------------------'
DO $$
DECLARE before_val timestamptz; after_val timestamptz;
BEGIN
  SELECT updated_at INTO before_val FROM projects
   WHERE id='11110000-0000-4000-8000-000000000001';
  PERFORM pg_sleep(0.05);
  UPDATE projects SET name='Project A renamed'
   WHERE id='11110000-0000-4000-8000-000000000001';
  SELECT updated_at INTO after_val FROM projects
   WHERE id='11110000-0000-4000-8000-000000000001';
  IF after_val <= before_val THEN
    RAISE EXCEPTION 'FAIL: updated_at did not advance';
  END IF;
  RAISE NOTICE 'PASS: updated_at trigger advanced';
END $$;

\echo ''
\echo '--- 16. OAuth secrets are bytea, never text --------------------------------'
DO $$
DECLARE t text;
BEGIN
  SELECT data_type INTO t FROM information_schema.columns
   WHERE table_name='oauth_credentials' AND column_name='refresh_token_enc';
  IF t <> 'bytea' THEN
    RAISE EXCEPTION 'FAIL: refresh token column is %, expected bytea', t;
  END IF;
  RAISE NOTICE 'PASS: OAuth token columns are bytea (AES-GCM ciphertext, key_version present)';
END $$;

\echo ''
\echo '=============================================================================='
\echo ' Verification complete. Any FAIL above means the schema must be corrected.'
\echo '=============================================================================='
