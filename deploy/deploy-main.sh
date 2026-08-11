#!/usr/bin/env bash
set -Eeuo pipefail
exec </dev/null

INSTALL_DIR="${INSTALL_DIR:-/opt/chitti}"
REMOTE_BRANCH="${REMOTE_BRANCH:-main}"
REPOSITORY_URL="${REPOSITORY_URL:-https://github.com/Moksha89/chitti.git}"
RUNNER_ENV="${RUNNER_ENV:-/etc/chitti/worker-runner.env}"
RUNNER_UNIT="chitti-worker-runner.service"
RUNNER_ROLE_SQL="${RUNNER_ROLE_SQL:-deploy/worker-runner/runner-role.sql}"
RUNNER_UNIT_SOURCE="${RUNNER_UNIT_SOURCE:-deploy/worker-runner/chitti-worker-runner.service}"
RUNNER_VENV_DIR="${RUNNER_VENV_DIR:-/opt/chitti-runner}"
RUNNER_PYTHON="${RUNNER_PYTHON:-/opt/chitti-runner/bin/python}"
DEPLOY_COMPLETION_MARKER="CHITTI_DEPLOY_COMPLETE"
fresh_clone=0
real_checkout=0
if [[ -e "${INSTALL_DIR}/.git" ]] &&
  git -c safe.directory="${INSTALL_DIR}" -C "${INSTALL_DIR}" rev-parse --verify HEAD >/dev/null 2>&1; then
  real_checkout=1
fi

if [[ "${DRY_RUN:-0}" == "1" ]]; then
  if [[ "${real_checkout}" -eq 1 ]]; then
    printf '%s\n' \
      "Would update ${INSTALL_DIR} to origin/${REMOTE_BRANCH}" \
      "Would apply migrations through the normal chitti startup path" \
      "Would build chitti-sandbox:latest" \
      "Would install and enable ${RUNNER_UNIT}" \
      "Would restart ${RUNNER_UNIT} and verify its loaded-code identity" \
      "Would create or verify the runner-only database role" \
      "Would verify schema, privileges, and container network boundaries"
  else
    printf '%s\n' \
      "Would clone origin/${REMOTE_BRANCH} into ${INSTALL_DIR}" \
      "Would retain the existing tree as a dated rollback directory" \
      "Would carry forward .env and projects content without printing .env" \
      "Would apply migrations through the normal chitti startup path" \
      "Would build chitti-sandbox:latest" \
      "Would install and enable ${RUNNER_UNIT}" \
      "Would restart ${RUNNER_UNIT} and verify its loaded-code identity" \
      "Would create or verify the runner-only database role" \
      "Would verify schema, privileges, and container network boundaries"
  fi
  echo "${DEPLOY_COMPLETION_MARKER}"
  exit 0
fi

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run this deployment as root." >&2
  exit 1
fi

if [[ "${real_checkout}" -eq 0 ]]; then
  if [[ ! -f "${INSTALL_DIR}/.env" ]]; then
    echo "Missing application artifact or .env: ${INSTALL_DIR}" >&2
    exit 1
  fi
  rollback_dir="${INSTALL_DIR%/*}/$(basename "${INSTALL_DIR}")-rollback-$(date -u +%Y%m%dT%H%M%SZ)"
  if [[ -e "${rollback_dir}" ]]; then
    echo "Rollback directory already exists: ${rollback_dir}" >&2
    exit 1
  fi
  clone_dir="${INSTALL_DIR%/*}/$(basename "${INSTALL_DIR}")-new-$(date -u +%Y%m%dT%H%M%SZ)"
  if [[ -e "${clone_dir}" ]]; then
    echo "Clone staging directory already exists: ${clone_dir}" >&2
    exit 1
  fi
  git clone --quiet --branch "${REMOTE_BRANCH}" "${REPOSITORY_URL}" "${clone_dir}"
  mv "${INSTALL_DIR}" "${rollback_dir}"
  mv "${clone_dir}" "${INSTALL_DIR}"
  install -o root -g root -m 0600 "${rollback_dir}/.env" "${INSTALL_DIR}/.env"
  install -d -o root -g root -m 0755 "${INSTALL_DIR}/projects"
  if [[ -d "${rollback_dir}/projects" ]]; then
    cp -a "${rollback_dir}/projects/." "${INSTALL_DIR}/projects/"
  fi
  rm -rf "${rollback_dir}/.git"
  rm -f \
    "${rollback_dir}/deploy/worker-runner/runner-role.sql" \
    "${rollback_dir}/deploy/worker-runner/chitti-worker-runner.service"
  fresh_clone=1
fi

