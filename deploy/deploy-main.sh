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
MATTE_MODEL_PATH="${MATTE_MODEL_PATH:-/opt/chitti-runner-models/u2net.onnx}"
MATTE_MODEL_IMAGE_PATH="/app/models/u2net.onnx"
GOOGLE_SYNC_ENV="${GOOGLE_SYNC_ENV:-/etc/chitti/google-sync.env}"
GOOGLE_SYNC_UNIT="chitti-google-sync.service"
GOOGLE_SYNC_UNIT_SOURCE="${GOOGLE_SYNC_UNIT_SOURCE:-deploy/google-sync/chitti-google-sync.service}"
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
      "Would install and enable ${GOOGLE_SYNC_UNIT}" \
      "Would restart ${RUNNER_UNIT} and verify its loaded-code identity" \
      "Would create or verify the Google sync-only database role" \
      "Would create or verify the runner-only database role" \
      "Would derive, reconcile, print, and assert runner table privileges" \
      "Would verify schema, privileges, and container network boundaries"
  else
    printf '%s\n' \
      "Would clone origin/${REMOTE_BRANCH} into ${INSTALL_DIR}" \
      "Would retain the existing tree as a dated rollback directory" \
      "Would carry forward .env and projects content without printing .env" \
      "Would apply migrations through the normal chitti startup path" \
      "Would build chitti-sandbox:latest" \
      "Would install and enable ${RUNNER_UNIT}" \
      "Would install and enable ${GOOGLE_SYNC_UNIT}" \
      "Would restart ${RUNNER_UNIT} and verify its loaded-code identity" \
      "Would create or verify the Google sync-only database role" \
      "Would create or verify the runner-only database role" \
      "Would derive, reconcile, print, and assert runner table privileges" \
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
    "${rollback_dir}/deploy/worker-runner/chitti-worker-runner.service" \
    "${rollback_dir}/deploy/google-sync/chitti-google-sync.service"
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

assert_container_setting_forwarded() {
  local container="$1"
  local setting="$2"
  if [[ -z "${!setting:-}" ]]; then
    return 0
  fi
  if ! docker inspect "${container}" --format '{{range .Config.Env}}{{println .}}{{end}}' |
    grep -q "^${setting}="; then
    echo "configured ${setting} was not forwarded to ${container}" >&2
    exit 1
  fi
}

assert_process_setting_forwarded() {
  local pid="$1"
  local setting="$2"
  if [[ -z "${!setting:-}" ]]; then
    return 0
  fi
  if ! tr '\0' '\n' <"/proc/${pid}/environ" | grep -q "^${setting}="; then
    echo "configured ${setting} was not forwarded to process ${pid}" >&2
    exit 1
  fi
}

git -c safe.directory="${INSTALL_DIR}" fetch --quiet origin "${REMOTE_BRANCH}"
git -c safe.directory="${INSTALL_DIR}" checkout --quiet --detach "origin/${REMOTE_BRANCH}"

mapfile -t matte_model_digests < <(
  sed -nE \
    's/.*echo "([[:xdigit:]]{64})  \/app\/models\/u2net\.onnx".*/\1/p' \
    app/Dockerfile
)
if [[ "${#matte_model_digests[@]}" -ne 1 ]] ||
  [[ -z "${matte_model_digests[0]:-}" ]]; then
  echo "could not derive matting model digest from app/Dockerfile" >&2
  exit 1
fi
MATTE_MODEL_SHA256="${matte_model_digests[0]}"

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
matte_model_container="$(docker create "${runner_image}")"
matte_model_tmp="${MATTE_MODEL_PATH}.tmp"
trap 'docker rm -f "${matte_model_container:-}" >/dev/null 2>&1 || true; rm -f "${matte_model_tmp:-}"' EXIT
install -d -o root -g root -m 0755 "$(dirname "${MATTE_MODEL_PATH}")"
docker cp "${matte_model_container}:${MATTE_MODEL_IMAGE_PATH}" "${matte_model_tmp}"
docker rm -f "${matte_model_container}" >/dev/null
matte_model_container=""
echo "${MATTE_MODEL_SHA256}  ${matte_model_tmp}" | sha256sum -c -
install -o root -g root -m 0644 "${matte_model_tmp}" "${MATTE_MODEL_PATH}"
rm -f "${matte_model_tmp}"
trap - EXIT
image_matte_digest="$(
  docker run --rm --entrypoint sha256sum "${runner_image}" "${MATTE_MODEL_IMAGE_PATH}" |
    awk '{print $1}'
)"
if [[ "${image_matte_digest}" != "${MATTE_MODEL_SHA256}" ]]; then
  echo "application image matting weights digest mismatch" >&2
  exit 1
