#!/usr/bin/env bash
# flow-test.sh - run the ZMS flow tests in one command. Manual use only (no timer).
#
#   learn    Allowed flows only: F1-F3, F5-F10 and the internet sites, 6 rounds 30 s apart.
#            Safe while ZMS learns flows for policy recommendations.
#   verify   Allowed AND denied flows (N1-N7), once. A denied flow that connects fails the run.
#            Run after the ZMS policy is enforced.
#
# Usage (on the control node):
#   sudo /opt/control-repo/scripts/flow-test.sh learn
#   sudo /opt/control-repo/scripts/flow-test.sh verify
#   sudo /opt/control-repo/scripts/flow-test.sh learn -e flow_rounds=12      # extra ansible-playbook args
#   FLOW_SKIP=flow-bastion sudo -E /opt/control-repo/scripts/flow-test.sh learn   # skip playbooks
#
# Each playbook runs as its own ansible-playbook process through estate-shell.sh, like
# converge.sh. Results: /var/log/ansible/flow-exercise.log (every line carries run=<id>).

set -uo pipefail

REPO="${CONTROL_REPO_DIR:-/opt/control-repo}"
LOG="${FLOW_LOG_FILE:-/var/log/ansible/flow-exercise.log}"

usage() {
    sed -n '2,17p' "$0" | sed 's/^# \{0,1\}//'
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