cd "${INSTALL_DIR}"
if [[ "${fresh_clone}" -eq 0 ]] &&
  [[ -n "$(git -c safe.directory="${INSTALL_DIR}" diff --stat)" ||
    -n "$(git -c safe.directory="${INSTALL_DIR}" diff --cached --stat)" ]]; then
  echo "Application checkout has local changes; refusing to overwrite them." >&2
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

git -c safe.directory="${INSTALL_DIR}" fetch --quiet origin "${REMOTE_BRANCH}"
git -c safe.directory="${INSTALL_DIR}" checkout --quiet --detach "origin/${REMOTE_BRANCH}"

install -d -o root -g root -m 0750 \
  /var/lib/chitti-previews /var/lib/chitti-preview-staging

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
runner_image="chitti-chitti:latest"

# Recreate LiteLLM so it cannot retain a process or file-mounted config from
# an earlier checkout.
docker compose up -d --build --force-recreate litellm
docker compose ps

litellm_container="$(docker compose ps -q litellm)"
[[ -n "${litellm_container}" ]]
for _ in {1..60}; do
  if [[ "$(docker inspect -f '{{.State.Health.Status}}' "${litellm_container}")" == "healthy" ]]; then
    break
  fi
  sleep 2
done
if [[ "$(docker inspect -f '{{.State.Health.Status}}' "${litellm_container}")" != "healthy" ]]; then
  echo "LiteLLM did not become healthy before loaded-route assertion" >&2
  exit 1
fi
docker exec "${litellm_container}" test -s /app/litellm/config.yaml
if ! gateway_models="$(
  curl --fail --silent --show-error --max-time 15 \
    -H "Authorization: Bearer ${LITELLM_MASTER_KEY}" \
    "http://127.0.0.1:${LITELLM_PORT:-4000}/v1/models"
)"; then
  echo "gateway loaded-route assertion failed: gateway request was unreachable or failed" >&2
  exit 1
fi
gateway_assertion_error="$(mktemp)"
if missing_gateway_routes="$(
  docker run --rm \
    --env "CHITTI_GATEWAY_MODELS_JSON=${gateway_models}" \
    --entrypoint python "${runner_image}" -c \
    'import json
import os
import sys
from chitti.provider import REQUIRED_GATEWAY_ROUTES

try:
    payload = json.loads(os.environ["CHITTI_GATEWAY_MODELS_JSON"])
except (KeyError, json.JSONDecodeError, TypeError) as exc:
    print(f"invalid gateway JSON response: {exc}", file=sys.stderr)
    raise SystemExit(2)
model_ids = {
    str(item["id"])
    for item in payload.get("data", [])
    if isinstance(item, dict) and "id" in item
}
print("\n".join(sorted(REQUIRED_GATEWAY_ROUTES - model_ids)))' \
    2>"${gateway_assertion_error}"
)"; then
  :
else
  gateway_assertion_status="$?"
  if [[ "${gateway_assertion_status}" -eq 2 ]]; then
    echo "gateway loaded-route assertion failed: response was not valid JSON" >&2
  else
    echo "gateway loaded-route assertion failed: route checker could not execute" >&2
  fi
  cat "${gateway_assertion_error}" >&2
  rm -f "${gateway_assertion_error}"
  exit 1
fi
rm -f "${gateway_assertion_error}"
if [[ -n "${missing_gateway_routes}" ]]; then
  echo "gateway loaded-route assertion failed; missing: ${missing_gateway_routes}" >&2
  exit 1
fi
echo "Gateway loaded-route assertions passed."

caddy_container="$(docker compose ps -q caddy)"
[[ -n "${caddy_container}" ]]
caddy_loaded_config="$(
  docker exec "${caddy_container}" \
    wget -qO- http://127.0.0.1:2019/config/
)"
printf '%s' "${caddy_loaded_config}" | grep -q '"apps"'
curl --fail --silent --show-error --max-time 15 \
  "https://${DOMAIN:-localhost}/login" >/dev/null
echo "Caddy loaded configuration and served the login page."

if [[ ! -x "${RUNNER_PYTHON}" ]]; then
  if ! python3 -m venv "${RUNNER_VENV_DIR}"; then
    apt-get update --quiet
    DEBIAN_FRONTEND=noninteractive apt-get install --yes --quiet python3.12-venv
    python3 -m venv "${RUNNER_VENV_DIR}"
  fi
