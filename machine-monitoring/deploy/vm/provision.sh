#!/usr/bin/env bash
# Provision the monitoring VM (Debian). Run as root ON THE VM; bootstrap.sh
# calls it over SSH after copying the repository to $APP_DIR.
#
# Idempotent configuration management for v0.1 (deploy/ansible/ is reserved
# for when the fleet outgrows a single script):
#   * data disk: formatted ONLY if it has no filesystem, mounted at /srv
#   * SSH: keys only (only applied when the admin user has an authorized key)
#   * Docker Engine + Compose plugin, data root on the data disk
#   * unattended security updates
#   * .env generated once (random secrets), non-secret keys kept in sync
#   * database backup before migrations when a database already exists
#   * systemd unit so the stack starts after every reboot
#   * health check
set -Eeuo pipefail

APP_DIR="${APP_DIR:-/opt/machine-monitoring}"
DATA_DEVICE="${DATA_DEVICE:-/dev/disk/by-id/scsi-0QEMU_QEMU_HARDDISK_drive-scsi1}"
DATA_MOUNT="${DATA_MOUNT:-/srv}"
DATA_LABEL="mmdata"
COMPOSE_DIR="$APP_DIR/deploy/docker"
ENV_FILE="$COMPOSE_DIR/.env"
ADMIN_USER="${ADMIN_USER:-}"

step() { printf '\n--> %s\n' "$*"; }
ok()   { printf '    ok %s\n' "$*"; }
warn() { printf '    warning: %s\n' "$*" >&2; }
die()  { printf '    error: %s\n' "$*" >&2; exit 1; }
trap 'die "provision failed at line $LINENO: $BASH_COMMAND"' ERR

[[ $EUID -eq 0 ]] || die "run as root"
[[ -f /etc/debian_version ]] || die "Debian expected"
[[ -f "$COMPOSE_DIR/docker-compose.yml" ]] || die "$COMPOSE_DIR/docker-compose.yml missing - copy the repository first"
export DEBIAN_FRONTEND=noninteractive

# ----------------------------------------------------------------- data disk
step "Data disk"
if [[ -b "$DATA_DEVICE" ]]; then
    if ! blkid "$DATA_DEVICE" >/dev/null 2>&1; then
        # blkid finds no signature at all -> brand new disk. Never formats existing data.
        mkfs.ext4 -q -L "$DATA_LABEL" "$DATA_DEVICE"
        ok "formatted new disk $DATA_DEVICE (ext4, label $DATA_LABEL)"
    fi
    uuid="$(blkid -s UUID -o value "$DATA_DEVICE")"
    install -d "$DATA_MOUNT"
    if ! grep -q "UUID=$uuid" /etc/fstab; then
        echo "UUID=$uuid $DATA_MOUNT ext4 defaults,nofail,noatime 0 2" >>/etc/fstab
        ok "fstab entry added"
    fi
    systemctl daemon-reload
    mountpoint -q "$DATA_MOUNT" || mount "$DATA_MOUNT"
    ok "$DATA_DEVICE mounted at $DATA_MOUNT"
else
    warn "$DATA_DEVICE not found - data stays on the OS disk under $DATA_MOUNT"
    install -d "$DATA_MOUNT"
fi
install -d -m 750 "$DATA_MOUNT/docker" "$DATA_MOUNT/backups"

# ------------------------------------------------------------------ base OS
step "Base packages and security updates"
apt-get update -qq
apt-get install -y -qq ca-certificates curl gnupg jq unattended-upgrades apt-listchanges qemu-guest-agent >/dev/null
cat >/etc/apt/apt.conf.d/20auto-upgrades <<'EOF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
EOF
# Security updates only; no automatic reboots (reboot windows are a plant decision).
cat >/etc/apt/apt.conf.d/52machine-monitoring-unattended <<'EOF'
Unattended-Upgrade::Origins-Pattern { "origin=Debian,codename=${distro_codename}-security,label=Debian-Security"; };
Unattended-Upgrade::Automatic-Reboot "false";
EOF
[[ -n "${TIMEZONE:-}" ]] && timedatectl set-timezone "$TIMEZONE" 2>/dev/null || true
systemctl enable --now qemu-guest-agent >/dev/null 2>&1 || true
ok "unattended security upgrades enabled"

# ---------------------------------------------------------------------- SSH
step "SSH hardening"
admin_home="$(getent passwd "${ADMIN_USER:-nobody}" | cut -d: -f6 || true)"
if [[ -n "$ADMIN_USER" && -s "$admin_home/.ssh/authorized_keys" ]]; then
    cat >/etc/ssh/sshd_config.d/10-machine-monitoring.conf <<'EOF'
# Managed by machine-monitoring provision.sh
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin no
EOF
    sshd -t && systemctl reload ssh
    ok "password login disabled, root login disabled"
