#!/usr/bin/env bash
#
# collect-debug.sh - Gather Ansible failure diagnostics from the ZMS Azure control node.
#
# Produces a timestamped tarball under /tmp containing cloud-init logs, the
# Ansible/collection state, config, dynamic-inventory + connectivity checks,
# managed-identity (IMDS) / Key Vault / DNS / egress probes, SSH-key metadata,
# the estate logs, and the systemd unit status/journals.
#
# Usage:
#   ./collect-debug.sh              # collect what the current user can read
#   sudo ./collect-debug.sh         # also capture root-owned logs/keys/journals
#
# SECURITY: the bundle may contain identifiers (subscription id, resource group,
# vault and secret names, identity client id, host names, IPs) but deliberately
# avoids secrets - it never prints the SSH private key or the consolidated Key
# Vault secret value, only the HTTP status of the managed-identity token
# requests (never a token), only the secret's metadata, never runs a playbook or
# `ansible-inventory --list` (which would resolve the Key Vault lookups), and
# runs a final redaction pass. Still, review the tarball before sharing it
# externally.

set -o pipefail

# ---------------------------------------------------------------- config -----
REPO_DIR="${CONTROL_REPO_DIR:-/opt/control-repo}"
ENV_FILE="/etc/ansible/estate.env"
LOG_DIR="/var/log/ansible"
KEY_FILE="/etc/ansible/keys/ansible_rsa"
IMDS="${IMDS:-http://169.254.169.254}"   # overridable for testing
TS="$(date -u +%Y%m%d-%H%M%SZ)"
HOST="$(hostname -s 2>/dev/null || hostname)"
OUT_DIR="/tmp/ansible-debug-${HOST}-${TS}"
REPORT="${OUT_DIR}/report.txt"
mkdir -p "$OUT_DIR"

# System Ansible (installed via apt); include /usr/local/bin just in case.
# Disable the shared log so collection stays read-only and avoids the
# not-writeable warning.
export PATH="/usr/local/bin:/usr/bin:${PATH}"
export ANSIBLE_LOG_PATH="${OUT_DIR}/ansible-collect.log"

# Collecting a bundle must not change the node it is inspecting.
#
# ansible.cfg sets fact_caching = jsonfile with fact_caching_connection =
# /var/tmp/ansible_facts. The systemd units run as User=ubuntu, but this script
# is normally run with sudo — so every ansible/ansible-inventory/ansible-playbook
# probe below would create that directory as root:root the first time, and from
# then on the ubuntu-owned converge cannot write it:
#   [WARNING]: 'jsonfile' cache, configured path (/var/tmp/ansible_facts) does
#   not have necessary permissions (rwx), disabling plugin
# On the AWS node ip-10-188-30-54 that warning appeared 9 times in one converge, and it began
# immediately after the first debug bundle was collected. Fact caching was then
# silently off for every run, so the estate re-gathered facts from scratch each
# time. Pin this process to the in-memory cache so it touches nothing on disk.
export ANSIBLE_CACHE_PLUGIN=memory
[ -r "$ENV_FILE" ] && { set -a; . "$ENV_FILE" 2>/dev/null || true; set +a; }
# export: runsh() below executes `bash -lc`, and the probes reference these as
# \${...} so they expand in that CHILD shell, where a non-exported variable
# does not exist. (On AWS the header printed the region while the probe on the
# same page said it was unset, because of exactly this.)
export ANSIBLE_KEYVAULT_NAME="${ANSIBLE_KEYVAULT_NAME:-}"
export ANSIBLE_SECRET_NAME="${ANSIBLE_SECRET_NAME:-}"
export AZURE_CLIENT_ID="${AZURE_CLIENT_ID:-}"
export ANSIBLE_RESOURCE_GROUP="${ANSIBLE_RESOURCE_GROUP:-}"
if [ -n "$ANSIBLE_RESOURCE_GROUP" ] && [ -z "${ANSIBLE_AZURE_VM_RESOURCE_GROUPS:-}" ]; then
  export ANSIBLE_AZURE_VM_RESOURCE_GROUPS="$ANSIBLE_RESOURCE_GROUP"
