#!/usr/bin/env bash
# One shot: cluster up -> builds -> report -> cluster down. Teardown runs even if a step fails
# or you press Ctrl-C. Pass --keep to leave the cluster up (remember bin/down.sh).
. "$(dirname "$0")/common.sh"
KEEP=no; [ "${1:-}" = --keep ] && { KEEP=yes; shift; }
export RUN_ID="${RUN_ID:-$(date +%Y%m%d-%H%M%S)}"; export RUN_DIR="$ROOT/runs/$RUN_ID"; mkdir -p "$RUN_DIR"
teardown() {
  rc=$?
  if [ "$KEEP" = no ]; then
    stamp "$RUN_DIR" down_start; "$ROOT/bin/down.sh" || log "teardown reported a problem: run bin/leftovers.sh"
    stamp "$RUN_DIR" down_end
  fi
  [ -f "$RUN_DIR/results/_orchestrator_done" ] && python3 "$ROOT/report/make_report.py" "$RUN_DIR" >/dev/null && log "final report (with teardown time): $RUN_DIR/report.html"
  exit $rc
}
trap teardown EXIT INT TERM
stamp "$RUN_DIR" up_start
"$ROOT/bin/up.sh"
stamp "$RUN_DIR" up_end
{ echo "INSTANCE_TYPES=$INSTANCE_TYPES"; echo "NODE_COUNT=$NODE_COUNT"; echo "AWS_REGION=$AWS_REGION"; } > "$RUN_DIR/cluster.env"
STORAGE_CLASS="${STORAGE_CLASS:-jbp-gp3}" "$ROOT/bin/run.sh" "$@"