else
    warn "no authorized_keys for '${ADMIN_USER:-?}' - leaving SSH settings unchanged to avoid a lockout"
fi

# ------------------------------------------------------------------- Docker
step "Docker Engine"
install -d /etc/docker
if [[ ! -f /etc/docker/daemon.json ]]; then
    cat >/etc/docker/daemon.json <<EOF
{
  "data-root": "$DATA_MOUNT/docker",
  "log-driver": "json-file",
  "log-opts": { "max-size": "20m", "max-file": "5" },
  "live-restore": true
}
EOF
    ok "daemon.json (data-root $DATA_MOUNT/docker)"
fi
if ! command -v docker >/dev/null || ! docker compose version >/dev/null 2>&1; then
    . /etc/os-release
    install -m 0755 -d /etc/apt/keyrings
    curl -fsSL "https://download.docker.com/linux/debian/gpg" -o /etc/apt/keyrings/docker.asc
    chmod a+r /etc/apt/keyrings/docker.asc
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian ${VERSION_CODENAME} stable" \
        >/etc/apt/sources.list.d/docker.list
    apt-get update -qq
    apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin >/dev/null
fi
systemctl enable --now docker >/dev/null
[[ -n "$ADMIN_USER" ]] && id "$ADMIN_USER" >/dev/null 2>&1 && usermod -aG docker "$ADMIN_USER"
ok "$(docker --version), $(docker compose version --short)"
actual_root="$(docker info -f '{{.DockerRootDir}}')"
[[ "$actual_root" == "$DATA_MOUNT/docker" ]] || warn "Docker data root is $actual_root (expected $DATA_MOUNT/docker)"

# ---------------------------------------------------------------- .env file
step "Application configuration"
bash "$APP_DIR/scripts/gen-env.sh" "$ENV_FILE"
set_env() {  # non-secret keys that follow the deployment (never used for secrets)
    local key="$1" value="$2"
    if grep -q "^$key=" "$ENV_FILE"; then
        sed -i "s|^$key=.*|$key=$value|" "$ENV_FILE"
    else
        echo "$key=$value" >>"$ENV_FILE"
    fi
}
[[ -n "${PUBLIC_URL:-}" ]] && set_env PUBLIC_URL "$PUBLIC_URL"
[[ -n "${COMPOSE_PROFILES:-}" ]] && set_env COMPOSE_PROFILES "$COMPOSE_PROFILES"
[[ -n "${RDC53_PLC_IP:-}" ]] && set_env RDC53_PLC_IP "$RDC53_PLC_IP"
set_env DOCKER_DATA_ROOT "$actual_root"
chmod 600 "$ENV_FILE"
chown root:root "$ENV_FILE"
ok "$ENV_FILE"

# ------------------------------------------------- backup before migrations
step "Database"
cd "$COMPOSE_DIR"
if docker volume inspect machine-monitoring_pgdata >/dev/null 2>&1; then
    # Existing database: take a backup before new migrations/images are applied.
    docker compose up -d postgres >/dev/null
    BACKUP_DIR="$DATA_MOUNT/backups" bash "$APP_DIR/scripts/backup.sh" pre-deploy
else
    ok "new installation - no existing database to back up"
fi

# ------------------------------------------------------------ systemd + start
step "Starting the stack"
cat >/etc/systemd/system/machine-monitoring.service <<EOF
[Unit]
Description=Nueva machine monitoring stack (read-only)
Requires=docker.service
After=docker.service network-online.target
Wants=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
WorkingDirectory=$COMPOSE_DIR
ExecStart=/usr/bin/docker compose up -d --remove-orphans
ExecStop=/usr/bin/docker compose stop
TimeoutStartSec=900

[Install]
WantedBy=multi-user.target
EOF
cat >/etc/systemd/system/machine-monitoring-backup.service <<EOF
[Unit]
Description=Nueva machine monitoring nightly database backup
Requires=docker.service
After=docker.service

[Service]
Type=oneshot
Environment=BACKUP_DIR=$DATA_MOUNT/backups
ExecStart=/usr/bin/bash $APP_DIR/scripts/backup.sh nightly
EOF
cat >/etc/systemd/system/machine-monitoring-backup.timer <<'EOF'
[Unit]
Description=Nightly machine monitoring database backup

[Timer]
OnCalendar=*-*-* 02:15:00
RandomizedDelaySec=15m
Persistent=true

[Install]
WantedBy=timers.target
EOF
systemctl daemon-reload
systemctl enable machine-monitoring.service machine-monitoring-backup.timer >/dev/null
systemctl start machine-monitoring-backup.timer
docker compose build --pull -q
systemctl restart machine-monitoring.service
ok "stack started; enabled at boot (machine-monitoring.service), nightly backup timer active"

# ----------------------------------------------------------------- verify
step "Health check"
bash "$APP_DIR/scripts/healthcheck.sh" --wait 180