fi
export IMDS

# --------------------------------------------------------------- helpers -----
section() { printf '\n\n========== %s ==========\n' "$*" >>"$REPORT"; }
run() {   # run <label> <cmd...>
  local label="$1"; shift
  printf '\n$ %s\n' "$label" >>"$REPORT"
  timeout 60 "$@" >>"$REPORT" 2>&1 || printf '[exit %s / not available]\n' "$?" >>"$REPORT"
}
runsh() { # runsh <label> <shell string>
  local label="$1"; shift
  printf '\n$ %s\n' "$label" >>"$REPORT"
  timeout 120 bash -lc "$*" >>"$REPORT" 2>&1 || printf '[exit %s / not available]\n' "$?" >>"$REPORT"
}
copy() {  # copy <src> [destname]
  local src="$1"; local dst="${2:-$(basename "$1")}"
  # Distinguish "does not exist" from "exists but unreadable". The old code
  # printed "not readable (try sudo)" for both, which is actively misleading:
  # on a node where bootstrap.yml has not run yet, /var/log/ansible/ansible.log
  # simply does not exist, and the bundle reported a permissions problem — as
  # root. That cost real time in the 2026-09-14 review.
  if [ ! -e "$src" ]; then
    printf '  does not exist: %s\n' "$src" >>"$REPORT"
  elif [ ! -r "$src" ]; then
    printf '  exists but not readable (try sudo): %s\n' "$src" >>"$REPORT"
  else
    cp -a "$src" "${OUT_DIR}/${dst}" 2>/dev/null \
      && printf '  collected: %s\n' "$src" >>"$REPORT" \
      || printf '  could not copy: %s\n' "$src" >>"$REPORT"
  fi
}

{
  echo "ZMS Azure control-node Ansible debug bundle"
  echo "generated : $(date -u) (UTC)"
  echo "user      : $(id -un) (uid $(id -u))"
  echo "host      : $(hostname -f 2>/dev/null || hostname)"
  echo "repo_dir  : ${REPO_DIR}"
  echo "location  : ${AZURE_LOCATION:-<unset>}"
  echo "rg        : ${ANSIBLE_RESOURCE_GROUP:-<unset>}"
  echo "vault     : ${ANSIBLE_KEYVAULT_NAME:-<unset>}"
} >"$REPORT"

# 1) host / OS
section "HOST / OS"
run "uname -a" uname -a
copy /etc/os-release os-release
run "uptime" uptime
runsh "disk usage" "df -h / /var /opt 2>/dev/null"
runsh "memory" "free -h 2>/dev/null"

# 2) cloud-init (first-boot control-node bootstrap)
section "CLOUD-INIT"
run "cloud-init status --long" cloud-init status --long
copy /var/log/cloud-init-output.log cloud-init-output.log
copy /var/log/cloud-init.log cloud-init.log

# 3) Ansible + Python environment
section "ANSIBLE / PYTHON ENVIRONMENT"
runsh "which ansible*" "command -v ansible ansible-playbook ansible-galaxy ansible-inventory 2>&1"
run "ansible --version" ansible --version
runsh "pip (ansible/azure/winrm pkgs)" "python3 -m pip list 2>/dev/null | grep -Ei 'ansible|^azure-(identity|core|mgmt-(compute|network|resource|core))|msal|pywinrm|requests|resolvelib|cryptography' || echo 'pip list not available'"
runsh "installed collections" "ansible-galaxy collection list 2>&1"
# Directly diagnose the common import root causes: the azure_rm inventory needs
# azure.azcollection's SDK set (scripts/reconverge.sh installs it), the Key
# Vault lookup needs requests, and the Windows (WinRM) path needs pywinrm.
printf '\n$ python import check (azure SDK / requests / winrm)\n' >>"$REPORT"
timeout 30 python3 - >>"$REPORT" 2>&1 <<'PY' || printf '[python not available]\n' >>"$REPORT"
mods = ['azure.identity', 'azure.mgmt.compute', 'azure.mgmt.network',
        'azure.mgmt.resource', 'requests', 'winrm']