fi
host_matte_digest="$(sha256sum "${MATTE_MODEL_PATH}" | awk '{print $1}')"
if [[ "${host_matte_digest}" != "${MATTE_MODEL_SHA256}" ]]; then
  echo "host runner matting weights digest mismatch" >&2
  exit 1
fi
echo "U-2-Net weights are digest-matched in the application image and host runner."

declared_app_settings="$(
  docker run --rm --entrypoint python "${runner_image}" -c '
from chitti.settings import Settings

fields = Settings.model_fields
if not fields:
    raise SystemExit("Settings model fields are empty or unavailable")
for field in sorted(fields):
    print(field.upper())
'
)"
if [[ -z "${declared_app_settings}" ]]; then
  echo "could not derive application settings from the Settings model" >&2
  exit 1
fi
while IFS= read -r app_setting; do
  if ! grep -Eq "^[[:space:]]+${app_setting}:" docker-compose.yml; then
    echo "docker-compose.yml does not pass through ${app_setting}" >&2
    exit 1
  fi
done <<<"${declared_app_settings}"
echo "Settings model compose pass-through assertions passed."

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
from chitti.provider import DEPLOYMENT_GATEWAY_ROUTES

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
print("\n".join(sorted(DEPLOYMENT_GATEWAY_ROUTES - model_ids)))' \
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
for gateway_route in chitti-chat planner coder coder-fallback reviewer bulk vision vision-fallback; do
  gateway_probe="$(
    curl --fail --silent --show-error --max-time 30 \
      -H "Authorization: Bearer ${LITELLM_MASTER_KEY}" \
      -H "Content-Type: application/json" \
      "http://127.0.0.1:${LITELLM_PORT:-4000}/v1/chat/completions" \
      --data "{\"model\":\"${gateway_route}\",\"messages\":[{\"role\":\"user\",\"content\":\"Return exactly OK.\"}],\"max_tokens\":1}"
  )" || {
    echo "gateway route probe failed: ${gateway_route}" >&2
    exit 1
  }
  if ! printf '%s' "${gateway_probe}" | docker run --rm -i --entrypoint python "${runner_image}" -c \
    'import json
import sys
payload = json.load(sys.stdin)
choices = payload.get("choices")
if not isinstance(choices, list) or not choices:
    raise SystemExit("gateway response contained no choices")'; then
    echo "gateway route probe returned an invalid response: ${gateway_route}" >&2
    exit 1
  fi
done
echo "Gateway route response probes passed."

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
  -r "${INSTALL_DIR}/app/requirements.txt"

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
    -e '/^RUNPOD_API_KEY=/d' \
    -e '/^RUNPOD_ENDPOINT_ID=/d' \
    -e '/^RUNPOD_GPU_RATE_USD=/d' \
    -e '/^CHITTI_MATTE_MODEL_PATH=/d' \
    "${RUNNER_ENV}" >"${runner_env_tmp}"
  printf 'LITELLM_BASE_URL=http://127.0.0.1:%s\nLITELLM_MASTER_KEY=%s\nRUNPOD_API_KEY=%s\nRUNPOD_ENDPOINT_ID=%s\nRUNPOD_GPU_RATE_USD=%s\nCHITTI_MATTE_MODEL_PATH=%s\n' \
    "${LITELLM_PORT:-4000}" "${LITELLM_MASTER_KEY}" \
    "${RUNPOD_API_KEY:-}" "${RUNPOD_ENDPOINT_ID:-}" "${RUNPOD_GPU_RATE_USD:-0.34}" \
    "${MATTE_MODEL_PATH}" \
    >>"${runner_env_tmp}"
  chmod 0600 "${runner_env_tmp}"
  install -o root -g root -m 0600 "${runner_env_tmp}" "${RUNNER_ENV}"
  rm -f "${runner_env_tmp}"
  trap - EXIT
