#!/usr/bin/env bash
set -Eeuo pipefail

INSTALL_DIR="${INSTALL_DIR:-/opt/chitti}"
REMOTE_BRANCH="${REMOTE_BRANCH:-main}"
REPOSITORY_URL="${REPOSITORY_URL:-https://github.com/Moksha89/chitti.git}"
RUNNER_ENV="${RUNNER_ENV:-/etc/chitti/worker-runner.env}"
RUNNER_UNIT="chitti-worker-runner.service"
fresh_checkout=0

if [[ ! -d "${INSTALL_DIR}/.git" ]]; then
  if [[ ! -f "${INSTALL_DIR}/.env" ]]; then
    echo "Missing application checkout and .env: ${INSTALL_DIR}" >&2
    exit 1
  fi
  git -C "${INSTALL_DIR}" init --quiet
  git -C "${INSTALL_DIR}" remote add origin "${REPOSITORY_URL}"
  git -C "${INSTALL_DIR}" add -A
  fresh_checkout=1
fi

cd "${INSTALL_DIR}"
if [[ "${DRY_RUN:-0}" == "1" ]]; then
  printf '%s\n' \
    "Would update ${INSTALL_DIR} to origin/${REMOTE_BRANCH}" \
    "Would apply migrations through the normal chitti startup path" \
    "Would build chitti-sandbox:latest" \
    "Would install and enable ${RUNNER_UNIT}" \
    "Would create or verify the runner-only database role" \
    "Would verify schema, privileges, and container network boundaries"
  exit 0
fi

if [[ "${fresh_checkout}" -eq 0 ]] &&
  [[ -n "$(git diff --stat)" || -n "$(git diff --cached --stat)" ]]; then
  echo "Application checkout has local changes; refusing to overwrite them." >&2
  exit 1
fi

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run this deployment as root." >&2
  exit 1
fi

if [[ ! -f .env ]]; then
  echo "Missing ${INSTALL_DIR}/.env; refusing to start the stack." >&2
  exit 1
fi

set -a
# shellcheck disable=SC1091
source .env
set +a

git fetch --quiet origin "${REMOTE_BRANCH}"
if [[ -n "$(git diff --cached --stat "origin/${REMOTE_BRANCH}" --)" ]]; then
  echo "Application checkout has local changes; refusing to overwrite them." >&2
  exit 1
fi
git checkout --quiet --detach "origin/${REMOTE_BRANCH}"

docker compose up -d --build
docker compose ps

for _ in {1..60}; do
  if docker compose ps --status running --services | grep -qx chitti; then
    break
  fi
  sleep 2
done
docker compose ps --status running --services | grep -qx chitti

docker build --quiet -t chitti-sandbox:latest sandbox >/dev/null

install -d -m 0755 /etc/chitti
umask 077

role_exists="$(
  docker compose exec -T postgres psql -X -qAt \
    -U "${POSTGRES_USER}" -d "${POSTGRES_DB}" \
    -c "SELECT 1 FROM pg_roles WHERE rolname = 'chitti_runner'"
)"

if [[ "${role_exists}" == "1" ]]; then
  if [[ ! -s "${RUNNER_ENV}" ]]; then
    echo "Runner role exists but ${RUNNER_ENV} is missing; refusing to rotate credentials." >&2
    exit 1
  fi
else
  runner_password="$(openssl rand -hex 32)"
  runner_env_tmp="$(mktemp /etc/chitti/worker-runner.env.XXXXXX)"
  runner_sql_tmp="$(mktemp /etc/chitti/runner-role.sql.XXXXXX)"
  trap 'rm -f "${runner_env_tmp:-}" "${runner_sql_tmp:-}"' EXIT

  printf 'DATABASE_URL=postgresql+asyncpg://chitti_runner:%s@127.0.0.1:5432/%s\n' \
    "${runner_password}" "${POSTGRES_DB}" >"${runner_env_tmp}"
  chmod 0600 "${runner_env_tmp}"

  sed "s/REPLACE_WITH_A_RANDOM_SECRET/${runner_password}/" \
    deploy/worker-runner/runner-role.sql >"${runner_sql_tmp}"
  chmod 0600 "${runner_sql_tmp}"
  docker compose exec -T postgres psql -X -v ON_ERROR_STOP=1 \
    -U "${POSTGRES_USER}" -d "${POSTGRES_DB}" <"${runner_sql_tmp}" >/dev/null
  install -o root -g root -m 0600 "${runner_env_tmp}" "${RUNNER_ENV}"
  rm -f "${runner_env_tmp}" "${runner_sql_tmp}"
  unset runner_password
  trap - EXIT
fi

install -o root -g root -m 0644 \
  deploy/worker-runner/chitti-worker-runner.service \
  "/etc/systemd/system/${RUNNER_UNIT}"
systemctl daemon-reload
systemctl enable --now "${RUNNER_UNIT}"

app_container="$(docker compose ps -q chitti)"
[[ -n "${app_container}" ]]
if docker inspect "${app_container}" \
  --format '{{range .Mounts}}{{if eq .Destination "/var/run/docker.sock"}}FAIL{{end}}{{end}}' |
  grep -q '^FAIL$'; then
    echo "Application container has Docker socket access." >&2
    exit 1
fi
docker exec "${app_container}" test ! -S /var/run/docker.sock

worker_targets=""
for service_port in postgres:5432 redis:6379 litellm:4000 caddy:80; do
  service="${service_port%%:*}"
  port="${service_port##*:}"
  container="$(docker compose ps -q "${service}")"
  [[ -n "${container}" ]]
  address="$(
    docker inspect "${container}" --format \
      '{{range .NetworkSettings.Networks}}{{if .IPAddress}}{{.IPAddress}}{{end}}{{end}}'
  )"
  [[ -n "${address}" ]]
  worker_targets+="${service}|${address}|${port};"
