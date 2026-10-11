#!/usr/bin/env bash
# Nueva machine monitoring - one-command Proxmox bootstrap.
#
#   git clone <repo> && cd machine-monitoring
#   sudo ./deploy/bootstrap.sh            # create/reuse VM, provision, start, verify
#   sudo ./deploy/bootstrap.sh --check    # preflight only, changes nothing
#   sudo ./deploy/bootstrap.sh --host 192.0.2.10   # (re)provision an existing Debian host, no Proxmox
#
# Safe to re-run: every step checks current state first. It never deletes a
# VM, never formats a disk that already has a filesystem, never overwrites
# existing secrets, and takes a database backup before applying migrations
# to an existing database.
#
# It never touches PLCs or machine networks: no discovery, no PLC
# configuration, no routes. See docs/architecture.md, "What bootstrap must not do".

set -Eeuo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG_FILE="$REPO_DIR/deploy/config.env"
CHECK_ONLY=0
TARGET_HOST=""
SNIPPET_NAME="machine-monitoring-vendor.yaml"
KNOWN_HOSTS="/root/.ssh/known_hosts.machine-monitoring"
GENERATED_KEY="/root/.ssh/machine-monitoring_ed25519"

# --------------------------------------------------------------------------- utils
c_info=$'\e[1;34m'; c_ok=$'\e[1;32m'; c_warn=$'\e[1;33m'; c_err=$'\e[1;31m'; c_off=$'\e[0m'
[[ -t 1 ]] || { c_info=""; c_ok=""; c_warn=""; c_err=""; c_off=""; }
step() { printf '\n%s==> %s%s\n' "$c_info" "$*" "$c_off"; }
ok()   { printf '%s  ok%s %s\n' "$c_ok" "$c_off" "$*"; }
warn() { printf '%s  warning%s %s\n' "$c_warn" "$c_off" "$*" >&2; }
die()  { printf '%s  error%s %s\n' "$c_err" "$c_off" "$*" >&2; exit 1; }
trap 'die "failed at line $LINENO: $BASH_COMMAND (nothing is rolled back; fix and re-run - steps are idempotent)"' ERR

usage() { sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }

while [[ $# -gt 0 ]]; do
    case "$1" in
        --config) CONFIG_FILE="$2"; shift 2 ;;
        --check) CHECK_ONLY=1; shift ;;
        --host) TARGET_HOST="$2"; shift 2 ;;
        -h|--help) usage 0 ;;
        *) printf 'unknown argument: %s\n' "$1" >&2; usage 1 ;;
    esac
done

load_config() {
    # shellcheck disable=SC1090,SC1091
    source "$REPO_DIR/deploy/config.env.example"
    if [[ -f "$CONFIG_FILE" ]]; then
        # shellcheck disable=SC1090
        source "$CONFIG_FILE"
        ok "config: $CONFIG_FILE"
    else
        warn "no $CONFIG_FILE - using defaults from config.env.example"
    fi
    [[ "$VMID" =~ ^[0-9]+$ ]] || die "VMID must be numeric"
    [[ "$VM_NAME" =~ ^[a-zA-Z0-9][a-zA-Z0-9.-]*$ ]] || die "VM_NAME is not a valid host name"
    if [[ "$VM_IP" != "dhcp" ]]; then
        [[ "$VM_IP" =~ ^[0-9.]+/[0-9]+$ ]] || die "VM_IP must be 'dhcp' or CIDR (e.g. 192.168.10.50/24)"
        [[ -n "$VM_GATEWAY" ]] || die "VM_GATEWAY is required with a static VM_IP"
    fi
}

ssh_vm() { ssh -i "$SSH_PRIVATE_KEY" -o UserKnownHostsFile="$KNOWN_HOSTS" -o StrictHostKeyChecking=accept-new \
               -o ConnectTimeout=5 -o BatchMode=yes -o LogLevel=ERROR "$VM_USER@$VM_ADDR" "$@"; }