else
  runner_password="$(openssl rand -hex 32)"
  runner_env_tmp="$(mktemp /etc/chitti/worker-runner.env.XXXXXX)"
  runner_sql_tmp="$(mktemp /etc/chitti/runner-role.sql.XXXXXX)"
  trap 'rm -f "${runner_env_tmp:-}" "${runner_sql_tmp:-}"' EXIT

  printf 'DATABASE_URL=postgresql+asyncpg://chitti_runner:%s@127.0.0.1:5432/%s\nPREVIEW_ROOT=/var/lib/chitti-previews\nPREVIEW_STAGING_ROOT=/var/lib/chitti-preview-staging\nLITELLM_BASE_URL=http://127.0.0.1:%s\nLITELLM_MASTER_KEY=%s\nRUNPOD_API_KEY=%s\nRUNPOD_ENDPOINT_ID=%s\nRUNPOD_GPU_RATE_USD=%s\nCHITTI_MATTE_MODEL_PATH=%s\n' \
    "${runner_password}" "${POSTGRES_DB}" "${LITELLM_PORT:-4000}" "${LITELLM_MASTER_KEY}" \
    "${RUNPOD_API_KEY:-}" "${RUNPOD_ENDPOINT_ID:-}" "${RUNPOD_GPU_RATE_USD:-0.34}" \
    "${MATTE_MODEL_PATH}" >"${runner_env_tmp}"
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

sync_role_exists="$(
  docker compose exec -T postgres psql -X -qAt \
    -U "${POSTGRES_USER}" -d "${POSTGRES_DB}" \
    -c "SELECT 1 FROM pg_roles WHERE rolname = 'chitti_google_sync'"
)"
if [[ "${sync_role_exists}" == "1" ]]; then
  if [[ ! -s "${GOOGLE_SYNC_ENV}" ]]; then
    echo "Google sync role exists but ${GOOGLE_SYNC_ENV} is missing; refusing to rotate credentials." >&2
    exit 1
  fi
  sync_env_tmp="$(mktemp /etc/chitti/google-sync.env.XXXXXX)"
  trap 'rm -f "${sync_env_tmp:-}"' EXIT
  sed \
    -e '/^GOOGLE_CREDENTIALS_KEY=/d' \
    -e '/^GOOGLE_SYNC_INTERVAL_SECONDS=/d' \
    -e '/^GOOGLE_RECENT_MAIL_DAYS=/d' \
    -e '/^GOOGLE_INITIAL_MAIL_LIMIT=/d' \
    -e '/^GOOGLE_CALENDAR_WINDOW_DAYS=/d' \
    "${GOOGLE_SYNC_ENV}" >"${sync_env_tmp}"
  printf 'GOOGLE_CREDENTIALS_KEY=%s\nGOOGLE_SYNC_INTERVAL_SECONDS=%s\nGOOGLE_RECENT_MAIL_DAYS=%s\nGOOGLE_INITIAL_MAIL_LIMIT=%s\nGOOGLE_CALENDAR_WINDOW_DAYS=%s\n' \
    "${GOOGLE_CREDENTIALS_KEY:-}" "${GOOGLE_SYNC_INTERVAL_SECONDS:-300}" \
    "${GOOGLE_RECENT_MAIL_DAYS:-30}" "${GOOGLE_INITIAL_MAIL_LIMIT:-100}" \
    "${GOOGLE_CALENDAR_WINDOW_DAYS:-30}" >>"${sync_env_tmp}"
  chmod 0600 "${sync_env_tmp}"
  install -o root -g root -m 0600 "${sync_env_tmp}" "${GOOGLE_SYNC_ENV}"
  rm -f "${sync_env_tmp}"
  trap - EXIT
