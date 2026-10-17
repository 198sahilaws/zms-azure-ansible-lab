#!/usr/bin/env bash
# reconverge.sh — systemd-timer entry point for the control node
# (ansible-bootstrap.service, every 30 minutes; first started by the Terraform
# cloud-init). Pulls the control repo, refreshes collections, makes sure the
# Python dependencies of the Azure collection and of WinRM are importable, then
# re-applies bootstrap.yml to itself, which reads the consolidated credentials
# secret from Key Vault and writes the SSH key from it.
set -euo pipefail

# Load the Terraform-injected environment (ANSIBLE_KEYVAULT_NAME,
# ANSIBLE_SECRET_NAME, ANSIBLE_RESOURCE_GROUP, AZURE_*, CONTROL_REPO_DIR) for
# MANUAL runs. Under systemd the unit already injects these
# via EnvironmentFile (read as root), and estate.env is root-owned 0640 — so the
# 'ansible' service user CANNOT read it. Test readability (-r), not existence
# (-f): if unreadable we simply rely on the already-injected environment instead
# of aborting under 'set -e'.
ENV_FILE="${ANSIBLE_ESTATE_ENV:-/etc/ansible/estate.env}"
if [ -r "$ENV_FILE" ]; then
  set -a
  . "$ENV_FILE"
  set +a
fi

# azure_rm reads its resource-group scope from this variable only; see
# scripts/converge.sh and inventory/azure_rm.yml.
if [ -n "${ANSIBLE_RESOURCE_GROUP:-}" ] && [ -z "${ANSIBLE_AZURE_VM_RESOURCE_GROUPS:-}" ]; then
  export ANSIBLE_AZURE_VM_RESOURCE_GROUPS="$ANSIBLE_RESOURCE_GROUP"
fi

REPO_DIR="${CONTROL_REPO_DIR:-/opt/control-repo}"

cd "${REPO_DIR}"

# The control node's bootstrap runs `chmod +x /opt/control-repo/scripts/*.sh`
# after cloning. Those files are 100644 in the repo, so the chmod flips them to
# 100755 and git reports them as modified forever — every debug bundle shows
#   M scripts/collect-debug.sh
#   M scripts/reconverge.sh
# which looks like someone edited the node by hand. It is only the exec bit.
#
# It is not cosmetic, and it has already cost a full estate outage. On the AWS
# control node ip-10-188-30-54 (2026-09-14) origin moved 068e812..87cebeb, that commit touched
# scripts/collect-debug.sh, and the pull refused:
#   error: Your local changes to the following files would be overwritten by merge:
#           scripts/collect-debug.sh
#   Aborting
# Under `set -e` this script then exited 1 before ansible-playbook bootstrap.yml
# ever ran, so /etc/ansible/keys/ansible_rsa was never written and every
# subsequent estate converge failed with UNREACHABLE on all three Linux hosts.
# The reported error named a debug script nobody had edited.
git config core.fileMode false

# Bring the working tree to exactly origin/<branch>, rather than merging into it.
#
# `git pull --ff-only` is the wrong verb for this machine. The control node is a
# pure CONSUMER of the repo — nothing here is ever authored locally, and anything
# that differs is drift to be discarded, not work to be preserved. A pull refuses
# to proceed whenever the tree is dirty for any reason (the exec bit above, a
# half-finished hand edit during debugging, a partially-applied file), which
# converts a cosmetic difference into a total converge failure. reset --hard
# cannot get stuck in that state, and it is what the Terraform cloud-init
# bootstrap script already does for the same repo, so the two entry points now
# behave identically.
#
# If you need to test a local change on the node, expect it to be erased at the
# next timer tick. Commit and push instead.
BRANCH="$(git rev-parse --abbrev-ref HEAD)"
git fetch --prune origin
git reset --hard "origin/${BRANCH}"

# reset --hard rewrites these at their recorded mode, and with core.fileMode
# false that means 644 — so the exec bit the bootstrap applied is stripped on
# every converge. The systemd units are immune (they invoke `/bin/bash <script>`
# deliberately), but a human typing ./scripts/collect-debug.sh is not. Re-apply.
# The permanent fix is to record the bit in git itself, once, from a workstation:
#   git update-index --chmod=+x scripts/*.sh && git commit -m 'mark scripts executable'
# after which this line becomes a no-op.
chmod +x scripts/*.sh 2>/dev/null || true

# Refresh collections. --upgrade so changed version pins actually replace an
# already-installed version (otherwise galaxy leaves the old one in place).
ansible-galaxy collection install --upgrade -r requirements.yml

# --- Python dependencies the control node needs at run time -----------------
#
# 1. azure.azcollection's SDK set (azure-identity, azure-mgmt-compute, ...).
#    The azure_rm inventory plugin cannot load without it. The Terraform
#    cloud-init installs it from collections/ansible_collections/azure/
#    azcollection/requirements.txt, but that path only exists if galaxy put
#    the collection in the repo tree. When the PPA bundle already ships a
#    satisfying azure.azcollection (it does), galaxy skips it, the copy that
#    loads is the dist-packages one, and cloud-init only logs a warning. So
#    find the requirements file next to whichever copy actually loads.
# 2. pywinrm, for every Windows host. Not guaranteed by the PPA package.
#
# Import checks first, so a converged node pays nothing. Installs need root:
# the service user (ubuntu) has passwordless sudo, which bootstrap.yml's
# become already relies on. --break-system-packages because Ubuntu 24.04's
# system Python is externally managed (PEP 668) and PPA Ansible runs on it;
# --ignore-installed so pip does not try to uninstall apt-owned packages.
azure_reqs() {
  local d
  for d in "${REPO_DIR}/collections/ansible_collections/azure/azcollection" \
           $(python3 -c 'import sys; print(" ".join(p + "/ansible_collections/azure/azcollection" for p in sys.path if p))' 2>/dev/null); do
    for f in requirements.txt requirements-azure.txt; do
      [ -f "$d/$f" ] && { echo "$d/$f"; return 0; }
    done
  done
  return 1
}

if ! python3 -c 'import azure.identity, azure.mgmt.compute, azure.mgmt.network, azure.mgmt.resource' 2>/dev/null; then
  if REQS="$(azure_reqs)"; then
    echo "Installing azure.azcollection Python requirements from ${REQS}"
    sudo -n python3 -m pip install --break-system-packages --ignore-installed -r "$REQS"
  else
    echo "ERROR: azure.azcollection is not installed anywhere Python can see;" \
         "the azure_rm inventory and Key Vault lookups will not work." >&2
    exit 1
  fi
fi

if ! python3 -c 'import winrm' 2>/dev/null; then
  echo "Installing python3-winrm for the Windows estate"
  sudo -n env DEBIAN_FRONTEND=noninteractive apt-get -o DPkg::Lock::Timeout=300 install -y python3-winrm \
    || sudo -n python3 -m pip install --break-system-packages pywinrm
fi

# The Key Vault lookup in bootstrap.yml authenticates with the control node's
# user-assigned identity (AZURE_CLIENT_ID), which holds Key Vault Secrets User
# on the estate vault and nothing else.
ansible-playbook bootstrap.yml
