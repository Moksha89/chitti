-- The deployment script creates this login with a generated password in a
-- pipe, then applies these grants. The runner can claim queued work, read the
-- approved plan, append run history, and prune ephemeral payloads. It cannot
-- read memory, auth, or provider tables.
GRANT CONNECT ON DATABASE chitti TO chitti_runner;
GRANT USAGE ON SCHEMA public TO chitti_runner;
GRANT SELECT ON plan_revisions, plan_approvals, decisions, decision_forgets, worker_runs,
    worker_run_events, worker_operations, worker_artifacts,
    worker_retention_policy, worker_artifact_payloads, worker_model_calls,
    export_manifests, promotion_approvals, previews
    TO chitti_runner;
GRANT INSERT ON plan_task_events, worker_run_events, worker_operations, worker_artifacts,
    worker_artifact_payloads, worker_model_calls, export_manifests, previews
    TO chitti_runner;
-- The runner uses SELECT ... FOR UPDATE OF worker_runs to claim a queued row.
GRANT UPDATE ON worker_runs TO chitti_runner;
GRANT DELETE ON worker_artifact_payloads TO chitti_runner;
GRANT USAGE, SELECT ON SEQUENCE
    plan_task_events_id_seq,
    worker_run_events_id_seq,
    worker_operations_id_seq,
    worker_artifacts_id_seq,
    worker_model_calls_id_seq,
    export_manifests_id_seq,
    previews_id_seq
    TO chitti_runner;

-- Enforce the narrow boundary if this script is applied to an older role.
REVOKE INSERT ON worker_runs FROM chitti_runner;
REVOKE USAGE, SELECT ON SEQUENCE worker_runs_id_seq FROM chitti_runner;