fi
"${RUNNER_PYTHON}" -m pip install --quiet --disable-pip-version-check \
  "asyncpg==0.30.0" \
  "httpx==0.28.1" \
  "pydantic-settings==2.8.1" \
  "SQLAlchemy[asyncio]==2.0.39"

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
  runner_sql_tmp="$(mktemp /etc/chitti/runner-role.XXXXXX)"
  trap 'rm -f "${runner_sql_tmp:-}"' EXIT
  sed "/^CREATE ROLE chitti_runner LOGIN PASSWORD /d" \
    "${RUNNER_ROLE_SQL}" >"${runner_sql_tmp}"
  chmod 0600 "${runner_sql_tmp}"
  docker compose exec -T postgres psql -X -v ON_ERROR_STOP=1 \
    -U "${POSTGRES_USER}" -d "${POSTGRES_DB}" <"${runner_sql_tmp}" >/dev/null
  rm -f "${runner_sql_tmp}"
  trap - EXIT
  runner_env_tmp="$(mktemp /etc/chitti/worker-runner.env.XXXXXX)"
  trap 'rm -f "${runner_env_tmp:-}"' EXIT
  sed \
    -e '/^LITELLM_BASE_URL=/d' \
    -e '/^LITELLM_MASTER_KEY=/d' \
    "${RUNNER_ENV}" >"${runner_env_tmp}"
  printf 'LITELLM_BASE_URL=http://127.0.0.1:%s\nLITELLM_MASTER_KEY=%s\n' \
    "${LITELLM_PORT:-4000}" "${LITELLM_MASTER_KEY}" >>"${runner_env_tmp}"
  chmod 0600 "${runner_env_tmp}"
  install -o root -g root -m 0600 "${runner_env_tmp}" "${RUNNER_ENV}"
  rm -f "${runner_env_tmp}"
  trap - EXIT
else
  runner_password="$(openssl rand -hex 32)"
  runner_env_tmp="$(mktemp /etc/chitti/worker-runner.env.XXXXXX)"
  runner_sql_tmp="$(mktemp /etc/chitti/runner-role.sql.XXXXXX)"
  trap 'rm -f "${runner_env_tmp:-}" "${runner_sql_tmp:-}"' EXIT

  printf 'DATABASE_URL=postgresql+asyncpg://chitti_runner:%s@127.0.0.1:5432/%s\nPREVIEW_ROOT=/var/lib/chitti-previews\nPREVIEW_STAGING_ROOT=/var/lib/chitti-preview-staging\nLITELLM_BASE_URL=http://127.0.0.1:%s\nLITELLM_MASTER_KEY=%s\n' \
    "${runner_password}" "${POSTGRES_DB}" "${LITELLM_PORT:-4000}" "${LITELLM_MASTER_KEY}" >"${runner_env_tmp}"
  chmod 0600 "${runner_env_tmp}"

  sed "s/REPLACE_WITH_A_RANDOM_SECRET/${runner_password}/" \
    "${RUNNER_ROLE_SQL}" >"${runner_sql_tmp}"
  chmod 0600 "${runner_sql_tmp}"
  docker compose exec -T postgres psql -X -v ON_ERROR_STOP=1 \
    -U "${POSTGRES_USER}" -d "${POSTGRES_DB}" <"${runner_sql_tmp}" >/dev/null
  install -o root -g root -m 0600 "${runner_env_tmp}" "${RUNNER_ENV}"
  rm -f "${runner_env_tmp}" "${runner_sql_tmp}"
  unset runner_password
  trap - EXIT
fi

install -o root -g root -m 0644 \
  "${RUNNER_UNIT_SOURCE}" \
  "/etc/systemd/system/${RUNNER_UNIT}"
systemctl daemon-reload
systemctl enable "${RUNNER_UNIT}"
expected_runner_digest="$(
  PYTHONPATH="${INSTALL_DIR}/app" CHITTI_CODE_IDENTITY_PATH=/run/chitti-worker/loaded-code.json \
    "${RUNNER_PYTHON}" -c \
    'from chitti.runtime_identity import loaded_code_digest; print(loaded_code_digest())'
)"
rm -f /run/chitti-worker/loaded-code.json
systemctl restart "${RUNNER_UNIT}"

for _ in {1..30}; do
  runner_pid="$(systemctl show --property=MainPID --value "${RUNNER_UNIT}")"
  if [[ "${runner_pid}" != "0" ]] &&
    [[ -s /run/chitti-worker/loaded-code.json ]]; then
    break
  fi
  sleep 1
done
runner_pid="$(systemctl show --property=MainPID --value "${RUNNER_UNIT}")"
if [[ "${runner_pid}" == "0" ]] || [[ ! -s /run/chitti-worker/loaded-code.json ]]; then
  echo "runner loaded-code identity was not produced after restart" >&2
  exit 1