done
docker run --rm --network bridge -e "WORKER_TARGETS=${worker_targets}" \
  chitti-sandbox:latest python3 -c '
import os
import socket

for target in os.environ["WORKER_TARGETS"].split(";"):
    if not target:
        continue
    name, address, port = target.split("|")
    for host in (name, address):
        try:
            socket.create_connection((host, int(port)), timeout=0.5)
        except OSError:
            continue
        raise SystemExit(f"worker reached {name}:{port} via {host}")
'

docker compose exec -T postgres psql -X -v ON_ERROR_STOP=1 \
  -U "${POSTGRES_USER}" -d "${POSTGRES_DB}" <<'SQL' >/dev/null
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM alembic_version
    WHERE version_num = '0010_model_context_compaction'
  ) THEN
    RAISE EXCEPTION 'database is not at migration 0010_model_context_compaction';
  END IF;
END
$$;
DO $$
DECLARE
  required_table text;
BEGIN
  FOREACH required_table IN ARRAY ARRAY[
    'decisions', 'decision_forgets', 'plan_revisions', 'plan_approvals',
    'worker_runs', 'worker_run_events', 'worker_operations',
    'worker_artifacts', 'worker_model_calls'
  ] LOOP
    IF to_regclass('public.' || required_table) IS NULL THEN
      RAISE EXCEPTION 'missing required table %', required_table;
    END IF;
  END LOOP;
END
$$;
DO $$
DECLARE
  required_trigger text;
BEGIN
  FOREACH required_trigger IN ARRAY ARRAY[
    'decisions_append_only',
    'plan_revisions_immutable',
    'plan_task_events_immutable',
    'plan_approvals_immutable',
    'reject_worker_run_mutation_trigger',
    'reject_worker_event_mutation_trigger',
    'reject_worker_operation_mutation_trigger',
    'reject_worker_artifact_mutation_trigger',
    'reject_worker_model_call_mutation_trigger'
  ] LOOP
    IF NOT EXISTS (
      SELECT 1 FROM pg_trigger
      WHERE tgname = required_trigger
    ) THEN
      RAISE EXCEPTION 'missing required append-only trigger %', required_trigger;
    END IF;
  END LOOP;
END
$$;
SQL

runner_image="$(docker compose images -q chitti | head -n1)"
[[ -n "${runner_image}" ]]
docker run --rm --network host --env-file "${RUNNER_ENV}" \
  --entrypoint python "${runner_image}" -c '
import asyncio
import os
import asyncpg

async def main():
    conn = await asyncpg.connect(os.environ["DATABASE_URL"])
    current_user = await conn.fetchval("SELECT current_user")
    if current_user != "chitti_runner":
        raise SystemExit("runner role identity check failed")

    # Derived from the SQL paths in worker.py and runner.py, rather than
    # copied from runner-role.sql.
    reads = [
        "plan_revisions", "plan_approvals", "decisions", "decision_forgets",
        "worker_runs", "worker_run_events", "worker_operations",
        "worker_artifacts", "worker_retention_policy",
        "worker_artifact_payloads", "worker_model_calls",
    ]
    inserts = [
        "plan_task_events", "worker_run_events", "worker_operations",
        "worker_artifacts", "worker_artifact_payloads", "worker_model_calls",
    ]
    updates = ["worker_runs"]  # SELECT ... FOR UPDATE OF worker_runs in runner.py
    deletes = ["worker_artifact_payloads"]
    sequences = [
        "plan_task_events_id_seq", "worker_run_events_id_seq",
        "worker_operations_id_seq", "worker_artifacts_id_seq",
        "worker_model_calls_id_seq",
    ]

    async def require_table_privilege(table, privilege):
        allowed = await conn.fetchval(
            "SELECT has_table_privilege(current_user, $1, $2)", table, privilege
        )
        if not allowed:
            raise SystemExit(f"runner lacks {privilege} on {table}")

    async def require_sequence_privilege(sequence):
        allowed = await conn.fetchval(
            "SELECT has_sequence_privilege(current_user, $1, $$USAGE$$)",
            sequence,
        )
        if not allowed:
            raise SystemExit(f"runner lacks sequence usage on {sequence}")

    for table in reads:
        await require_table_privilege(table, "SELECT")
    for table in inserts:
        await require_table_privilege(table, "INSERT")
    for table in updates:
        await require_table_privilege(table, "UPDATE")
    for table in deletes:
        await require_table_privilege(table, "DELETE")
    for sequence in sequences:
        await require_sequence_privilege(sequence)

    negatives = [
        ("decisions", "INSERT"),
        ("worker_runs", "INSERT"),
    ]
    for table, privilege in negatives:
        allowed = await conn.fetchval(
            "SELECT has_table_privilege(current_user, $1, $2)", table, privilege
        )
        if allowed:
            raise SystemExit(f"runner unexpectedly has {privilege} on {table}")
    if await conn.fetchval(
        "SELECT has_sequence_privilege(current_user, $$worker_runs_id_seq$$, $$USAGE$$)"
    ):
        raise SystemExit("runner unexpectedly has sequence usage on worker_runs_id_seq")

    await conn.close()

asyncio.run(main())
'

systemctl is-enabled --quiet "${RUNNER_UNIT}"
systemctl is-active --quiet "${RUNNER_UNIT}"
echo "Deployment and post-deploy boundary checks completed."
