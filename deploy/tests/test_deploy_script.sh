#!/usr/bin/env bash
set -Eeuo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
script="${repo_root}/deploy/deploy-main.sh"
remote_wrapper="${repo_root}/deploy/deploy-remote.sh"

[[ "$(tail -n 1 "${script}")" == 'echo "${DEPLOY_COMPLETION_MARKER}"' ]]
[[ "$(sed -n '3p' "${script}")" == 'exec </dev/null' ]]
grep -Fq 'DEPLOY_COMPLETION_MARKER="CHITTI_DEPLOY_COMPLETE"' "${script}"
grep -Fq 'CHITTI_GATEWAY_MODELS_JSON=${gateway_models}' "${script}"
grep -Fq 'json.loads(os.environ["CHITTI_GATEWAY_MODELS_JSON"])' "${script}"
grep -Fq 'docker run --rm \' "${script}"
! grep -Fq 'docker run --rm -i --entrypoint python "${runner_image}" -c \' "${script}"
! grep -Fq '< <(printf '\''%s'\'' "${gateway_models}")' "${script}"
grep -Fq 'response was not valid JSON' "${script}"
grep -Fq 'gateway request was unreachable or failed' "${script}"
gateway_input='{"data":[{"id":"chitti-chat"}]}'
parsed_gateway_input="$(
  CHITTI_GATEWAY_MODELS_JSON="${gateway_input}" \
    python3 -c 'import json, os; print(json.loads(os.environ["CHITTI_GATEWAY_MODELS_JSON"])["data"][0]["id"])'
)"
[[ "${parsed_gateway_input}" == 'chitti-chat' ]]
if CHITTI_GATEWAY_MODELS_JSON='not-json' \
  python3 -c 'import json, os; json.loads(os.environ["CHITTI_GATEWAY_MODELS_JSON"])' \
  >/dev/null 2>&1; then
  echo "malformed gateway JSON unexpectedly parsed" >&2
  exit 1
fi
grep -Fq '"${remote_script}" </dev/null' "${remote_wrapper}"
grep -Fq 'completion marker was missing' "${remote_wrapper}"

closed_input="$(mktemp)"
trap 'rm -f "${closed_input}"' EXIT
printf 'this input must not become the deploy script\n' > "${closed_input}"
output="$(DRY_RUN=1 INSTALL_DIR=/tmp/chitti-deploy-test bash "${script}" < "${closed_input}")"
[[ "$(printf '%s\n' "${output}" | tail -n 1)" == 'CHITTI_DEPLOY_COMPLETE' ]]