else
  sync_password="$(openssl rand -hex 32)"
  sync_env_tmp="$(mktemp /etc/chitti/google-sync.env.XXXXXX)"
  trap 'rm -f "${sync_env_tmp:-}"' EXIT
  printf 'DATABASE_URL=postgresql+asyncpg://chitti_google_sync:%s@127.0.0.1:5432/%s\nGOOGLE_CREDENTIALS_KEY=%s\nGOOGLE_SYNC_INTERVAL_SECONDS=%s\nGOOGLE_RECENT_MAIL_DAYS=%s\nGOOGLE_INITIAL_MAIL_LIMIT=%s\nGOOGLE_CALENDAR_WINDOW_DAYS=%s\n' \
    "${sync_password}" "${POSTGRES_DB}" "${GOOGLE_CREDENTIALS_KEY:-}" \
    "${GOOGLE_SYNC_INTERVAL_SECONDS:-300}" "${GOOGLE_RECENT_MAIL_DAYS:-30}" \
    "${GOOGLE_INITIAL_MAIL_LIMIT:-100}" "${GOOGLE_CALENDAR_WINDOW_DAYS:-30}" >"${sync_env_tmp}"
  chmod 0600 "${sync_env_tmp}"
  docker compose exec -T postgres psql -X -v ON_ERROR_STOP=1 \
    -U "${POSTGRES_USER}" -d "${POSTGRES_DB}" \
    -c "CREATE ROLE chitti_google_sync LOGIN PASSWORD '${sync_password}'" >/dev/null
  install -o root -g root -m 0600 "${sync_env_tmp}" "${GOOGLE_SYNC_ENV}"
  rm -f "${sync_env_tmp}"
  unset sync_password
  trap - EXIT
fi
docker compose exec -T postgres psql -X -v ON_ERROR_STOP=1 \
  -U "${POSTGRES_USER}" -d "${POSTGRES_DB}" \
  -c "GRANT CONNECT ON DATABASE ${POSTGRES_DB} TO chitti_google_sync; GRANT USAGE ON SCHEMA public TO chitti_google_sync;" >/dev/null

POSTGRES_USER="${POSTGRES_USER}" \
POSTGRES_PASSWORD="${POSTGRES_PASSWORD}" \
POSTGRES_DB="${POSTGRES_DB}" \
POSTGRES_PORT="${POSTGRES_PORT:-5432}" \
PYTHONPATH="${INSTALL_DIR}/app" "${RUNNER_PYTHON}" - <<'PY'
import asyncio
import os
from urllib.parse import quote
import asyncpg
from chitti.google_sync_access import reconcile_sync_privileges

async def main():
    user = quote(os.environ["POSTGRES_USER"], safe="")
    password = quote(os.environ["POSTGRES_PASSWORD"], safe="")
    database = quote(os.environ["POSTGRES_DB"], safe="")
    port = os.environ["POSTGRES_PORT"]
    conn = await asyncpg.connect(
        f"postgresql://{user}:{password}@127.0.0.1:{port}/{database}"
    )
    try:
        await reconcile_sync_privileges(conn)
    finally:
        await conn.close()

asyncio.run(main())
PY

install -o root -g root -m 0644 \
  "${GOOGLE_SYNC_UNIT_SOURCE}" \
  "/etc/systemd/system/${GOOGLE_SYNC_UNIT}"
systemctl daemon-reload
systemctl enable "${GOOGLE_SYNC_UNIT}"
systemctl restart "${GOOGLE_SYNC_UNIT}"

POSTGRES_USER="${POSTGRES_USER}" \
POSTGRES_PASSWORD="${POSTGRES_PASSWORD}" \
POSTGRES_DB="${POSTGRES_DB}" \
POSTGRES_PORT="${POSTGRES_PORT:-5432}" \
PYTHONPATH="${INSTALL_DIR}/app" "${RUNNER_PYTHON}" - <<'PY'
import asyncio
import os
from urllib.parse import quote

import asyncpg

from chitti.runner_access import reconcile_runner_privileges


def database_url() -> str:
    user = quote(os.environ["POSTGRES_USER"], safe="")
    password = quote(os.environ["POSTGRES_PASSWORD"], safe="")
    database = quote(os.environ["POSTGRES_DB"], safe="")
    port = os.environ["POSTGRES_PORT"]
    return f"postgresql://{user}:{password}@127.0.0.1:{port}/{database}"


async def main() -> None:
    conn = await asyncpg.connect(database_url())
    try:
        await reconcile_runner_privileges(conn)
    finally:
        await conn.close()


asyncio.run(main())
PY

install -o root -g root -m 0644 \
  "${RUNNER_UNIT_SOURCE}" \
  "/etc/systemd/system/${RUNNER_UNIT}"
systemctl daemon-reload
systemctl enable "${RUNNER_UNIT}"
CHITTI_MATTE_MODEL_PATH="${MATTE_MODEL_PATH}" \
  PYTHONPATH="${INSTALL_DIR}/app" "${RUNNER_PYTHON}" -c \
  'import chitti.runner; print("Runner module import assertion passed.")'
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
runner_restarts_after_start="$(systemctl show --property=NRestarts --value "${RUNNER_UNIT}")"
sleep 3
if [[ "$(systemctl show --property=MainPID --value "${RUNNER_UNIT}")" != "${runner_pid}" ]] ||
  [[ "$(systemctl show --property=NRestarts --value "${RUNNER_UNIT}")" != "${runner_restarts_after_start}" ]]; then
  echo "runner restarted after its active-state proof" >&2
  exit 1
