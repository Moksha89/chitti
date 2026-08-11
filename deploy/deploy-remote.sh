#!/usr/bin/env bash
set -Eeuo pipefail

if [[ "$#" -lt 1 || "$#" -gt 2 ]]; then
  echo "usage: $0 user@host [ssh-key]" >&2
  exit 2
fi

target="$1"
ssh_key="${2:-}"
script_name="chitti-deploy-main-$$.sh"
remote_script="/tmp/${script_name}"
ssh_args=(-o IdentitiesOnly=yes)
if [[ -n "${ssh_key}" ]]; then
  ssh_args+=(-i "${ssh_key}")
fi

cleanup() {
  ssh "${ssh_args[@]}" "${target}" "rm -f '${remote_script}'" >/dev/null 2>&1 || true
}
trap cleanup EXIT

scp "${ssh_args[@]}" deploy/deploy-main.sh "${target}:${remote_script}"
remote_command=$(cat <<EOF
set -o pipefail
log_file="/tmp/${script_name}.log"
chmod 700 "${remote_script}"
"${remote_script}" </dev/null 2>&1 | tee "\${log_file}"
status=\${PIPESTATUS[0]}
if [[ \${status} -ne 0 ]] || ! grep -Fxq "CHITTI_DEPLOY_COMPLETE" "\${log_file}"; then
  echo "deployment failed or completion marker was missing" >&2
  rm -f "\${log_file}"
  exit 1
fi
rm -f "\${log_file}"
EOF
)
ssh "${ssh_args[@]}" "${target}" "bash -lc $(printf '%q' "${remote_command}")"
