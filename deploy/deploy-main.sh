#!/usr/bin/env bash
set -Eeuo pipefail

INSTALL_DIR="${INSTALL_DIR:-/opt/chitti}"
REMOTE_BRANCH="${REMOTE_BRANCH:-main}"
RUNNER_ENV="${RUNNER_ENV:-/etc/chitti/worker-runner.env}"
RUNNER_UNIT="chitti-worker-runner.service"
COMPOSE_PROJECT="${COMPOSE_PROJECT:-chitti}"

if [[ ! -d "${INSTALL_DIR}/.git" ]]; then
  echo "Missing application checkout: ${INSTALL_DIR}" >&2
  exit 1
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

if [[ -n "$(git diff --stat)" || -n "$(git diff --cached --stat)" ]]; then
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

docker run --rm --network none chitti-sandbox:latest python3 -c '
import socket
targets = [("postgres", 5432), ("redis", 6379), ("litellm", 4000), ("caddy", 80)]
for host, port in targets:
    try:
        socket.create_connection((host, port), timeout=0.5)
    except OSError:
        continue
    raise SystemExit(f"worker reached {host}:{port}")
'

docker compose exec -T postgres psql -X -v ON_ERROR_STOP=1 \
  -U "${POSTGRES_USER}" -d "${POSTGRES_DB}" <<'SQL' >/dev/null
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
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_trigger
    WHERE tgname = 'decisions_append_only'
  ) THEN
    RAISE EXCEPTION 'missing decisions append-only trigger';
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM pg_trigger
    WHERE tgname = 'plan_revisions_immutable'
  ) THEN
    RAISE EXCEPTION 'missing plan revision immutability trigger';
  END IF;
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
    row = await conn.fetchrow("""
    SELECT current_user,
               has_table_privilege(current_user, $$decisions$$, $$INSERT$$),
               has_table_privilege(current_user, $$worker_runs$$, $$INSERT$$),
               has_sequence_privilege(current_user, $$worker_runs_id_seq$$, $$USAGE$$)
    """)
    await conn.close()
    if row[0] != "chitti_runner" or row[1] or not row[2] or not row[3]:
        raise SystemExit("runner role privilege boundary failed")

asyncio.run(main())
'

systemctl is-enabled --quiet "${RUNNER_UNIT}"
systemctl is-active --quiet "${RUNNER_UNIT}"
echo "Deployment and post-deploy boundary checks completed."