vm_exists() { qm status "$VMID" >/dev/null 2>&1; }
vm_cfg() { qm config "$VMID" 2>/dev/null | sed -n "s/^$1: //p"; }

# ----------------------------------------------------------------------- preflight
preflight() {
    step "Preflight checks"
    [[ $EUID -eq 0 ]] || die "run as root (sudo)"
    for c in curl ssh ssh-keygen tar sha512sum; do command -v "$c" >/dev/null || die "missing command: $c"; done

    command -v pveversion >/dev/null || die "not a Proxmox VE host (pveversion missing). Use --host to provision an existing VM."
    local ver major
    ver="$(pveversion | sed -n 's#^pve-manager/\([0-9.]*\).*#\1#p')"
    major="${ver%%.*}"
    [[ -n "$major" && "$major" -ge 8 ]] || die "Proxmox VE 8 or newer required (found ${ver:-unknown})"
    ok "Proxmox VE $ver"

    local free_mb
    free_mb=$(awk '/MemAvailable/ {print int($2/1024)}' /proc/meminfo)
    if vm_exists; then
        ok "memory check skipped (VM $VMID already exists)"
    elif (( free_mb < MIN_FREE_MEMORY_MB )); then
        die "only ${free_mb} MB RAM available, need ${MIN_FREE_MEMORY_MB} MB (VM_MEMORY_MB=${VM_MEMORY_MB} + headroom)"
    else
        ok "memory available: ${free_mb} MB"
    fi

    pvesm status -storage "$VM_STORAGE" >/dev/null 2>&1 || die "storage '$VM_STORAGE' not found (pvesm status)"
    pvesm status -storage "$VM_STORAGE" -content images 2>/dev/null | grep -q "^$VM_STORAGE " \
        || die "storage '$VM_STORAGE' does not allow disk images"
    if ! vm_exists; then
        local avail_gb need_gb=$(( VM_OS_DISK_GB + VM_DATA_DISK_GB ))
        avail_gb=$(pvesm status -storage "$VM_STORAGE" | awk 'NR==2 {print int($6/1024/1024)}')
        (( avail_gb >= need_gb )) || die "storage '$VM_STORAGE' has ${avail_gb} GB free, need ${need_gb} GB"
        ok "storage $VM_STORAGE: ${avail_gb} GB free (need ${need_gb} GB)"
    fi
    pvesm status -storage "$SNIPPET_STORAGE" >/dev/null 2>&1 || die "snippet storage '$SNIPPET_STORAGE' not found"

    [[ -e "/sys/class/net/$VM_BRIDGE" ]] || die "bridge '$VM_BRIDGE' does not exist"
    ok "bridge $VM_BRIDGE"

    # VM / container ID and name conflicts: never resolved by deleting anything.
    if pct status "$VMID" >/dev/null 2>&1; then
        die "ID $VMID is used by an LXC container. Choose another VMID in deploy/config.env."
    fi
    if vm_exists; then
        local name; name="$(vm_cfg name)"
        [[ "$name" == "$VM_NAME" ]] || die "VMID $VMID belongs to VM '$name', not '$VM_NAME'. Choose another VMID - nothing was changed."
        ok "VM $VMID ($VM_NAME) exists - will be reused"
    else
        local other
        other="$( (qm list 2>/dev/null || true) | awk -v n="$VM_NAME" 'NR > 1 && $2 == n {print $1; exit}')"
        [[ -z "$other" ]] || die "a VM named '$VM_NAME' already exists with ID $other. Set VMID=$other or pick another VM_NAME."
        ok "VMID $VMID is free"
    fi

    if [[ ! -f "$(image_path)" ]]; then
        curl -fsI --max-time 15 "$IMAGE_URL" >/dev/null || die "cannot reach $IMAGE_URL (Internet/DNS needed for the first install)"
        ok "image URL reachable"
    fi
    getent hosts deb.debian.org >/dev/null || warn "DNS lookup of deb.debian.org failed on the host; the VM needs Internet access for packages"
}

