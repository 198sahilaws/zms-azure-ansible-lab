#!/usr/bin/env bash
# converge.sh — the estate convergence runner (what ansible-estate.service runs).
#
# WHY THIS EXISTS (instead of pointing systemd straight at orchestrate.yml):
# orchestrate.yml chains playbooks with import_playbook, so they share ONE
# ansible-playbook process. If any play ends with all its hosts failed, Ansible
# prints "NO MORE HOSTS LEFT" and aborts the whole run — every later playbook is
# skipped. In practice one broken role (or unreachable Windows) stopped the
# entire estate from converging.
#
# This runner executes each playbook as its OWN ansible-playbook invocation, so a
# failure is contained: it is recorded, and the remaining playbooks still run.
# The script exits non-zero at the end if anything failed, so systemd still
# reports the unit as failed and notify-result.sh logs/alerts — but only AFTER
# giving every role a chance to converge.
#
# Usage:
#   ./converge.sh                 # full estate
#   ./converge.sh --linux         # Linux chain only
#   ./converge.sh --windows       # Windows chain only
set -o pipefail

# Load the Terraform-injected env for manual runs. Under systemd the unit already
# provides these via EnvironmentFile, and estate.env is root-owned 0640 (the
# service user cannot read it) — so test readability, never abort on it.
ENV_FILE="${ANSIBLE_ESTATE_ENV:-/etc/ansible/estate.env}"
if [ -r "$ENV_FILE" ]; then
  set -a; . "$ENV_FILE"; set +a
fi

# The azure_rm inventory plugin reads the resource-group scope from this
# variable only (it does not template its config file). Terraform writes the
# estate's resource group as ANSIBLE_RESOURCE_GROUP; copy it across unless the
# caller already chose a scope. See inventory/azure_rm.yml.
if [ -n "${ANSIBLE_RESOURCE_GROUP:-}" ] && [ -z "${ANSIBLE_AZURE_VM_RESOURCE_GROUPS:-}" ]; then
  export ANSIBLE_AZURE_VM_RESOURCE_GROUPS="$ANSIBLE_RESOURCE_GROUP"
fi

REPO_DIR="${CONTROL_REPO_DIR:-/opt/control-repo}"
cd "$REPO_DIR" || { echo "FATAL: cannot cd to $REPO_DIR"; exit 1; }

# Blast-radius controls, overridable from the unit or the environment.
#
# Read the two together. max_fail_percentage aborts a play when the failed
# share of the CURRENT BATCH is strictly greater than the percentage, and
# serial rounds the batch size UP. With a 25% batch on an estate this size the
# batch was two or three hosts, so one failure was 33-50% and always exceeded
# 20: MAX_FAIL_PCT was decorative. At 50% of nine hosts the batch is five and
# one failure is exactly 20%, which is tolerated. (AWS fix pass 2026-10-15.)
# The five-host Windows pool in deployments/adds still aborts on a single
# failure; that is accepted, because the Windows chain is the AD chain.
ROLLING_BATCH="${ROLLING_BATCH:-50%}"
MAX_FAIL_PCT="${MAX_FAIL_PCT:-20}"

