"""EKS cluster lifecycle: create, delete (with verification), and a leftovers check.

Called by the jbp CLI; needs aws, eksctl and kubectl on PATH and AWS credentials in the environment.
"""
from __future__ import annotations

import json
import logging
import tempfile
import time
from pathlib import Path

from jbpcore import CommandError, JbpError, Settings, StageTracker, fmt_duration, run, run_streaming

log = logging.getLogger("cluster")
ROOT = Path(__file__).resolve().parent.parent


def aws(cfg: Settings, *args, **kw):
    return run(["aws", "--region", cfg["AWS_REGION"], *args], log=log, **kw)


def check_credentials(cfg: Settings) -> str:
    try:
        ident = json.loads(aws(cfg, "sts", "get-caller-identity", "--output", "json", timeout=30).stdout)
    except CommandError as e:
        raise JbpError(f"AWS credentials are not usable: {e}")
    log.info("AWS identity OK (account …%s), region %s", ident["Account"][-4:], cfg["AWS_REGION"])
    return ident["Arn"]


def cluster_exists(cfg: Settings) -> bool:
    cp = aws(cfg, "eks", "describe-cluster", "--name", cfg["CLUSTER_NAME"], "--output", "json", check=False, timeout=30)
    return cp.returncode == 0


def pick_version(cfg: Settings) -> str:
    # DECISION: Kubernetes version. "auto" = newest version still in STANDARD support. A version in
    # extended support bills the control plane at $0.60/h instead of $0.10/h.
    if cfg["K8S_VERSION"] != "auto":
        return cfg["K8S_VERSION"]
    out = aws(cfg, "eks", "describe-cluster-versions", "--version-status", "STANDARD_SUPPORT",
              "--query", "clusterVersions[].clusterVersion", "--output", "json", timeout=60).stdout
    # Sort numerically, not as text, so "1.10" comes after "1.9".
    versions = sorted(json.loads(out), key=lambda v: tuple(int(x) for x in v.split(".")))
    if not versions:
        raise JbpError("AWS listed no EKS versions in standard support; set K8S_VERSION")
    log.info("EKS versions in standard support: %s -> using %s", ", ".join(versions), versions[-1])
    return versions[-1]


def available_instance_types(cfg: Settings) -> tuple[list[str], list[str]]:
    """INSTANCE_TYPES minus the ones this region doesn't offer in at least 2 AZs, and the AZs that offer
    every type kept (sorted).

    eksctl drops every AZ that lacks *any* listed type, so one missing type (e.g. m7a in ap-south-1)
    makes cluster creation fail with "only 0 zones discovered".
    """
    wanted = [t.strip() for t in cfg["INSTANCE_TYPES"].split(",") if t.strip()]
    out = aws(cfg, "ec2", "describe-instance-type-offerings", "--location-type", "availability-zone",
              "--filters", f"Name=instance-type,Values={','.join(wanted)}",
              "--query", "InstanceTypeOfferings[].[InstanceType,Location]", "--output", "json", timeout=60).stdout
    zones: dict[str, set] = {}
    for itype, az in json.loads(out):
        zones.setdefault(itype, set()).add(az)
    # EKS needs subnets in at least 2 AZs, so a type offered in only one AZ is useless here.
    keep = [t for t in wanted if len(zones.get(t, ())) >= 2]
    dropped = [t for t in wanted if t not in keep]
    if dropped:
        log.warning("not offered in %s (in >= 2 AZs), skipping: %s", cfg["AWS_REGION"], ", ".join(dropped))
    if not keep:
        raise JbpError(f"none of INSTANCE_TYPES ({', '.join(wanted)}) is offered in {cfg['AWS_REGION']}")
    # eksctl needs AZs that offer every listed type; keep only types sharing >= 2 AZs with the first one
    common = set(zones[keep[0]])
    result = []
    for t in keep:
        if len(common & zones[t]) >= 2:
            common &= zones[t]
            result.append(t)
    return result, sorted(common)


