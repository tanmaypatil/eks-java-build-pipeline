#!/usr/bin/env bash
# Render every manifest with sample values; check YAML parses and no __PLACEHOLDER__ is left.
set -euo pipefail
cd "$(dirname "$0")/.."
. bin/common.sh
export NAMESPACE RUN_ID=t ORCH_IMAGE MAX_PARALLEL BUILD_CPU BUILD_MEM BUILD_TIMEOUT_S DEP_CACHE MAVEN_ARGS_EXTRA GRADLE_ARGS_EXTRA IMAGE_REGISTRY CLUSTER_NAME PVC_SIZE
export STORAGE_CLASS_LINE="storageClassName: jbp-gp3"
out=$(mktemp -d); trap 'rm -rf "$out"' EXIT
for f in rbac pvc orchestrator-job collector-pod storageclass-eks; do render "k8s/$f.yaml" > "$out/$f.yaml"; done
# build job is rendered by the orchestrator's sed; mimic it
sed -e 's/__JOB__/jbp-build-x/;s/__NAMESPACE__/jbp/;s/__IMAGE__/maven:3/;s/__CPU__/2/g;s/__MEM__/4Gi/g;s/__TIMEOUT__/1800/;s/__PVC__/jbp-workspace/;s/__[A-Z_]*__/v/g' \
  k8s/build-job.tpl.yaml > "$out/build-job.yaml"
python3 - "$out" <<'PY'
import glob, sys, yaml, re
bad = 0
for f in sorted(glob.glob(sys.argv[1] + "/*.yaml")):
    txt = open(f).read()
    left = re.findall(r"__[A-Z_]+__", txt)
    docs = [d for d in yaml.safe_load_all(txt) if d]
    for d in docs:
        assert d.get("apiVersion") and d.get("kind") and d["metadata"].get("name"), f
    if left: bad += 1; print("placeholders left in", f, left)
    print("ok", f.split("/")[-1], [d["kind"] for d in docs])
sys.exit(bad)
PY