# Baseline first, then the Linux role services (independent of AD), then the
# Windows AD chain. Same order and intent as orchestrate.yml — see the design
# rules documented there. Full OS upgrades (ubuntu-setup) are deliberately NOT
# here: they reboot hosts and belong in a maintenance window.
LINUX_PLAYS=(
  # MUST be first: RHEL 8 ships Python 3.6, which ansible-core cannot use on a
  # managed node. This raw-based playbook installs python3.11 before any
  # Python-dependent task (including fact gathering) is attempted.
  playbooks/rhel8-python-bootstrap.yml
  site.yml
  playbooks/ubuntu-apache2.yml
  playbooks/sles-apache2.yml
  playbooks/rhel-httpd.yml
  playbooks/ubuntu-mysql.yml
  playbooks/sles-mariadb.yml
  playbooks/rhel-mariadb.yml
  playbooks/linux-fileshare.yml
  playbooks/linux-client.yml
  # --- ZMS microservices demo application ----------------------------------
  # Must follow the *-mariadb plays above: zms-app-db.yml expects
  # mariadb.service to already exist on the hosts it prepares.
  #
  # These three are the reason the app self-heals unattended. THIS array -- not
  # orchestrate.yml -- is what the hourly ansible-estate timer actually runs, so
  # a playbook missing here never runs on a schedule no matter what
  # orchestrate.yml imports. Keep the two lists in step when adding a playbook.
  #
  # All three are idempotent and fast on a converged estate: the seeder no-ops
  # once rows exist and pip no-ops once the venv is built. To stop deploying the
  # demo app on a schedule, delete these three lines; nothing else depends on
  # them.
  playbooks/zms-app-db.yml
  playbooks/zms-app-services.yml
  playbooks/zms-app-frontend.yml
  # --- Service desk (Flask + MySQL across the Ubuntu web/middleware/db trio) -
  # Must follow ubuntu-mysql.yml above: servicedesk-db.yml expects mysql.service
  # to already exist on the host it prepares. db -> middleware -> web -> client
  # is the only valid order, and each step depends on the one before it:
  #   db          creates the schema and the grant for the middleware host
  #   middleware  creates the tables and seeds them; it holds the credentials
  #   web         renders pages from the middleware over HTTP on :8091
  #   client      sends traffic at the web tier on :8090
  # Run web before middleware and its health check reports a 503 naming an
  # unreachable middleware — correct, but a wasted converge.
  #
  # NOT servicedesk-app.yml. That file still exists as a compatibility alias
  # that imports the middleware and web plays; listing it here as well would
  # deploy both tiers twice on every converge.
  #
  # All four are idempotent and fast on a converged estate: CREATE TABLE IF
  # NOT EXISTS is a no-op, pip no-ops once the venv is built, and the seeder
  # exits without writing once tickets exist. To stop deploying the service
  # desk on a schedule, delete these four lines; nothing else depends on them.
  playbooks/servicedesk-db.yml
  playbooks/servicedesk-middleware.yml
  playbooks/servicedesk-web.yml
  playbooks/servicedesk-client.yml
  # NOT playbooks/servicedesk-verify.yml -- it ends in asserts, so it would
  # mark the unit failed during a deliberate failure demo. Run it by hand.
  # NOT playbooks/zms-app-verify.yml -- it ends in an assert, so it would mark
  # the unit failed during a deliberate failure demo. Run it by hand.
)
WINDOWS_PLAYS=(
  playbooks/windows-adds.yml
  # Additional WRITABLE domain controllers (role_adc). Must follow windows-adds
  # (it promotes into the forest that play creates) and precede windows-rodc (an
  # RODC replicates from a writable DC). NOT the same as role_dc: windows-adds
  # CREATES a forest on every role_dc host, so a second Role=dc host would build
  # a second forest rather than joining the first.
  playbooks/windows-adc.yml
  playbooks/windows-domain-join.yml
  playbooks/windows-rodc.yml
  playbooks/windows-iis.yml
  playbooks/windows-share.yml
  playbooks/windows-python.yml
  playbooks/windows-client.yml
)

case "${1:-}" in
  --linux)   PLAYS=("${LINUX_PLAYS[@]}") ;;
  --windows) PLAYS=("${WINDOWS_PLAYS[@]}") ;;
  *)         PLAYS=("${LINUX_PLAYS[@]}" "${WINDOWS_PLAYS[@]}") ;;
esac

FAILED=()
PASSED=()

for play in "${PLAYS[@]}"; do
  [ -f "$play" ] || { echo "SKIP (missing): $play"; continue; }
  echo "=============================================================="
  echo ">>> $play"
  echo "=============================================================="
  if ansible-playbook "$play" \
        -e "rolling_batch=${ROLLING_BATCH}" \
        -e "max_fail_pct=${MAX_FAIL_PCT}"; then
    PASSED+=("$play")
  else
    rc=$?
    echo "!!! FAILED (exit ${rc}): $play — continuing with the remaining playbooks"
    FAILED+=("$play")
  fi
done

echo
echo "================= CONVERGE SUMMARY ================="
printf 'passed : %s\n' "${#PASSED[@]}"
for p in "${PASSED[@]}"; do printf '   ok   %s\n' "$p"; done
printf 'failed : %s\n' "${#FAILED[@]}"
for p in "${FAILED[@]}"; do printf '   FAIL %s\n' "$p"; done
echo "==================================================="

# Non-zero if anything failed, so systemd + notify-result.sh still flag it.
[ "${#FAILED[@]}" -eq 0 ] || exit 1
exit 0