for m in mods:
    try:
        __import__(m)
        print('ok  ', m)
    except Exception as e:
        print('FAIL', m, '->', repr(e))
PY

# 4) configuration
section "CONFIGURATION"
copy "${REPO_DIR}/ansible.cfg" ansible.cfg
copy "${REPO_DIR}/inventory/azure_rm.yml" azure_rm.yml
runsh "azure.azcollection location(s)" "ansible-galaxy collection list azure.azcollection 2>&1 | head -n 20"
runsh "effective config (changed only)" "cd '${REPO_DIR}' && ansible-config dump --only-changed 2>&1 | head -n 80"
if [ -r "$ENV_FILE" ]; then
  printf '\n--- %s (names only; no secrets by design) ---\n' "$ENV_FILE" >>"$REPORT"
  cat "$ENV_FILE" >>"$REPORT" 2>/dev/null
  cp -a "$ENV_FILE" "${OUT_DIR}/estate.env" 2>/dev/null
else
  printf '\n%s not readable (try sudo)\n' "$ENV_FILE" >>"$REPORT"
fi

# 5) dynamic inventory (structure only - never --list/--vars, they resolve creds)
section "INVENTORY (structure only)"
runsh "inventory scope" "printf 'ANSIBLE_AZURE_VM_RESOURCE_GROUPS=[%s] ANSIBLE_ESTATE=[%s] auth=[%s]\n' \"\${ANSIBLE_AZURE_VM_RESOURCE_GROUPS:-}\" \"\${ANSIBLE_ESTATE:-}\" \"\${ANSIBLE_AZURE_AUTH_SOURCE:-}\""
runsh "ansible-inventory --graph" "cd '${REPO_DIR}' && ansible-inventory --graph 2>&1"
# -vv makes the plugin say which VMs it dropped and which filter dropped them.
runsh "inventory exclusions (-vv)" "cd '${REPO_DIR}' && ansible-inventory --graph -vv 2>&1 | grep -E 'excluded by|matched filter|azure_rm' | head -n 60"

# 6) Managed identity via IMDS (HTTP status only, never the token)
section "MANAGED IDENTITY (IMDS)"
runsh "instance metadata (compute: name, rg, subscription, location)" "curl -s -H Metadata:true --noproxy '*' --max-time 10 '${IMDS}/metadata/instance/compute?api-version=2021-02-01' | python3 -c 'import json,sys; d=json.load(sys.stdin); [print(k, \"=\", d.get(k)) for k in (\"name\",\"resourceGroupName\",\"subscriptionId\",\"location\",\"vmSize\")]' 2>&1 || echo 'IMDS unreachable'"
runsh "AZURE_CLIENT_ID set?" "[ -n \"\${AZURE_CLIENT_ID:-}\" ] && echo \"AZURE_CLIENT_ID=\${AZURE_CLIENT_ID}\" || echo 'AZURE_CLIENT_ID NOT set (estate.env not loaded?)'"
# Token requests for the two resources Ansible uses. Status codes only: 200 is
# fine; 400 'Identity not found' means AZURE_CLIENT_ID names an identity that is
# not attached to this VM.
runsh "token for ARM (status only)" "curl -s -o /dev/null -w 'HTTP %{http_code}\n' -H Metadata:true --noproxy '*' --max-time 10 \"${IMDS}/metadata/identity/oauth2/token?api-version=2018-02-01&resource=https://management.azure.com/\${AZURE_CLIENT_ID:+&client_id=\$AZURE_CLIENT_ID}\" || echo 'IMDS unreachable'"
runsh "token for Key Vault (status only)" "curl -s -o /dev/null -w 'HTTP %{http_code}\n' -H Metadata:true --noproxy '*' --max-time 10 \"${IMDS}/metadata/identity/oauth2/token?api-version=2018-02-01&resource=https://vault.azure.net\${AZURE_CLIENT_ID:+&client_id=\$AZURE_CLIENT_ID}\" || echo 'IMDS unreachable'"

