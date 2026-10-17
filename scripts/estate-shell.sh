#!/usr/bin/env bash
# estate-shell.sh — run Ansible by hand on the control node with exactly the
# environment the systemd units see.
#
# WHY. /etc/ansible/estate.env is 0640 root:root (Terraform writes it that way):
# systemd reads it as root before dropping to the ubuntu user, but ubuntu
# itself cannot. So a manual `ansible-playbook` run as ubuntu has no vault
# name, no secret name and no resource-group scope, and the azure_rm inventory
# comes back empty or refused. This script reads the file as root, exports it,
# adds ANSIBLE_AZURE_VM_RESOURCE_GROUPS (as converge.sh does), and drops to the
# service user in the repo directory.
#
# Usage (on the control node):
#   sudo /opt/control-repo/scripts/estate-shell.sh                 # interactive shell
#   sudo /opt/control-repo/scripts/estate-shell.sh ansible-inventory --graph
#   sudo /opt/control-repo/scripts/estate-shell.sh ansible-playbook playbooks/zms-app-verify.yml
#   sudo /opt/control-repo/scripts/estate-shell.sh scripts/converge.sh --linux
#
# Runs as ubuntu, never as root: the SSH key, fact cache and log directory are
# owned by ubuntu, and a root run would leave root-owned files behind that the
# next timer run cannot write (see the fact-cache note in collect-debug.sh).
set -euo pipefail

ENV_FILE="${ANSIBLE_ESTATE_ENV:-/etc/ansible/estate.env}"
SERVICE_USER="${ANSIBLE_SERVICE_USER:-ubuntu}"

if [ "$(id -u)" -ne 0 ]; then
  echo "Run with sudo: estate.env is readable by root only." >&2
  exit 1
fi
if [ ! -r "$ENV_FILE" ]; then
  echo "Cannot read $ENV_FILE. Has the control node's cloud-init finished?" >&2
  echo "  sudo cloud-init status --long; sudo tail -n 50 /var/log/zms-control-bootstrap.log" >&2
  exit 1
fi

set -a
# shellcheck disable=SC1090
. "$ENV_FILE"
set +a

if [ -n "${ANSIBLE_RESOURCE_GROUP:-}" ] && [ -z "${ANSIBLE_AZURE_VM_RESOURCE_GROUPS:-}" ]; then
  export ANSIBLE_AZURE_VM_RESOURCE_GROUPS="$ANSIBLE_RESOURCE_GROUP"
fi

REPO_DIR="${CONTROL_REPO_DIR:-/opt/control-repo}"
cd "$REPO_DIR"

USER_HOME="$(getent passwd "$SERVICE_USER" | cut -d: -f6)"
export HOME="${USER_HOME:-/home/$SERVICE_USER}"

# runuser without --login keeps the environment built above.
if [ "$#" -gt 0 ]; then
  exec runuser -u "$SERVICE_USER" -- "$@"
fi
echo "Estate shell: ${ANSIBLE_ESTATE:-?} (resource group ${ANSIBLE_AZURE_VM_RESOURCE_GROUPS:-?}) as $SERVICE_USER in $REPO_DIR. Exit to leave."
exec runuser -u "$SERVICE_USER" -- bash --norc -i
