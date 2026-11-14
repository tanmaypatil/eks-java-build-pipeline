#!/usr/bin/env bash
# No cluster at all: run the same orchestrator + build script with Docker, a local folder
# standing in for the PVC. Good for testing repo lists and detection for free.
# Usage: bin/local-docker.sh [repos-file]
. "$(dirname "$0")/common.sh"
need docker git jq python3
REPOS="${1:-$ROOT/config/repos.txt}"
export RUN_ID="${RUN_ID:-$(date +%Y%m%d-%H%M%S)}"; RUN_DIR="$ROOT/runs/$RUN_ID"; mkdir -p "$RUN_DIR"
export WORKSPACE="${WORKSPACE:-$RUN_DIR/workspace}"; mkdir -p "$WORKSPACE"
export LAUNCHER=docker REPOS_FILE="$REPOS" IMAGES_FILE="$ROOT/config/images.env" SCRIPTS_DIR="$ROOT/scripts"
export IMAGE_REGISTRY="${LOCAL_IMAGE_REGISTRY-}"    # Docker Hub locally
export BUILD_CPU BUILD_MEM MAX_PARALLEL DEP_CACHE MAVEN_ARGS_EXTRA GRADLE_ARGS_EXTRA
stamp "$RUN_DIR" run_start
bash "$ROOT/scripts/orchestrator.sh"
stamp "$RUN_DIR" builds_end
ln -sfn "$WORKSPACE/results" "$RUN_DIR/results"
{ echo "context=local-docker"; for v in RUN_ID BUILD_CPU BUILD_MEM MAX_PARALLEL DEP_CACHE MAVEN_ARGS_EXTRA GRADLE_ARGS_EXTRA; do echo "$v=${!v}"; done; } > "$RUN_DIR/run.env"
stamp "$RUN_DIR" run_end
python3 "$ROOT/report/make_report.py" "$RUN_DIR"
log "report: $RUN_DIR/report.html"
