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
echo "$TS ${LABEL} result=${RESULT} exit=${STATUS} host=${HOST}" >> "$LOG_DIR/converge-status.log" 2>/dev/null || true

if [ "$RESULT" != "success" ]; then
  echo "$TS ${LABEL} FAILED (exit=${STATUS}) — inspect: journalctl -u ansible-${LABEL}.service" \
    >> "$LOG_DIR/converge-failures.log" 2>/dev/null || true
fi
exit 0
