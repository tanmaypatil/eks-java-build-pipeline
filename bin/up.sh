#!/usr/bin/env bash
# Create an ephemeral EKS cluster: public subnets only (no NAT gateway), one spot managed node group,
# EBS CSI driver for the workspace PVC. ~15-20 min.
. "$(dirname "$0")/common.sh"
need eksctl aws kubectl python3

if [ "$K8S_VERSION" = auto ]; then
  K8S_VERSION=$(aws eks describe-cluster-versions --region "$AWS_REGION" --version-status STANDARD_SUPPORT \
      --query 'clusterVersions[].clusterVersion' --output text 2>/dev/null | tr '\t' '\n' | sort -V | tail -1 || true)
  [ -z "$K8S_VERSION" ] && { echo "Could not look up EKS versions; set K8S_VERSION (one in standard support)"; exit 1; }
  log "using EKS $K8S_VERSION (newest in standard support)"
fi

if eksctl get cluster --name "$CLUSTER_NAME" --region "$AWS_REGION" >/dev/null 2>&1; then
  log "cluster $CLUSTER_NAME already exists, reusing"
else
  types=$(echo "$INSTANCE_TYPES" | sed 's/,/", "/g')
  cfg=$(mktemp)
  cat > "$cfg" <<YAML
apiVersion: eksctl.io/v1alpha5
kind: ClusterConfig
metadata:
  name: ${CLUSTER_NAME}
  region: ${AWS_REGION}
  version: "${K8S_VERSION}"
  tags: {project: java-build-perf, jbp-ephemeral: "true"}
iam:
  withOIDC: true
vpc:
  nat: {gateway: Disable}          # NAT gateway is ~\$0.045/h + data: nodes use public subnets instead
  clusterEndpoints: {publicAccess: true, privateAccess: true}
addons:
  - {name: vpc-cni}
  - {name: coredns}
  - {name: kube-proxy}
  - name: aws-ebs-csi-driver
    wellKnownPolicies: {ebsCSIController: true}
managedNodeGroups:
  - name: build-spot
    amiFamily: AmazonLinux2023
    spot: true
    instanceTypes: ["${types}"]
    desiredCapacity: ${NODE_COUNT}
    minSize: 0
    maxSize: $(( NODE_COUNT + 2 ))
    volumeSize: ${NODE_DISK_GB}
    volumeType: gp3
    privateNetworking: false
    labels: {jbp/pool: build}
YAML
  log "creating cluster $CLUSTER_NAME in $AWS_REGION (this takes ~15-20 min)"
  eksctl create cluster -f "$cfg"
  rm -f "$cfg"
fi
aws eks update-kubeconfig --name "$CLUSTER_NAME" --region "$AWS_REGION" >/dev/null
export CLUSTER_NAME; render "$ROOT/k8s/storageclass-eks.yaml" | kubectl apply -f -
kubectl get nodes -o wide
log "cluster ready. Use STORAGE_CLASS=jbp-gp3 for runs (bin/all.sh sets it)."
