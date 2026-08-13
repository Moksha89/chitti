-- The deployment script creates this login with a generated password in a
-- pipe, then applies these grants. The runner can claim queued work, read the
-- approved plan, append run history, and prune ephemeral payloads. It cannot
-- read memory, auth, or provider tables.
GRANT CONNECT ON DATABASE chitti TO chitti_runner;
GRANT USAGE ON SCHEMA public TO chitti_runner;
GRANT SELECT ON plan_revisions, plan_approvals, decisions, decision_forgets, worker_runs,
    worker_run_events, worker_operations, worker_artifacts,
    worker_operation_output_chunks,
    worker_run_heartbeats,
    worker_retention_policy, worker_artifact_payloads, worker_model_calls,
    export_manifests, promotion_approvals, previews,
    reminders, reminder_occurrences, notifications,
    notification_acknowledgements, daily_briefings, runner_health,
    brand_profiles
    TO chitti_runner;
GRANT INSERT ON plan_task_events, worker_run_events, worker_operations, worker_artifacts,
    worker_operation_output_chunks, worker_artifact_payloads, worker_model_calls,
    export_manifests, previews, reminder_occurrences, notifications,
    worker_run_heartbeats
    TO chitti_runner;
GRANT UPDATE, DELETE ON worker_run_heartbeats TO chitti_runner;
GRANT INSERT, UPDATE ON runner_health TO chitti_runner;
-- The runner uses SELECT ... FOR UPDATE OF worker_runs to claim a queued row.
GRANT UPDATE ON worker_runs TO chitti_runner;
GRANT DELETE ON worker_artifact_payloads TO chitti_runner;
GRANT DELETE ON worker_operation_output_chunks TO chitti_runner;
GRANT USAGE, SELECT ON SEQUENCE
    plan_task_events_id_seq,
    worker_run_events_id_seq,
    worker_operations_id_seq,
    worker_artifacts_id_seq,
    worker_operation_output_chunks_id_seq,
    worker_model_calls_id_seq,
    export_manifests_id_seq,
    reminder_occurrences_id_seq,
    notifications_id_seq
    TO chitti_runner;

-- Enforce the narrow boundary if this script is applied to an older role.
REVOKE INSERT ON worker_runs FROM chitti_runner;
REVOKE USAGE, SELECT ON SEQUENCE worker_runs_id_seq FROM chitti_runner;