# 7) Key Vault: can this identity read the secret? (metadata only - never the value)
section "KEY VAULT (metadata only)"
runsh "vault / secret names" "printf 'ANSIBLE_KEYVAULT_NAME=[%s] ANSIBLE_SECRET_NAME=[%s]\n' \"\${ANSIBLE_KEYVAULT_NAME:-}\" \"\${ANSIBLE_SECRET_NAME:-}\""
# GET .../secrets/<name>?api-version is the value-bearing call, so instead list
# the secret's VERSIONS: same RBAC (Key Vault Secrets User covers both), no
# value in the response, and the body shows 'enabled' and the timestamps.
# 200 = readable; 403 = role missing or not yet propagated; 404 = wrong name.
runsh "secret versions (metadata, status + attributes)" "T=\$(curl -s -H Metadata:true --noproxy '*' --max-time 10 \"${IMDS}/metadata/identity/oauth2/token?api-version=2018-02-01&resource=https://vault.azure.net\${AZURE_CLIENT_ID:+&client_id=\$AZURE_CLIENT_ID}\" | python3 -c 'import json,sys; print(json.load(sys.stdin).get(\"access_token\",\"\"))' 2>/dev/null); [ -n \"\$T\" ] || { echo 'no token'; exit 0; }; curl -s -w '\nHTTP %{http_code}\n' --max-time 15 -H \"Authorization: Bearer \$T\" \"https://\${ANSIBLE_KEYVAULT_NAME}.vault.azure.net/secrets/\${ANSIBLE_SECRET_NAME}/versions?api-version=7.4&maxresults=5\" | head -c 2000"

# 8) DNS + egress
section "DNS / NETWORK EGRESS"
copy /etc/resolv.conf resolv.conf
runsh "resolve pypi" "getent hosts pypi.org || echo 'getent failed (exit '\$?')'"
runsh "resolve ARM" "getent hosts management.azure.com || echo 'getent failed (exit '\$?')'"
runsh "resolve the vault" "if [ -n \"\${ANSIBLE_KEYVAULT_NAME:-}\" ]; then getent hosts \"\${ANSIBLE_KEYVAULT_NAME}.vault.azure.net\" || echo 'no A/AAAA record (getent exit '\$?')'; else echo 'skipped: ANSIBLE_KEYVAULT_NAME empty'; fi"
runsh "egress -> pypi (status)" "curl -s -o /dev/null -w 'HTTP %{http_code}\n' --max-time 12 https://pypi.org/simple/ || echo 'no egress'"
runsh "egress -> ARM (status; 400/401 = reachable)" "curl -s -o /dev/null -w 'HTTP %{http_code}\n' --max-time 12 https://management.azure.com/ || echo 'unreachable (curl exit '\$?')'"
runsh "egress -> vault (status; 401/403 = reachable)" "if [ -n \"\${ANSIBLE_KEYVAULT_NAME:-}\" ]; then curl -s -o /dev/null -w 'HTTP %{http_code}\n' --max-time 12 \"https://\${ANSIBLE_KEYVAULT_NAME}.vault.azure.net/secrets?api-version=7.4\" || echo 'unreachable (curl exit '\$?')'; else echo 'skipped: ANSIBLE_KEYVAULT_NAME empty'; fi"

# 9) SSH key (metadata only, never the contents)
section "SSH KEY (metadata only)"
runsh "key file perms/owner" "ls -l '${KEY_FILE}' 2>&1 || echo 'key file missing (bootstrap.yml not run?)'"
runsh "key fingerprint" "ssh-keygen -lf '${KEY_FILE}' 2>&1 || echo 'cannot read fingerprint'"

