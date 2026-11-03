#!/usr/bin/env bash
# systemd ExecStopPost hook. Records each converge's result to a persistent log,
# and on failure to a dedicated failures log, both under /var/log/ansible.
# Local logs only: there is no alerting (no webhook, no SNS), by design.
# Called with one arg: the label ("bootstrap" or "estate"). Always exits 0 so
# it never alters the unit's own result.
set -u

LABEL="${1:-ansible}"
LOG_DIR="${ANSIBLE_LOG_DIR:-/var/log/ansible}"
RESULT="${SERVICE_RESULT:-unknown}"   # systemd: 'success' or e.g. 'exit-code'
STATUS="${EXIT_STATUS:-?}"            # systemd: numeric exit status
TS="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
HOST="$(hostname)"

mkdir -p "$LOG_DIR" 2>/dev/null || true

# Hosts the run skipped as unreachable, written by converge.sh at the end of a
# timed run. Read once and deleted, so it never carries over to the next run.
# A run with skipped hosts is logged as result=partial (or keeps its failure
# result), because the playbooks deliberately exit 0 when a host is
# unreachable and systemd would otherwise report a plain success.
UNREACHABLE_HOSTS=""
MARKER="$LOG_DIR/.${LABEL}-last-run"
if [ -r "$MARKER" ]; then
  UNREACHABLE_HOSTS="$(sed -n 's/^UNREACHABLE_HOSTS=//p' "$MARKER" | head -n 1)"
  rm -f "$MARKER" 2>/dev/null || true
fi
EXTRA=""
if [ -n "$UNREACHABLE_HOSTS" ]; then
  [ "$RESULT" = "success" ] && RESULT="partial"
  EXTRA=" unreachable=${UNREACHABLE_HOSTS}"
fi

echo "$TS ${LABEL} result=${RESULT} exit=${STATUS} host=${HOST}${EXTRA}" >> "$LOG_DIR/converge-status.log" 2>/dev/null || true

if [ "$RESULT" = "partial" ]; then
  echo "$TS ${LABEL} PARTIAL: skipped unreachable host(s) ${UNREACHABLE_HOSTS} — inspect: grep UNREACHABLE ${LOG_DIR}/ansible.log" \
    >> "$LOG_DIR/converge-failures.log" 2>/dev/null || true
elif [ "$RESULT" != "success" ]; then
  echo "$TS ${LABEL} FAILED (exit=${STATUS})${EXTRA} — inspect: journalctl -u ansible-${LABEL}.service" \
    >> "$LOG_DIR/converge-failures.log" 2>/dev/null || true
fi
exit 0