fi
RUNNER_PID="${runner_pid}" EXPECTED_RUNNER_DIGEST="${expected_runner_digest}" \
  "${RUNNER_PYTHON}" - <<'PY'
import json
import os
from pathlib import Path

identity = json.loads(Path("/run/chitti-worker/loaded-code.json").read_text())
if str(identity.get("pid")) != os.environ["RUNNER_PID"]:
    raise SystemExit("runner loaded-code identity belongs to a different process")
if identity.get("digest") != os.environ["EXPECTED_RUNNER_DIGEST"]:
    raise SystemExit("runner loaded-code identity does not match deployed code")
PY
echo "Runner loaded-code identity assertion passed."

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

expected_migration="$(
  docker run --rm --entrypoint python "${runner_image}" -c '
from alembic.config import Config
from alembic.script import ScriptDirectory

heads = ScriptDirectory.from_config(Config("alembic.ini")).get_heads()
if len(heads) != 1:
    raise SystemExit(f"expected exactly one migration head, found: {heads}")
print(heads[0])
'
)"

live_migration="$(
  docker compose exec -T postgres psql -X -v ON_ERROR_STOP=1 -At \
    -U "${POSTGRES_USER}" -d "${POSTGRES_DB}" \
    -c "SELECT version_num FROM alembic_version"
)"
if [[ "${live_migration}" != "${expected_migration}" ]]; then
  echo \
    "database migration mismatch: live ${live_migration:-<empty>}, " \
    "expected ${expected_migration}" >&2
  exit 1
fi
docker compose exec -T postgres psql -X -v ON_ERROR_STOP=1 \
  -U "${POSTGRES_USER}" -d "${POSTGRES_DB}" <<'SQL' >/dev/null
DO $$
DECLARE
  required_table text;
BEGIN
  FOREACH required_table IN ARRAY ARRAY[
    'decisions', 'decision_forgets', 'plan_revisions', 'plan_approvals',
    'worker_runs', 'worker_run_events', 'worker_operations',
    'worker_artifacts', 'worker_model_calls', 'export_manifests',
    'promotion_approvals', 'previews'
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
    'reject_worker_model_call_mutation_trigger',
    'reject_export_manifest_mutation_trigger',
    'reject_promotion_approval_mutation_trigger',
    'reject_preview_mutation_trigger'
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

[[ -n "${runner_image}" ]]
docker run --rm --network host --env-file "${RUNNER_ENV}" \
  --entrypoint python "${runner_image}" -c '
import asyncio
import os
import asyncpg

async def main():
    database_url = os.environ["DATABASE_URL"].replace(
        "postgresql+asyncpg://", "postgresql://", 1
    )
    conn = await asyncpg.connect(database_url)
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
        "export_manifests", "promotion_approvals", "previews",
    ]
    inserts = [
        "plan_task_events", "worker_run_events", "worker_operations",
        "worker_artifacts", "worker_artifact_payloads", "worker_model_calls",
        "export_manifests", "previews",
    ]
    updates = ["worker_runs"]  # SELECT ... FOR UPDATE OF worker_runs in runner.py
    deletes = ["worker_artifact_payloads"]
    async def require_table_privilege(table, privilege):
        allowed = await conn.fetchval(
            "SELECT has_table_privilege(current_user, $1, $2)", table, privilege
        )
        if not allowed:
            raise SystemExit(f"runner lacks {privilege} on {table}")

    async def require_sequence_privilege(table):
        has_id = await conn.fetchval(
            "SELECT EXISTS ("
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_schema = $$public$$ AND table_name = $1 "
            "AND column_name = $$id$$"
            ")", table
        )
        if not has_id:
            return
        sequence = await conn.fetchval(
            "SELECT pg_get_serial_sequence($1, $$id$$)", table
        )
        if sequence is None:
            return
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
    for table in inserts:
        await require_sequence_privilege(table)

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
    worker_runs_sequence = await conn.fetchval(
        "SELECT pg_get_serial_sequence($1, $$id$$)", "worker_runs"
    )
    if worker_runs_sequence and await conn.fetchval(
        "SELECT has_sequence_privilege(current_user, $1, $$USAGE$$)",
        worker_runs_sequence,
    ):
        raise SystemExit(
            f"runner unexpectedly has sequence usage on {worker_runs_sequence}"
        )

    await conn.close()

asyncio.run(main())
'

systemctl is-enabled --quiet "${RUNNER_UNIT}"
systemctl is-active --quiet "${RUNNER_UNIT}"
echo "Deployment and post-deploy boundary checks completed."
echo "${DEPLOY_COMPLETION_MARKER}"