image_path() { printf '%s/%s' "$IMAGE_CACHE_DIR" "$(basename "$IMAGE_URL")"; }

# --------------------------------------------------------------------- ssh key
ensure_ssh_key() {
    step "SSH key"
    if [[ -n "$SSH_PUBLIC_KEY_FILE" ]]; then
        [[ -f "$SSH_PUBLIC_KEY_FILE" ]] || die "SSH_PUBLIC_KEY_FILE $SSH_PUBLIC_KEY_FILE not found"
        SSH_PRIVATE_KEY="${SSH_PRIVATE_KEY:-${SSH_PUBLIC_KEY_FILE%.pub}}"
        [[ -f "$SSH_PRIVATE_KEY" ]] || die "private key for $SSH_PUBLIC_KEY_FILE not found (set SSH_PRIVATE_KEY)"
    else
        if [[ ! -f "$GENERATED_KEY" ]]; then
            install -d -m 700 /root/.ssh
            ssh-keygen -q -t ed25519 -N "" -C "machine-monitoring-bootstrap@$(hostname)" -f "$GENERATED_KEY"
            ok "generated $GENERATED_KEY"
        fi
        SSH_PUBLIC_KEY_FILE="$GENERATED_KEY.pub"
        SSH_PRIVATE_KEY="$GENERATED_KEY"
    fi
    ok "using $SSH_PUBLIC_KEY_FILE"
}

# ----------------------------------------------------------------------- image
ensure_image() {
    step "Debian cloud image"
    local img; img="$(image_path)"
    install -d "$IMAGE_CACHE_DIR"
    if [[ -f "$img" ]]; then
        ok "cached: $img"
        return
    fi
    curl -fL --retry 3 -o "$img.partial" "$IMAGE_URL"
    local sums expected
    sums="$(curl -fsSL --retry 3 "$IMAGE_SHA512_URL")"
    expected="$(awk -v f="$(basename "$IMAGE_URL")" '$2 == f {print $1}' <<<"$sums")"
    [[ -n "$expected" ]] || { rm -f "$img.partial"; die "no checksum for $(basename "$IMAGE_URL") in $IMAGE_SHA512_URL"; }
    echo "$expected  $img.partial" | sha512sum -c --quiet - || { rm -f "$img.partial"; die "image checksum mismatch"; }
    mv "$img.partial" "$img"
    ok "downloaded and verified $img"
}

