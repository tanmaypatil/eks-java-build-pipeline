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
    if cfg["K8S_VERSION"] != "auto":
        return cfg["K8S_VERSION"]
    out = aws(cfg, "eks", "describe-cluster-versions", "--version-status", "STANDARD_SUPPORT",
              "--query", "clusterVersions[].clusterVersion", "--output", "json", timeout=60).stdout
    versions = sorted(json.loads(out), key=lambda v: tuple(int(x) for x in v.split(".")))
    if not versions:
        raise JbpError("AWS listed no EKS versions in standard support; set K8S_VERSION")
    log.info("EKS versions in standard support: %s -> using %s", ", ".join(versions), versions[-1])
    return versions[-1]


def eksctl_config(cfg: Settings, version: str) -> str:
    types = ", ".join(f'"{t.strip()}"' for t in cfg["INSTANCE_TYPES"].split(",") if t.strip())
    n = cfg.int("NODE_COUNT", 1)
    return f"""apiVersion: eksctl.io/v1alpha5
kind: ClusterConfig
metadata:
  name: {cfg['CLUSTER_NAME']}
  region: {cfg['AWS_REGION']}
  version: "{version}"
  tags: {{project: java-build-perf, jbp-ephemeral: "true"}}
iam:
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
    labels: {{jbp/pool: build}}
"""


def up(cfg: Settings, tracker: StageTracker | None = None) -> None:
    check_credentials(cfg)
    name = cfg["CLUSTER_NAME"]
    if cluster_exists(cfg):
        log.info("cluster %s already exists, reusing it", name)
    else:
        version = pick_version(cfg)
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as fh:
            fh.write(eksctl_config(cfg, version))
            path = fh.name
        log.info("creating cluster %s (EKS %s, %s x spot %s) - takes ~15-20 min",
                 name, version, cfg["NODE_COUNT"], cfg["INSTANCE_TYPES"].split(",")[0] + "…")
        t0 = time.time()
        try:  # eksctl prints its own progress; each line goes into our log as it arrives
            run_streaming(["eksctl", "create", "cluster", "-f", path], log=log, prefix="eksctl: ", timeout=3600)
        except CommandError:
            raise JbpError("eksctl create cluster failed (see log above); `jbp down` cleans up partial resources")
        log.info("cluster created in %s", fmt_duration(time.time() - t0))
    run(["aws", "eks", "update-kubeconfig", "--name", name, "--region", cfg["AWS_REGION"]], log=log, timeout=60)
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
            log.info("deleting namespace %s first, so the PVC's EBS volume is released", cfg["NAMESPACE"])
            run(["kubectl", "delete", "namespace", cfg["NAMESPACE"], "--ignore-not-found", "--wait=true"],
                log=log, timeout=300)
        except (CommandError, JbpError) as e:
            log.warning("namespace delete did not finish (%s); continuing, leftovers check will catch the volume", e)
        log.info("deleting cluster %s (~10 min: node group, then control plane, then VPC)", name)
        t0 = time.time()
        try:
            run_streaming(["eksctl", "delete", "cluster", "--name", name, "--region", cfg["AWS_REGION"], "--wait",
                           "--disable-nodegroup-eviction"], log=log, prefix="eksctl: ", timeout=2700)
        except (CommandError, JbpError) as e:
            log.error("eksctl delete failed: %s", e)
        log.info("eksctl finished after %s; verifying", fmt_duration(time.time() - t0))
    else:
        log.info("cluster %s not found", name)
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