# 10) estate logs
section "ANSIBLE LOGS"
copy "${LOG_DIR}/ansible.log" ansible.log
copy "${LOG_DIR}/converge-status.log" converge-status.log
copy "${LOG_DIR}/converge-failures.log" converge-failures.log
runsh "recent failures in ansible.log" "tail -n 600 '${LOG_DIR}/ansible.log' 2>/dev/null | grep -iE 'fatal|failed=|unreachable|error|traceback' | tail -n 120 || echo 'no ansible.log (try sudo)'"

# 11) systemd units + journals
section "SYSTEMD (timers + services)"
run "list-timers" systemctl list-timers "ansible-*" --all --no-pager
for u in ansible-bootstrap ansible-estate; do
  run "status ${u}.service" systemctl status "${u}.service" --no-pager -l
  runsh "journal ${u} (last 200)" "journalctl -u ${u}.service -n 200 --no-pager 2>&1 || echo 'no journal access (try sudo)'"
done

# 12) control repo state + syntax check
section "CONTROL REPO"
runsh "git log/status" "cd '${REPO_DIR}' && git -c safe.directory='${REPO_DIR}' log --oneline -n 5 2>&1; echo '---'; git -c safe.directory='${REPO_DIR}' status -s 2>&1"
runsh "site.yml --syntax-check" "cd '${REPO_DIR}' && ansible-playbook site.yml --syntax-check 2>&1 | tail -n 40"
runsh "orchestrate.yml --syntax-check" "cd '${REPO_DIR}' && ansible-playbook orchestrate.yml --syntax-check 2>&1 | tail -n 40"

# 13) live reachability (default verbosity; credentials are NOT printed)
section "CONNECTIVITY PROBE (best-effort)"
runsh "ping Linux (SSH)" "cd '${REPO_DIR}' && timeout 90 ansible os_linux -m ansible.builtin.ping -o 2>&1 | tail -n 50 || echo 'skipped/failed'"
runsh "ping Windows (WinRM)" "cd '${REPO_DIR}' && timeout 120 ansible os_windows -m ansible.windows.win_ping -o 2>&1 | tail -n 50 || echo 'skipped/failed'"

# ----------------------------------------------- redact + package (safety) ---
# Defense in depth: scrub anything that looks like a token/secret value.
find "$OUT_DIR" -type f -print0 2>/dev/null | xargs -0 -r sed -i -E \
  -e 's/(access_token"?[[:space:]]*[:=][[:space:]]*"?)[A-Za-z0-9._-]+/\1<REDACTED>/g' \
  -e 's/(Bearer )[A-Za-z0-9._+/=-]+/\1<REDACTED>/g' \
  -e 's/([Pp]assword"?[[:space:]]*[:=][[:space:]]*"?)[^",[:space:]]+/\1<REDACTED>/g' \
  -e 's/("?value"?[[:space:]]*:[[:space:]]*")[^"]+/\1<REDACTED>/g' \
  -e 's/(eyJ[A-Za-z0-9_-]{10,}\.)[A-Za-z0-9._-]+/\1<REDACTED>/g' \
  2>/dev/null || true

TARBALL="/tmp/ansible-debug-${HOST}-${TS}.tar"
tar -cf "$TARBALL" -C "$(dirname "$OUT_DIR")" "$(basename "$OUT_DIR")" 2>/dev/null

echo
echo "Ansible debug bundle written:"
echo "  folder : $OUT_DIR"
echo "  tarball: $TARBALL"
echo
echo "Copy it off the node, e.g. from your workstation:"
echo "  scp -i <key>.pem -o ProxyJump=ubuntu@<bastion-ip> ubuntu@$(hostname -s):$TARBALL ."
echo "  # (connection-details.txt next to the Terraform state has the exact command)"
echo
echo "Review before sharing (contains subscription / resource-group / vault identifiers)."
echo "Re-run with sudo to include root-owned logs, keys, and journals."
