-- Run as the database administrator. Supply a generated password out of band.
-- The runner can claim queued work, read the approved plan, append run history,
-- and prune ephemeral payloads. It cannot read memory, auth, or provider tables.
CREATE ROLE chitti_runner LOGIN PASSWORD 'REPLACE_WITH_A_RANDOM_SECRET';

GRANT CONNECT ON DATABASE chitti TO chitti_runner;
GRANT USAGE ON SCHEMA public TO chitti_runner;
GRANT SELECT ON plan_revisions, plan_approvals, worker_runs,
    worker_run_events, worker_retention_policy, worker_artifact_payloads TO chitti_runner;
GRANT INSERT ON worker_run_events, worker_operations, worker_artifacts,
    worker_artifact_payloads TO chitti_runner;
GRANT DELETE ON worker_artifact_payloads TO chitti_runner;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO chitti_runner;
