#!/usr/bin/env bash
# Free local Kubernetes rehearsal of the real flow (needs Docker + kind).
#   bin/local-kind.sh up | run [repos-file] | down
. "$(dirname "$0")/common.sh"
need kind kubectl
case "${1:-run}" in
  up)   kind create cluster --name jbp-local --wait 120s ;;
  run)  shift || true; kubectl config use-context kind-jbp-local >/dev/null
        IMAGE_REGISTRY="${IMAGE_REGISTRY-}" STORAGE_CLASS="${STORAGE_CLASS:-standard}" BUILD_CPU="${BUILD_CPU:-1}" BUILD_MEM="${BUILD_MEM:-3Gi}" \
          "$ROOT/bin/run.sh" "$@" ;;
  down) kind delete cluster --name jbp-local ;;
esac