# ------------------------------------------------------------- cloud-init snippet
ensure_snippet() {
    step "cloud-init vendor snippet"
    if ! pvesm status -storage "$SNIPPET_STORAGE" -content snippets 2>/dev/null | grep -q "^$SNIPPET_STORAGE "; then
        local content
        content="$(pvesh get "/storage/$SNIPPET_STORAGE" --output-format json | sed -n 's/.*"content":"\([^"]*\)".*/\1/p')"
        [[ -n "$content" ]] || die "cannot read content types of storage '$SNIPPET_STORAGE'"
        warn "enabling 'snippets' content on storage '$SNIPPET_STORAGE' (additive: $content -> $content,snippets)"
        pvesm set "$SNIPPET_STORAGE" --content "$content,snippets"
    fi
    local dir
    dir="$(dirname "$(pvesm path "$SNIPPET_STORAGE:snippets/$SNIPPET_NAME")")"
    install -d "$dir"
    cat >"$dir/$SNIPPET_NAME" <<EOF
#cloud-config
# Managed by machine-monitoring/deploy/bootstrap.sh
timezone: ${TIMEZONE}
ssh_pwauth: false
package_update: true
packages:
  - qemu-guest-agent
runcmd:
  - [systemctl, enable, --now, qemu-guest-agent]
EOF
    ok "$dir/$SNIPPET_NAME"
}

# ------------------------------------------------------------------------- VM
ensure_vm() {
    step "Monitoring VM $VMID ($VM_NAME)"
    if ! vm_exists; then
        local net="virtio,bridge=$VM_BRIDGE"
        [[ -n "$VM_VLAN_TAG" ]] && net="$net,tag=$VM_VLAN_TAG"
        qm create "$VMID" --name "$VM_NAME" --tags machine-monitoring --ostype l26 --machine q35 \
            --cpu host --cores "$VM_CORES" --memory "$VM_MEMORY_MB" --balloon 0 \
            --scsihw virtio-scsi-single --net0 "$net" --agent enabled=1 \
            --serial0 socket --vga serial0 --onboot 1 \
            --description "Nueva machine monitoring platform. Managed by deploy/bootstrap.sh - read-only monitoring, never controls machines."
        ok "created VM $VMID"
    fi
    if [[ -z "$(vm_cfg scsi0)" ]]; then
        qm set "$VMID" --scsi0 "$VM_STORAGE:0,import-from=$(image_path),discard=on,iothread=1,ssd=1" >/dev/null
        ok "imported OS disk"
    fi
    local size_gb
    size_gb="$(vm_cfg scsi0 | sed -n 's/.*size=\([0-9]*\)G.*/\1/p')"
    if [[ -n "$size_gb" ]] && (( size_gb < VM_OS_DISK_GB )); then
        qm resize "$VMID" scsi0 "${VM_OS_DISK_GB}G" >/dev/null
        ok "OS disk resized to ${VM_OS_DISK_GB}G"
    fi
    if [[ -z "$(vm_cfg scsi1)" ]]; then
        qm set "$VMID" --scsi1 "$VM_STORAGE:$VM_DATA_DISK_GB,discard=on,iothread=1,ssd=1" >/dev/null
        ok "added ${VM_DATA_DISK_GB}G data disk"
    fi
    if [[ -z "$(vm_cfg ide2)" ]]; then
        qm set "$VMID" --ide2 "$VM_STORAGE:cloudinit" >/dev/null
        ok "added cloud-init drive"
    fi
    qm set "$VMID" --boot order=scsi0 >/dev/null

    # Cloud-init settings are (re)applied every run; they only act on first boot
    # or when the instance changes, so re-applying is harmless.
    local ipcfg="ip=dhcp"
    [[ "$VM_IP" != "dhcp" ]] && ipcfg="ip=$VM_IP,gw=$VM_GATEWAY"
    local args=(--ciuser "$VM_USER" --sshkeys "$SSH_PUBLIC_KEY_FILE" --ipconfig0 "$ipcfg"
                --cicustom "vendor=$SNIPPET_STORAGE:snippets/$SNIPPET_NAME")
    [[ -n "$VM_DNS" ]] && args+=(--nameserver "$VM_DNS")
    [[ -n "$VM_SEARCHDOMAIN" ]] && args+=(--searchdomain "$VM_SEARCHDOMAIN")
    qm set "$VMID" "${args[@]}" >/dev/null
    ok "cloud-init configured (user $VM_USER, $ipcfg)"

    if [[ "$(qm status "$VMID" | awk '{print $2}')" != "running" ]]; then
        qm start "$VMID"
        ok "started"
    else
        ok "already running"
    fi
}

discover_ip() {
    step "Waiting for VM address"
    if [[ "$VM_IP" != "dhcp" ]]; then
        VM_ADDR="${VM_IP%/*}"
        ok "static address $VM_ADDR"
        return
    fi
    local json
    for _ in $(seq 1 90); do
        if json="$(qm guest cmd "$VMID" network-get-interfaces 2>/dev/null)"; then
            VM_ADDR="$(python3 - "$json" <<'PY'
import json, sys
for nic in json.loads(sys.argv[1]):
    if nic.get("name") == "lo":
        continue
    for a in nic.get("ip-addresses", []):
        if a.get("ip-address-type") == "ipv4" and not a["ip-address"].startswith("169.254."):
            print(a["ip-address"]); sys.exit(0)
PY
)" || true
            [[ -n "${VM_ADDR:-}" ]] && { ok "DHCP address $VM_ADDR"; return; }
        fi
        sleep 5
    done
    die "no address from the QEMU guest agent after 7.5 min. Check DHCP / Internet access of the VM (console: qm terminal $VMID), or set a static VM_IP."
}

wait_ssh() {
    step "Waiting for SSH on $VM_ADDR"
    for _ in $(seq 1 60); do
        if ssh_vm true 2>/dev/null; then
            ok "SSH reachable"
            ssh_vm 'cloud-init status --wait >/dev/null 2>&1 || true'
            ok "cloud-init finished"
            return
        fi
        sleep 5
    done
    die "SSH to $VM_USER@$VM_ADDR not reachable after 5 min"
}

# ---------------------------------------------------------------- provisioning
sync_repo() {
    step "Copying platform to $VM_ADDR:$APP_DIR"
    local version
    version="$(git -C "$REPO_DIR" describe --always --dirty 2>/dev/null || echo unknown)"
    # Secrets (.env) and local config never leave their host; existing files on
    # the VM that are not in the repo (e.g. deploy/docker/.env) are preserved.
    tar -C "$REPO_DIR" --exclude=.git --exclude='deploy/docker/.env' --exclude='deploy/config.env' \
        --exclude='__pycache__' --exclude='.pytest_cache' -czf - . \
        | ssh_vm "sudo install -d -m 755 '$APP_DIR' && sudo tar -xzf - -C '$APP_DIR' --no-same-owner && echo '$version' | sudo tee '$APP_DIR/DEPLOYED_VERSION' >/dev/null"
    ok "version $version"
}

provision() {
    step "Provisioning VM (Docker, data disk, stack)"
    local profiles="observability"
    [[ "$ENABLE_SIMULATOR" == "true" ]] && profiles="simulator,observability"
    ssh_vm "sudo env APP_DIR='$APP_DIR' PUBLIC_URL='http://$VM_ADDR' COMPOSE_PROFILES='$profiles' \
        RDC53_PLC_IP='$RDC53_PLC_IP' TIMEZONE='$TIMEZONE' ADMIN_USER='$VM_USER' \
        bash '$APP_DIR/deploy/vm/provision.sh'"
}

summary() {
    local grafana_pw
    grafana_pw="$(ssh_vm "sudo sed -n 's/^GRAFANA_ADMIN_PASSWORD=//p' '$APP_DIR/deploy/docker/.env'")"
    cat <<EOF

${c_ok}Machine monitoring platform is up.${c_off}

  VM            $VM_NAME (id ${VMID:-n/a}) at $VM_ADDR
  SSH           ssh -i $SSH_PRIVATE_KEY $VM_USER@$VM_ADDR
  Grafana       http://$VM_ADDR/grafana/   (admin / $grafana_pw)
  API           http://$VM_ADDR/api/v1/health
  API docs      http://$VM_ADDR/api/v1/docs
  Secrets       $APP_DIR/deploy/docker/.env on the VM (root only) - back it up off-host

Next steps
  1. Check the simulated machine SIM01 on the "Machine detail" dashboard.
  2. Phase 0: survey the RDC-53 network (docs/network/rdc53-network.md).
  3. Do not enable RDC53 until the read-only Mitsubishi driver (M4) is merged.
EOF
}

# ------------------------------------------------------------------------ main
main() {
    load_config
    if [[ -n "$TARGET_HOST" ]]; then
        VM_ADDR="$TARGET_HOST"
        VMID=""
        ensure_ssh_key
        wait_ssh
        sync_repo
        provision
        summary
        return
    fi
    preflight
    if (( CHECK_ONLY )); then
        printf '\n%sPreflight passed. Nothing was changed.%s\n' "$c_ok" "$c_off"
        return
    fi
    ensure_ssh_key
    ensure_image
    ensure_snippet
    ensure_vm
    discover_ip
    wait_ssh
    sync_repo
    provision
    summary
}

main