def eksctl_config(cfg: Settings, version: str, instance_types: list[str] | None = None,
                  zones: list[str] | None = None) -> str:
    instance_types = instance_types or [t.strip() for t in cfg["INSTANCE_TYPES"].split(",") if t.strip()]
    types = ", ".join(f'"{t}"' for t in instance_types)
    n = cfg.int("NODE_COUNT", 1)
    # DECISION: pin the node group to ONE zone. The workspace PVC is an EBS volume, which lives in one
    # zone; a spot replacement node started in another zone could never attach it, and the orchestrator
    # would sit Pending until its deadline. The cluster itself still spans >= 2 zones (EKS requires it).
    zones = zones or []
    node_az = cfg.get("NODE_AZ", "") or (zones[0] if zones else "")
    if node_az and zones and node_az not in zones:
        raise JbpError(f"NODE_AZ={node_az} does not offer every instance type (choose from {', '.join(zones)})")
    cluster_azs = f"availabilityZones: [{', '.join(zones)}]\n" if len(zones) >= 2 else ""
    ng_az = f"\n    availabilityZones: [{node_az}]" if node_az else ""
    return f"""apiVersion: eksctl.io/v1alpha5
kind: ClusterConfig
metadata:
  name: {cfg['CLUSTER_NAME']}
  region: {cfg['AWS_REGION']}
  version: "{version}"
  tags: {{project: java-build-perf, jbp-ephemeral: "true"}}
{cluster_azs}iam:
  withOIDC: true
vpc:
  nat: {{gateway: Disable}}        # no NAT gateway (~$0.045/h + per GB): nodes use public subnets
  clusterEndpoints: {{publicAccess: true, privateAccess: true}}
addons:
  - {{name: vpc-cni}}
  - {{name: coredns}}
  - {{name: kube-proxy}}
  - name: aws-ebs-csi-driver
    wellKnownPolicies: {{ebsCSIController: true}}
managedNodeGroups:
  - name: build-spot
    amiFamily: AmazonLinux2023
    spot: true
    instanceTypes: [{types}]
    desiredCapacity: {n}
    minSize: 0
    maxSize: {n + 2}
    volumeSize: {cfg.int('NODE_DISK_GB', 40)}
    volumeType: gp3
    privateNetworking: false
    labels: {{jbp/pool: build}}{ng_az}
"""


def up(cfg: Settings, tracker: StageTracker | None = None) -> None:
    check_credentials(cfg)
    name = cfg["CLUSTER_NAME"]
    # DECISION: reuse an existing cluster of the same name (e.g. after `jbp all --keep`) instead of
    # failing; creating one takes 15-20 minutes.
    if cluster_exists(cfg):
        log.info("cluster %s already exists, reusing it", name)
    else:
        version = pick_version(cfg)
        itypes, zones = available_instance_types(cfg)
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as fh:
            fh.write(eksctl_config(cfg, version, itypes, zones))
            path = fh.name
        log.info("creating cluster %s (EKS %s, %s x spot %s in %s) - takes ~15-20 min",
                 name, version, cfg["NODE_COUNT"], ", ".join(itypes), cfg.get("NODE_AZ") or zones[0])
        t0 = time.time()
        try:  # eksctl prints its own progress; each line goes into our log as it arrives
            run_streaming(["eksctl", "create", "cluster", "-f", path], log=log, prefix="eksctl: ", timeout=3600)
        except CommandError:
            raise JbpError("eksctl create cluster failed (see log above); `jbp down` cleans up partial resources")
        log.info("cluster created in %s", fmt_duration(time.time() - t0))
    run(["aws", "eks", "update-kubeconfig", "--name", name, "--region", cfg["AWS_REGION"]], log=log, timeout=60)
    # EKS has no default StorageClass; without jbp-gp3 the workspace PVC would stay Pending forever.
    sc = (ROOT / "k8s" / "storageclass-eks.yaml").read_text().replace("__CLUSTER_NAME__", name)
    run(["kubectl", "apply", "-f", "-"], input=sc, log=log, retries=3, timeout=60)
    nodes = json.loads(run(["kubectl", "get", "nodes", "-o", "json"], log=log, timeout=60).stdout)["items"]
    for n in nodes:
        lab = n["metadata"]["labels"]
        log.info("node %s: %s %s, %s", n["metadata"]["name"], lab.get("node.kubernetes.io/instance-type"),
                 lab.get("eks.amazonaws.com/capacityType", ""), lab.get("topology.kubernetes.io/zone"))


