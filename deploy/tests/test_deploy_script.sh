#!/usr/bin/env bash
set -Eeuo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
script="${repo_root}/deploy/deploy-main.sh"
remote_wrapper="${repo_root}/deploy/deploy-remote.sh"

[[ "$(tail -n 1 "${script}")" == 'echo "${DEPLOY_COMPLETION_MARKER}"' ]]
grep -Fq 'DEPLOY_COMPLETION_MARKER="CHITTI_DEPLOY_COMPLETE"' "${script}"
grep -Fq 'docker run --rm -i --entrypoint python "${runner_image}" -c \' "${script}"
grep -Fq '< <(printf '\''%s'\'' "${gateway_models}")' "${script}"
! grep -Fq 'printf '\''%s'\'' "${gateway_models}" |' "${script}"
grep -Fq '"${remote_script}" </dev/null' "${remote_wrapper}"
grep -Fq 'completion marker was missing' "${remote_wrapper}"

closed_input="$(mktemp)"
trap 'rm -f "${closed_input}"' EXIT
printf 'this input must not become the deploy script\n' > "${closed_input}"
output="$(DRY_RUN=1 INSTALL_DIR=/tmp/chitti-deploy-test bash "${script}" < "${closed_input}")"
[[ "$(printf '%s\n' "${output}" | tail -n 1)" == 'CHITTI_DEPLOY_COMPLETE' ]]
