#!/usr/bin/env bash
set -Eeuo pipefail

SERVICE_USER="${SERVICE_USER:-chitti}"
INSTALL_DIR="${INSTALL_DIR:-/opt/chitti}"
BACKUP_DIR="${BACKUP_DIR:-/var/backups/chitti}"
RETENTION_DAYS="${RETENTION_DAYS:-14}"

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run this script as root." >&2
  exit 1
fi

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y age ca-certificates curl gnupg ufw fail2ban postgresql-client

if ! id "${SERVICE_USER}" >/dev/null 2>&1; then
  useradd --system --create-home --shell /usr/sbin/nologin "${SERVICE_USER}"
fi
usermod -aG docker "${SERVICE_USER}" 2>/dev/null || true

if ! command -v docker >/dev/null 2>&1; then
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg | gpg --dearmor --yes -o /etc/apt/keyrings/docker.gpg
  chmod a+r /etc/apt/keyrings/docker.gpg
  . /etc/os-release
  printf 'deb [arch=%s signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu %s stable\n' \
    "$(dpkg --print-architecture)" "${VERSION_CODENAME}" > /etc/apt/sources.list.d/docker.list
  apt-get update
  apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
  systemctl enable --now docker
fi

install -d -o "${SERVICE_USER}" -g "${SERVICE_USER}" "${INSTALL_DIR}" "${BACKUP_DIR}"

# Do not lock out the operator: only disable password authentication when a
# usable authorized_keys file already exists.
AUTHORIZED_KEYS="/root/.ssh/authorized_keys"
if [[ -s "${AUTHORIZED_KEYS}" ]]; then
  install -d -m 0700 /etc/ssh/sshd_config.d
  cat >/etc/ssh/sshd_config.d/99-chitti-hardening.conf <<'EOF'
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin prohibit-password
PubkeyAuthentication yes
EOF
  sshd -t
  systemctl reload ssh
else
  echo "No /root/.ssh/authorized_keys found; retaining password SSH auth." >&2
fi

ufw --force reset
ufw default deny incoming
ufw default allow outgoing
ufw allow 22/tcp
ufw allow 80/tcp
ufw allow 443/tcp
ufw --force enable

systemctl enable --now fail2ban

install -d -m 0700 -o "${SERVICE_USER}" -g "${SERVICE_USER}" "${BACKUP_DIR}"
cat >/etc/cron.d/chitti-postgres-backup <<EOF
17 2 * * * ${SERVICE_USER} BACKUP_DIR=${BACKUP_DIR} RETENTION_DAYS=${RETENTION_DAYS} ${INSTALL_DIR}/deploy/backup-postgres.sh
EOF
chmod 0644 /etc/cron.d/chitti-postgres-backup

echo "Bootstrap complete. Copy the stack into ${INSTALL_DIR}, review .env, then run make up."
