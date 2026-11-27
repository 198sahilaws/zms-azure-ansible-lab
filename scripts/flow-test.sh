#!/usr/bin/env bash
# flow-test.sh - run the ZMS flow tests in one command. Manual use only (no timer).
#
#   learn    Allowed flows only: F1-F3, F5-F10 and the internet sites, 6 rounds 30 s apart.
#            Safe while ZMS learns flows for policy recommendations.
#   verify   Allowed AND denied flows (N1-N6), once. A denied flow that connects fails the run.
#            Run after the ZMS policy is enforced.
#
# Usage (on the control node):
#   sudo /opt/control-repo/scripts/flow-test.sh learn
#   sudo /opt/control-repo/scripts/flow-test.sh verify
#   sudo /opt/control-repo/scripts/flow-test.sh learn -e flow_rounds=12      # extra ansible-playbook args
#   FLOW_SKIP=flow-bastion sudo -E /opt/control-repo/scripts/flow-test.sh learn   # skip playbooks
#
# If the hourly converge or the repo refresh is running (it holds .git/zms-ansible.lock), the
# script says which, waits for it to finish (FLOW_LOCK_WAIT seconds, default 1800; 0 = do not
# wait), then runs the tests. Each playbook runs as its own ansible-playbook process through
# estate-shell.sh, like converge.sh. Results: /var/log/ansible/flow-exercise.log (run=<id>).

set -uo pipefail

REPO="${CONTROL_REPO_DIR:-/opt/control-repo}"
LOG="${FLOW_LOG_FILE:-/var/log/ansible/flow-exercise.log}"

usage() {
    sed -n '2,19p' "$0" | sed 's/^# \{0,1\}//'
    exit 2
}

phase="${1:-}"
case "$phase" in
    learn | verify) shift ;;
    -h | --help | "") usage ;;
    *) echo "Unknown phase '$phase' (use learn or verify)" >&2; usage ;;
esac

if [ "$(id -u)" -ne 0 ]; then
    exec sudo -- "$0" "$phase" "$@"
fi

SERVICE_USER="${ANSIBLE_SERVICE_USER:-ubuntu}"
LOCK_FILE="${ANSIBLE_LOCK_FILE:-$REPO/.git/zms-ansible.lock}"
LOCK_WAIT="${FLOW_LOCK_WAIT:-1800}"
ignore_lock=0
for arg in "$@"; do
    [[ "$arg" == *flow_ignore_lock=true* ]] && ignore_lock=1
done

# Wait until no converge or repo refresh holds the run lock. The check runs as the service user,
# like the playbooks' own pre-flight. flock exits 75 only for "held by someone else", so a lock
# file this user cannot open is reported as such, not as a running converge.
wait_for_lock() {
    [ "$ignore_lock" -eq 1 ] && return 0
    local out rc holders="" unit
    out="$(runuser -u "$SERVICE_USER" -- flock --nonblock --conflict-exit-code 75 "$LOCK_FILE" true 2>&1)"
    rc=$?
    [ "$rc" -eq 0 ] && return 0
    if [ "$rc" -ne 75 ]; then
        echo "Cannot check the run lock $LOCK_FILE as $SERVICE_USER (flock rc=$rc): $out" >&2
        echo "Check its owner and mode: ls -l $LOCK_FILE (it should belong to $SERVICE_USER)." >&2
        return 1
    fi
    for unit in ansible-estate.service ansible-bootstrap.service; do
        [ "$(systemctl is-active "$unit" 2>/dev/null)" = activating ] && holders+="$unit "
    done
    case "$holders" in
        *ansible-estate*)     echo "The estate converge (ansible-estate.service) is running." ;;
        *ansible-bootstrap*)  echo "The repo refresh (ansible-bootstrap.service) is running." ;;
        *)                    echo "Another Ansible run holds $LOCK_FILE (a manual converge.sh, or a unit just finishing)." ;;
    esac
    if [ "$LOCK_WAIT" -le 0 ]; then
        echo "Not waiting (FLOW_LOCK_WAIT=0). Run again when it has finished: systemctl is-active ansible-estate.service ansible-bootstrap.service" >&2
        return 75
    fi
    echo "Waiting up to $(( (LOCK_WAIT + 59) / 60 )) min for it to finish (Ctrl-C to stop) ... started $(date +%H:%M:%S)"
    out="$(runuser -u "$SERVICE_USER" -- flock --wait "$LOCK_WAIT" --conflict-exit-code 75 "$LOCK_FILE" true 2>&1)"
    rc=$?
    if [ "$rc" -eq 75 ]; then
        echo "Still held after $(( (LOCK_WAIT + 59) / 60 )) min; not running the tests. Check: journalctl -u ansible-estate.service -n 50" >&2
        return 75
    elif [ "$rc" -ne 0 ]; then
        echo "Cannot check the run lock $LOCK_FILE (flock rc=$rc): $out" >&2
        return 1
    fi
    echo "Lock free at $(date +%H:%M:%S); continuing."
}

playbooks=(flow-exercise flow-internet flow-bastion)
[ "$phase" = verify ] && playbooks+=(flow-negative)

run_id="$(date +%Y%m%dT%H%M%S)"
skip=" ${FLOW_SKIP:-} "
declare -a summary=()
failed=0

echo "Flow tests: phase=$phase run=$run_id"
for pb in "${playbooks[@]}"; do
    if [[ "$skip" == *" $pb "* ]]; then
        summary+=("$(printf '%-14s skipped' "$pb")")
        continue
    fi
    # Checked before every playbook: the 30-minute refresh can start between two of them.
    wait_for_lock
    lock_rc=$?
    if [ "$lock_rc" -ne 0 ]; then
        summary+=("$(printf '%-14s not run (run lock)' "$pb")")
        failed=1
        break
    fi
    echo
    echo "==================== $pb ($phase) ===================="
    (cd "$REPO" && /bin/bash "$REPO/scripts/estate-shell.sh" ansible-playbook "playbooks/$pb.yml" \
        -e "flow_phase=$phase" -e "flow_run_id=$run_id" "$@")
    rc=$?
    if [ "$rc" -eq 0 ]; then
        summary+=("$(printf '%-14s ok' "$pb")")
    else
        summary+=("$(printf '%-14s FAILED (rc=%s)' "$pb" "$rc")")
        failed=1
    fi
done

echo
echo "==================== summary: phase=$phase run=$run_id ===================="
printf '%s\n' "${summary[@]}"
if [ -r "$LOG" ]; then
    echo
    echo "Probes by flow and verdict:"
    grep -F " run=$run_id " "$LOG" \
        | sed -n 's/.* verdict=\([A-Z]*\) flow=\([A-Z0-9]*\) .*/\2 \1/p' \
        | sort | uniq -c | awk '{printf "  %-4s %-5s %s\n", $2, $3, $1}'
    echo
    echo "Failures:"
    grep -F " run=$run_id " "$LOG" | grep -F " verdict=FAIL " || echo "  none"
fi

exit "$failed"
