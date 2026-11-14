#!/usr/bin/env bash
# Tear everything down. Deletes the namespace first so the PVC's EBS volume is released
# (a volume left behind keeps billing after the cluster is gone), then the cluster, then
# checks for leftovers.
. "$(dirname "$0")/common.sh"
need eksctl aws
if eksctl get cluster --name "$CLUSTER_NAME" --region "$AWS_REGION" >/dev/null 2>&1; then
  aws eks update-kubeconfig --name "$CLUSTER_NAME" --region "$AWS_REGION" >/dev/null 2>&1 || true
  log "deleting namespace $NAMESPACE (releases the EBS volume)"
  timeout 300 kubectl delete namespace "$NAMESPACE" --ignore-not-found --wait=true || log "namespace delete timed out, continuing"
  log "deleting cluster $CLUSTER_NAME (~10 min)"
  eksctl delete cluster --name "$CLUSTER_NAME" --region "$AWS_REGION" --wait --disable-nodegroup-eviction
else
  log "cluster $CLUSTER_NAME not found"
fi
"$(dirname "$0")/leftovers.sh"
