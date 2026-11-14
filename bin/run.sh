#!/usr/bin/env bash
# Run the pipeline on the CURRENT kube context (EKS, kind, Docker Desktop, minikube...).
# Usage: bin/run.sh [repos-file]     -> results + report in runs/<RUN_ID>/
. "$(dirname "$0")/common.sh"
need kubectl python3
REPOS="${1:-$ROOT/config/repos.txt}"
export RUN_ID="${RUN_ID:-$(date +%Y%m%d-%H%M%S)}"
RUN_DIR="${RUN_DIR:-$ROOT/runs/$RUN_ID}"; mkdir -p "$RUN_DIR"
export NAMESPACE PVC_SIZE BUILD_CPU BUILD_MEM BUILD_TIMEOUT_S MAX_PARALLEL DEP_CACHE MAVEN_ARGS_EXTRA GRADLE_ARGS_EXTRA ORCH_IMAGE IMAGE_REGISTRY
if [ -n "$STORAGE_CLASS" ]; then export STORAGE_CLASS_LINE="storageClassName: $STORAGE_CLASS"; else export STORAGE_CLASS_LINE=""; fi
log "run $RUN_ID on context $(kubectl config current-context)"
stamp "$RUN_DIR" run_start

kubectl create namespace "$NAMESPACE" --dry-run=client -o yaml | kubectl apply -f - >/dev/null
k() { kubectl -n "$NAMESPACE" "$@"; }
k create configmap jbp-scripts --from-file="$ROOT/scripts" --dry-run=client -o yaml | kubectl apply -f - >/dev/null
k create configmap jbp-config --from-file=repos.txt="$REPOS" --from-file=images.env="$ROOT/config/images.env" --dry-run=client -o yaml | kubectl apply -f - >/dev/null
k create configmap jbp-templates --from-file="$ROOT/k8s/build-job.tpl.yaml" --dry-run=client -o yaml | kubectl apply -f - >/dev/null
render "$ROOT/k8s/rbac.yaml" | kubectl apply -f - >/dev/null
render "$ROOT/k8s/pvc.yaml" | kubectl apply -f - >/dev/null
k delete job jbp-orchestrator --ignore-not-found --wait=true >/dev/null
k delete pod jbp-collector --ignore-not-found --wait=true >/dev/null
k delete jobs -l app=jbp-build --ignore-not-found >/dev/null
render "$ROOT/k8s/orchestrator-job.yaml" | kubectl apply -f - >/dev/null

log "orchestrator started; streaming its log"
until k get pods -l job-name=jbp-orchestrator -o jsonpath='{.items[0].status.phase}' 2>/dev/null | grep -qE 'Running|Succeeded|Failed'; do sleep 3; done
k logs -f job/jbp-orchestrator || true
while :; do
  st=$(k get job jbp-orchestrator -o jsonpath='{range .status.conditions[*]}{.type}={.status} {end}')
  case "$st" in *Complete=True*|*Failed=True*) break;; esac; sleep 5
done
log "orchestrator finished: $st"
stamp "$RUN_DIR" builds_end

# copy results off the PVC
render "$ROOT/k8s/collector-pod.yaml" | kubectl apply -f - >/dev/null
k wait --for=condition=Ready pod/jbp-collector --timeout=300s >/dev/null
k cp "jbp-collector:/workspace/results" "$RUN_DIR/results" >/dev/null
k delete pod jbp-collector --wait=false >/dev/null
kubectl get nodes -o json > "$RUN_DIR/nodes.json"
{ echo "context=$(kubectl config current-context)"; for v in RUN_ID NAMESPACE BUILD_CPU BUILD_MEM MAX_PARALLEL DEP_CACHE MAVEN_ARGS_EXTRA GRADLE_ARGS_EXTRA; do echo "$v=${!v}"; done; } > "$RUN_DIR/run.env"
stamp "$RUN_DIR" run_end
python3 "$ROOT/report/make_report.py" "$RUN_DIR"
log "report: $RUN_DIR/report.html"