fi
systemctl is-active --quiet "${RUNNER_UNIT}"
echo "Runner remained active with zero restarts during the stability window."

for runner_setting in RUNPOD_API_KEY RUNPOD_ENDPOINT_ID RUNPOD_GPU_RATE_USD \
  CHITTI_MATTE_MODEL_PATH; do
  assert_process_setting_forwarded "${runner_pid}" "${runner_setting}"
done
sync_pid="$(systemctl show --property=MainPID --value "${GOOGLE_SYNC_UNIT}")"
if [[ "${sync_pid}" != "0" ]]; then
  for sync_setting in GOOGLE_CREDENTIALS_KEY GOOGLE_SYNC_INTERVAL_SECONDS \
    GOOGLE_RECENT_MAIL_DAYS GOOGLE_INITIAL_MAIL_LIMIT GOOGLE_CALENDAR_WINDOW_DAYS; do
    assert_process_setting_forwarded "${sync_pid}" "${sync_setting}"
  done
fi
echo "Configured runtime settings reached their owning processes."

app_container="$(docker compose ps -q chitti)"
[[ -n "${app_container}" ]]
for app_setting in \
  RUNPOD_API_KEY RUNPOD_ENDPOINT_ID RUNPOD_GPU_RATE_USD \
  GOOGLE_CLIENT_ID GOOGLE_CLIENT_SECRET GOOGLE_OAUTH_REDIRECT_URI \
  GOOGLE_CREDENTIALS_KEY GOOGLE_SYNC_INTERVAL_SECONDS \
  GOOGLE_RECENT_MAIL_DAYS GOOGLE_INITIAL_MAIL_LIMIT GOOGLE_CALENDAR_WINDOW_DAYS; do
  assert_container_setting_forwarded "${app_container}" "${app_setting}"
done
if docker inspect "${app_container}" \
  --format '{{range .Mounts}}{{if eq .Destination "/var/run/docker.sock"}}FAIL{{end}}{{end}}' |
  grep -q '^FAIL$'; then
    echo "Application container has Docker socket access." >&2
    exit 1
fi
docker exec "${app_container}" test ! -S /var/run/docker.sock

for runner_setting in RUNPOD_API_KEY RUNPOD_ENDPOINT_ID RUNPOD_GPU_RATE_USD \
  CHITTI_MATTE_MODEL_PATH; do
  if [[ -n "${!runner_setting:-}" ]] &&
    ! grep -Eq "^${runner_setting}=" "${RUNNER_ENV}"; then
    echo "configured ${runner_setting} was not written to ${RUNNER_ENV}" >&2
    exit 1
  fi
done
for sync_setting in GOOGLE_CREDENTIALS_KEY GOOGLE_SYNC_INTERVAL_SECONDS \
  GOOGLE_RECENT_MAIL_DAYS GOOGLE_INITIAL_MAIL_LIMIT GOOGLE_CALENDAR_WINDOW_DAYS; do
  if [[ -n "${!sync_setting:-}" ]] &&
    ! grep -Eq "^${sync_setting}=" "${GOOGLE_SYNC_ENV}"; then
    echo "configured ${sync_setting} was not written to ${GOOGLE_SYNC_ENV}" >&2
    exit 1
  fi
done
echo "Configured runtime settings were forwarded to their owning processes."

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
expected_run_event_statuses="$(
  docker run --rm --entrypoint python "${runner_image}" -c '
import json
from chitti.run_status import RUN_EVENT_STATUSES
print(json.dumps(sorted(RUN_EVENT_STATUSES), separators=(",", ":")))
'
)"
docker compose exec -T postgres psql -X -v ON_ERROR_STOP=1 \
  -v "expected_run_event_statuses=${expected_run_event_statuses}" \
  -U "${POSTGRES_USER}" -d "${POSTGRES_DB}" <<'SQL' >/dev/null
CREATE TEMP TABLE expected_run_event_status_values(status text);
INSERT INTO expected_run_event_status_values(status)
SELECT value
FROM json_array_elements_text(:'expected_run_event_statuses'::json);
DO $$
DECLARE
  expected_statuses text;
  live_statuses text;
