#!/usr/bin/env bash
# systemd ExecStopPost hook. Records each converge's result to a persistent log,
# and on failure to a dedicated failures log (so a log collector or cron can
# watch a single file) and optionally POSTs to a webhook. Called with one arg:
# the label ("bootstrap" or "estate"). Always exits 0 so it never alters the
# unit's own result.
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

  # Optional alert: ANSIBLE_ALERT_WEBHOOK_URL, which Terraform writes into
  # estate.env from the deployment's alert_webhook_url variable. On AWS the
  # equivalent (an SNS topic ARN) was read here but never written by
  # cloud-init, so failure alerting was silently off on every node; on Azure
  # the variable is part of the contract, and an empty value means "local
  # logs only".
  #
  # The body is a small JSON document with a "text" field, which Slack and
  # Teams incoming webhooks render as a message, and which an Azure Logic App
  # or Monitor action group webhook receives as-is. json.dumps does the
  # escaping, so a quote or newline in the log tail cannot break the payload.
  # A failed POST is recorded and otherwise ignored.
  if [ -n "${ANSIBLE_ALERT_WEBHOOK_URL:-}" ] && command -v curl >/dev/null 2>&1; then
    TAIL="$(tail -n 20 "$LOG_DIR/converge-failures.log" 2>/dev/null)"
    BODY="$(LABEL="$LABEL" HOST="$HOST" TS="$TS" RESULT="$RESULT" STATUS="$STATUS" TAIL="$TAIL" \
      ESTATE="${ANSIBLE_ESTATE:-}" RG="${ANSIBLE_RESOURCE_GROUP:-}" python3 -c '
import json, os
e = os.environ
print(json.dumps({
    "text": "Ansible %s converge FAILED on %s (estate %s, exit %s)" % (e["LABEL"], e["HOST"], e["ESTATE"] or "?", e["STATUS"]),
    "label": e["LABEL"], "host": e["HOST"], "estate": e["ESTATE"],
    "resource_group": e["RG"], "result": e["RESULT"], "exit_status": e["STATUS"],
    "timestamp": e["TS"], "recent_failures": e["TAIL"],
}))' 2>/dev/null)"
    if [ -n "$BODY" ]; then
      HTTP="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 15 \
        -H 'Content-Type: application/json' -d "$BODY" "$ANSIBLE_ALERT_WEBHOOK_URL" 2>/dev/null)" || HTTP="curl-error"
      case "$HTTP" in
        2??) ;;
        *) echo "$TS ${LABEL} webhook alert not delivered (HTTP ${HTTP})" \
             >> "$LOG_DIR/converge-failures.log" 2>/dev/null || true ;;
      esac
    fi
  fi
fi
exit 0
