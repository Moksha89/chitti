#!/usr/bin/env bash
set -Eeuo pipefail

: "${BACKUP_DIR:?BACKUP_DIR is required}"
: "${RETENTION_DAYS:?RETENTION_DAYS is required}"
: "${POSTGRES_CONTAINER:=chitti-postgres-1}"
: "${POSTGRES_USER:=chitti}"
: "${POSTGRES_DB:=chitti}"

umask 077
mkdir -p "${BACKUP_DIR}"
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
plain="${BACKUP_DIR}/chitti-${stamp}.dump"
encrypted="${plain}.enc"

if [[ -z "${BACKUP_AGE_RECIPIENT:-}" ]]; then
  echo "Set BACKUP_AGE_RECIPIENT to an age recipient (age1...)." >&2
  exit 1
fi

trap 'rm -f "${plain}"' EXIT
docker exec "${POSTGRES_CONTAINER}" pg_dump -Fc -U "${POSTGRES_USER}" "${POSTGRES_DB}" >"${plain}"
age --recipient "${BACKUP_AGE_RECIPIENT}" --output "${encrypted}" "${plain}"
find "${BACKUP_DIR}" -type f -name '*.dump.enc' -mtime +"${RETENTION_DAYS}" -delete