BEGIN
  SELECT string_agg(status, E'\n' ORDER BY status)
  INTO expected_statuses
  FROM expected_run_event_status_values;
  SELECT string_agg(captures[1], E'\n' ORDER BY captures[1])
  INTO live_statuses
  FROM (
    SELECT regexp_matches(
      pg_get_constraintdef(oid), '''([^'']+)''', 'g'
    ) AS captures
    FROM pg_constraint
    WHERE conname = 'worker_run_event_status_ck'
      AND conrelid = 'worker_run_events'::regclass
  ) AS constraint_statuses;
  IF live_statuses IS DISTINCT FROM expected_statuses THEN
    RAISE EXCEPTION
      'run-event status contract drift: expected %, live %',
      COALESCE(expected_statuses, '<empty>'),
      COALESCE(live_statuses, '<empty>');
  END IF;
END
$$;
SQL
echo "Run-event status contract full-set proof passed."
docker compose exec -T postgres psql -X -v ON_ERROR_STOP=1 \
  -U "${POSTGRES_USER}" -d "${POSTGRES_DB}" <<'SQL' >/dev/null
DO $$
DECLARE
  required_table text;
BEGIN
  FOREACH required_table IN ARRAY ARRAY[
    'decisions', 'decision_forgets', 'plan_revisions', 'plan_approvals',
    'worker_runs', 'worker_run_events', 'worker_operations',
    'worker_artifacts', 'worker_model_calls', 'worker_image_jobs', 'export_manifests',
    'promotion_approvals', 'previews'
    , 'google_provider_accounts', 'google_oauth_credentials',
    'google_sync_state', 'google_gmail_messages', 'google_calendar_events',
    'google_account_audit', 'google_email_actions',
    'google_email_action_approvals'
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
    'reject_worker_image_job_mutation_trigger',
    'reject_export_manifest_mutation_trigger',
    'reject_promotion_approval_mutation_trigger',
    'reject_preview_mutation_trigger',
    'chat_transcript_entries_immutable'
    , 'reject_google_account_audit_mutation_trigger',
    'reject_google_email_action_mutation_trigger',
    'reject_google_email_approval_mutation_trigger'
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
from pathlib import Path
import asyncpg

async def main():
    database_url = os.environ["DATABASE_URL"].replace(
        "postgresql+asyncpg://", "postgresql://", 1
    )
    conn = await asyncpg.connect(database_url)
    current_user = await conn.fetchval("SELECT current_user")
    if current_user != "chitti_runner":
        raise SystemExit("runner role identity check failed")

    from chitti.runner_access import assert_runner_privileges
    await assert_runner_privileges(conn)

    from chitti.runner import best_effort_preview_maintenance
    from chitti.settings import get_settings
    from chitti.worker import DockerSandboxDispatcher
    from chitti.db import Database

    database = Database(get_settings())
    class FailingDispatcher:
        async def cleanup_expired_previews(self):
            raise RuntimeError("deployment maintenance resilience proof")

    await best_effort_preview_maintenance(database, FailingDispatcher())
    health = await conn.fetchrow(
        "SELECT status, detail FROM runner_health "
        "WHERE component = '\''preview_cleanup'\''"
    )
    if health is None or health["status"] != "failed" or \
       "deployment maintenance resilience proof" not in health["detail"]:
        raise SystemExit("maintenance failure was not durably recorded")

    preview_root = Path("/tmp/chitti-preview-proof")
    preview_root.mkdir(parents=True, exist_ok=True)
    dispatcher = DockerSandboxDispatcher(database, preview_root=preview_root)
    await best_effort_preview_maintenance(database, dispatcher)
    health = await conn.fetchrow(
        "SELECT status FROM runner_health "
        "WHERE component = '\''preview_cleanup'\''"
    )
    if health is None or health["status"] != "healthy":
        raise SystemExit("preview maintenance success was not recorded")
    await database.close()
    await conn.close()

asyncio.run(main())
'
echo "Runner maintenance path and failure isolation proof passed."

systemctl is-enabled --quiet "${RUNNER_UNIT}"
systemctl is-active --quiet "${RUNNER_UNIT}"
echo "Deployment and post-deploy boundary checks completed."
echo "${DEPLOY_COMPLETION_MARKER}"