def down(cfg: Settings) -> bool:
    """Delete namespace (frees the EBS volume), cluster, then verify nothing is left. Returns True if clean."""
    check_credentials(cfg)
    name = cfg["CLUSTER_NAME"]
    if cluster_exists(cfg):
        try:
            run(["aws", "eks", "update-kubeconfig", "--name", name, "--region", cfg["AWS_REGION"]], log=log, timeout=60)
            # ORDER MATTERS: delete the namespace (and with it the PVC) while the EBS CSI driver still runs,
            # so the driver deletes the volume (reclaimPolicy: Delete). If the cluster went first, the
            # volume would be orphaned and keep billing.
            log.info("deleting namespace %s first, so the PVC's EBS volume is released", cfg["NAMESPACE"])
            run(["kubectl", "delete", "namespace", cfg["NAMESPACE"], "--ignore-not-found", "--wait=true"],
                log=log, timeout=300)
        except (CommandError, JbpError) as e:
            log.warning("namespace delete did not finish (%s); continuing, leftovers check will catch the volume", e)
        log.info("deleting cluster %s (~10 min: node group, then control plane, then VPC)", name)
        t0 = time.time()
        try:
            # --disable-nodegroup-eviction: nothing on the node needs a graceful drain, and skipping it
            # saves minutes.
            run_streaming(["eksctl", "delete", "cluster", "--name", name, "--region", cfg["AWS_REGION"], "--wait",
                           "--disable-nodegroup-eviction"], log=log, prefix="eksctl: ", timeout=2700)
        except (CommandError, JbpError) as e:
            log.error("eksctl delete failed: %s", e)
        log.info("eksctl finished after %s; verifying", fmt_duration(time.time() - t0))
    else:
        log.info("cluster %s not found", name)
    # Don't trust eksctl's exit code alone: poll AWS until nothing billable is left.
    return wait_until_clean(cfg)


def leftovers(cfg: Settings) -> dict:
    name, out = cfg["CLUSTER_NAME"], {}
    if cluster_exists(cfg):
        out["cluster"] = [name]
    stacks = json.loads(aws(cfg, "cloudformation", "list-stacks", "--stack-status-filter", "CREATE_COMPLETE",
                            "UPDATE_COMPLETE", "DELETE_FAILED", "DELETE_IN_PROGRESS", "CREATE_IN_PROGRESS",
                            "ROLLBACK_COMPLETE", "--output", "json", timeout=60).stdout)["StackSummaries"]
    st = [f'{s["StackName"]} ({s["StackStatus"]})' for s in stacks if s["StackName"].startswith(f"eksctl-{name}-")]
    if st:
        out["stacks"] = st
    vols = json.loads(aws(cfg, "ec2", "describe-volumes", "--filters", f"Name=tag:jbp-cluster,Values={name}",
                          "--output", "json", timeout=60).stdout)["Volumes"]
    if vols:
        out["volumes"] = [f'{v["VolumeId"]} {v["Size"]}GB {v["State"]}' for v in vols]
    return out


def wait_until_clean(cfg: Settings, timeout_s: int = 1200, every_s: int = 30) -> bool:
    """The poller: keep checking until cluster, stacks and tagged volumes are all gone."""
    t0 = time.time()
    while True:
        left = leftovers(cfg)
        if not left:
            log.info("verified: no cluster, stacks or volumes left for %s", cfg["CLUSTER_NAME"])
            return True
        if time.time() - t0 > timeout_s:
            for k, v in left.items():
                log.error("STILL BILLING - %s: %s", k, "; ".join(v))
            log.error("run `jbp leftovers --delete` (volumes) or delete the stacks in the CloudFormation console")
            return False
        log.info("still present after %s: %s", fmt_duration(time.time() - t0),
                 "; ".join(f"{k}: {len(v)}" for k, v in left.items()))
        time.sleep(every_s)


def delete_orphan_volumes(cfg: Settings) -> None:
    vols = json.loads(aws(cfg, "ec2", "describe-volumes", "--filters", f"Name=tag:jbp-cluster,Values={cfg['CLUSTER_NAME']}",
                          "Name=status,Values=available", "--output", "json", timeout=60).stdout)["Volumes"]
    for v in vols:
        aws(cfg, "ec2", "delete-volume", "--volume-id", v["VolumeId"], timeout=60)
        log.info("deleted orphaned volume %s", v["VolumeId"])
