#!/usr/bin/env bash
# List (and with --delete, remove) billable things a run can leave behind.
. "$(dirname "$0")/common.sh"
need aws
vols=$(aws ec2 describe-volumes --region "$AWS_REGION" --filters "Name=tag:jbp-cluster,Values=$CLUSTER_NAME" \
  --query 'Volumes[].[VolumeId,State,Size]' --output text)
clusters=$(aws eks list-clusters --region "$AWS_REGION" --query 'clusters' --output text | tr '\t' '\n' | grep -x "$CLUSTER_NAME" || true)
stacks=$(aws cloudformation list-stacks --region "$AWS_REGION" --stack-status-filter CREATE_COMPLETE UPDATE_COMPLETE DELETE_FAILED \
  --query "StackSummaries[?starts_with(StackName, 'eksctl-${CLUSTER_NAME}-')].StackName" --output text)
[ -z "$vols$clusters$stacks" ] && { log "no leftovers for $CLUSTER_NAME in $AWS_REGION"; exit 0; }
echo "LEFTOVERS (still billing):"; [ -n "$clusters" ] && echo " cluster: $clusters"
[ -n "$stacks" ] && echo " stacks: $stacks"; [ -n "$vols" ] && echo " volumes:" && echo "$vols"
if [ "${1:-}" = --delete ]; then
  for v in $(echo "$vols" | awk '$2=="available"{print $1}'); do aws ec2 delete-volume --region "$AWS_REGION" --volume-id "$v" && echo "deleted $v"; done
fi
exit 1
