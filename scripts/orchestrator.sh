#!/usr/bin/env bash
# Orchestrator: clone each repo (one by one) onto the shared volume, detect tool + Java,
# launch a build Job in a matching image, wait, and collect results.
#
# Runs in-cluster (LAUNCHER=k8s, image alpine/k8s: bash git jq kubectl) or on a laptop
# with Docker for quick tests (LAUNCHER=docker).
#
# Env:
#   REPOS_FILE   repo list (default /config/repos.txt)
#   IMAGES_FILE  image map (default /config/images.env)
#   WORKSPACE    shared volume mount (default /workspace)
#   SCRIPTS_DIR  where detect.sh/build.sh live (default /scripts)
#   MAX_PARALLEL concurrent build Jobs (default 1 = strictly one by one)
#   RUN_ID, NAMESPACE, BUILD_CPU, BUILD_MEM, BUILD_TIMEOUT_S, DEP_CACHE, MAVEN_ARGS_EXTRA, GRADLE_ARGS_EXTRA
set -uo pipefail

REPOS_FILE="${REPOS_FILE:-/config/repos.txt}"
IMAGES_FILE="${IMAGES_FILE:-/config/images.env}"
WORKSPACE="${WORKSPACE:-/workspace}"
SCRIPTS_DIR="${SCRIPTS_DIR:-/scripts}"
LAUNCHER="${LAUNCHER:-k8s}"
MAX_PARALLEL="${MAX_PARALLEL:-1}"
RUN_ID="${RUN_ID:-$(date +%Y%m%d-%H%M%S)}"
NAMESPACE="${NAMESPACE:-jbp}"
BUILD_CPU="${BUILD_CPU:-2}"
BUILD_MEM="${BUILD_MEM:-4Gi}"
BUILD_TIMEOUT_S="${BUILD_TIMEOUT_S:-1800}"
DEP_CACHE="${DEP_CACHE:-shared}"
TEMPLATE="${TEMPLATE:-/templates/build-job.tpl.yaml}"

# shellcheck disable=SC1090
. "$IMAGES_FILE"
RESULTS="$WORKSPACE/results"
mkdir -p "$RESULTS" "$WORKSPACE/repos" "$WORKSPACE/cache"
log() { echo "[orchestrator $(date +%H:%M:%S)] $*"; }
now_ms() { date +%s%3N 2>/dev/null || python3 -c 'import time;print(int(time.time()*1000))'; }

# pick the smallest supported JDK >= required (javac --release N works on any newer JDK)
pick_jdk() {
  local need=$1 j
  for j in $SUPPORTED_JDKS; do [ "$j" -ge "$need" ] && { echo "$j"; return; }; done
  echo "${SUPPORTED_JDKS##* }"   # newer than anything we have: use the newest
}
pick_image() { # tool wrapper jdk
  local var
  if [ "$1" = maven ]; then var="MAVEN_IMAGE_$3"
  elif [ "$2" = yes ]; then var="JDK_IMAGE_$3"          # gradlew brings its own Gradle
  else var="GRADLE_IMAGE_$3"; fi
  echo "${!var:-}"
}

json_merge() { # merge orchestrator facts with the build container's json
  local name=$1 facts=$2 bj="$RESULTS/$name.build.json"
  if [ -s "$bj" ] && jq -e . "$bj" >/dev/null 2>&1; then
    jq -s '.[0] * .[1]' "$bj" <(echo "$facts") > "$RESULTS/$name.json"
  else
    echo "$facts" | jq '. + {rc: (.rc // 1)}' > "$RESULTS/$name.json"
  fi
}

# ---------------- launchers ----------------
launch_k8s() { # name image subdir env...
  local name=$1 image=$2 workdir=$3 tool=$4 wrapper=$5 jdk=$6
  local job="jbp-build-${name}"
  job=$(echo "$job" | tr 'A-Z_.' 'a-z--' | cut -c1-63)
  sed -e "s|__JOB__|$job|g" -e "s|__NAMESPACE__|$NAMESPACE|g" -e "s|__IMAGE__|$image|g" \
      -e "s|__REPO__|$name|g" -e "s|__TOOL__|$tool|g" -e "s|__WRAPPER__|$wrapper|g" \
      -e "s|__JAVA__|$jdk|g" -e "s|__WORKDIR__|$workdir|g" -e "s|__RUN_ID__|$RUN_ID|g" \
      -e "s|__CPU__|$BUILD_CPU|g" -e "s|__MEM__|$BUILD_MEM|g" -e "s|__TIMEOUT__|$BUILD_TIMEOUT_S|g" \
      -e "s|__DEP_CACHE__|$DEP_CACHE|g" -e "s|__PVC__|${PVC_NAME:-jbp-workspace}|g" \
      -e "s|__MAVEN_ARGS__|${MAVEN_ARGS_EXTRA:-}|g" -e "s|__GRADLE_ARGS__|${GRADLE_ARGS_EXTRA:-}|g" \
      "$TEMPLATE" > "$RESULTS/$name.job.yaml"
  kubectl -n "$NAMESPACE" delete job "$job" --ignore-not-found --wait=true >/dev/null 2>&1
  kubectl apply -f "$RESULTS/$name.job.yaml" >/dev/null || return 1
  # wait for Complete or Failed
  local deadline=$(( $(date +%s) + BUILD_TIMEOUT_S + 600 )) s f
  while :; do
    s=$(kubectl -n "$NAMESPACE" get job "$job" -o jsonpath='{.status.succeeded}' 2>/dev/null)
    f=$(kubectl -n "$NAMESPACE" get job "$job" -o jsonpath='{.status.conditions[?(@.type=="Failed")].status}' 2>/dev/null)
    [ "${s:-0}" -ge 1 ] 2>/dev/null && break
    [ "$f" = True ] && break
    [ "$(date +%s)" -gt "$deadline" ] && { log "$name: timed out waiting for job"; break; }
    sleep 5
  done
  # pod timeline: created -> scheduled -> container started (pull) -> finished
  local pod; pod=$(kubectl -n "$NAMESPACE" get pods -l job-name="$job" --sort-by=.metadata.creationTimestamp -o name | tail -1)
  kubectl -n "$NAMESPACE" get "$pod" -o json > "$RESULTS/$name.pod.json" 2>/dev/null
  kubectl -n "$NAMESPACE" get pods -l job-name="$job" --no-headers 2>/dev/null | wc -l | tr -d ' ' > "$RESULTS/$name.attempts"
  [ "${s:-0}" -ge 1 ] 2>/dev/null
}

launch_docker() { # local test path: same image + build.sh, a host dir stands in for the PVC
  local name=$1 image=$2 workdir=$3 tool=$4 wrapper=$5 jdk=$6
  local cpus=${BUILD_CPU%m}; [[ "$BUILD_CPU" == *m ]] && cpus=$(awk -v m="${BUILD_CPU%m}" 'BEGIN{print m/1000}')
  docker run --rm --name "jbp-build-$name" ${DOCKER_EXTRA_ARGS:-} \
    --cpus "$cpus" --memory "$(echo "$BUILD_MEM" | sed -E 's/Gi$/g/;s/Mi$/m/')" \
    -v "$WORKSPACE:/workspace" -v "$SCRIPTS_DIR:/scripts:ro" \
    -e REPO_NAME="$name" -e TOOL="$tool" -e WRAPPER="$wrapper" -e JAVA="$jdk" \
    -e WORKDIR="/workspace/${workdir#"$WORKSPACE"/}" -e RESULT_DIR=/workspace/results \
    -e DEP_CACHE="$DEP_CACHE" -e MAVEN_ARGS_EXTRA="${MAVEN_ARGS_EXTRA:-}" -e GRADLE_ARGS_EXTRA="${GRADLE_ARGS_EXTRA:-}" \
    "$image" bash /scripts/build.sh > "$RESULTS/$name.docker.out" 2>&1
}

# ---------------- one repo ----------------
process_build() { # runs (possibly in background) after clone + detect
  local name=$1 image=$2 workdir=$3 tool=$4 wrapper=$5 jdk=$6 facts=$7
  local t0 t1 ok=true
  t0=$(now_ms)
  if [ -z "$image" ]; then ok=false; echo "no image for $tool/java $jdk" > "$RESULTS/$name.build.log"
  else "launch_$LAUNCHER" "$name" "$image" "$workdir" "$tool" "$wrapper" "$jdk" || ok=false; fi
  t1=$(now_ms)
  facts=$(echo "$facts" | jq --argjson a "$t0" --argjson b "$t1" --argjson ok "$ok" \
    '. + {launch_ms:$a, done_ms:$b, job_wall_s: (($b-$a)/1000), ok:$ok}')
  json_merge "$name" "$facts"
  log "$name: $( $ok && echo PASS || echo FAIL ) in $(( (t1 - t0) / 1000 ))s"
}

running=0
while read -r name url ref subdir extra; do
  [ -z "${name:-}" ] && continue; case "$name" in \#*) continue;; esac
  [ "${ref:--}" = - ] && ref=""; [ "${subdir:--}" = - ] && subdir=""
  if [ "${SKIP_DONE:-1}" = 1 ] && [ -s "$RESULTS/$name.json" ]; then log "$name: already done, skipping"; continue; fi

  dest="$WORKSPACE/repos/$name"; rm -rf "$dest"
  log "$name: cloning $url ${ref:+@$ref}"
  c0=$(now_ms)
  if [ -n "$ref" ]; then
    git clone -q --depth 1 --branch "$ref" "$url" "$dest" 2>"$RESULTS/$name.clone.err" \
      || { rm -rf "$dest"; git clone -q "$url" "$dest" 2>>"$RESULTS/$name.clone.err" && git -C "$dest" checkout -q "$ref"; }
  else
    git clone -q --depth 1 "$url" "$dest" 2>"$RESULTS/$name.clone.err"
  fi
  crc=$?; c1=$(now_ms)
  commit=$(git -C "$dest" rev-parse --short HEAD 2>/dev/null || echo "")
  size_kb=$(du -sk "$dest" 2>/dev/null | cut -f1)
  base=$(jq -n --arg r "$name" --arg u "$url" --arg ref "$ref" --arg sub "$subdir" --arg c "$commit" \
    --argjson c0 "$c0" --argjson c1 "$c1" --argjson kb "${size_kb:-0}" --arg run "$RUN_ID" --arg l "$LAUNCHER" \
    '{repo:$r,url:$u,ref:$ref,subdir:$sub,commit:$c,run_id:$run,launcher:$l,clone_s:(($c1-$c0)/1000),repo_kb:$kb}')
  if [ $crc -ne 0 ]; then
    log "$name: clone FAILED"; echo "$base" | jq --arg e "$(head -c 300 "$RESULTS/$name.clone.err")" '. + {ok:false, rc:128, stage:"clone", error_excerpt:$e}' > "$RESULTS/$name.json"
    continue
  fi

  workdir="$dest${subdir:+/$subdir}"
  # per-repo overrides in the 5th column: tool=gradle java=21
  FORCE_TOOL=""; FORCE_JAVA=""; TOOL=""; WRAPPER=""; JAVA=""; JAVA_SOURCE=""
  for kv in $extra; do case "$kv" in tool=*) FORCE_TOOL=${kv#tool=};; java=*) FORCE_JAVA=${kv#java=};; esac; done
  eval "$(FORCE_TOOL="$FORCE_TOOL" bash "$SCRIPTS_DIR/detect.sh" "$workdir")"
  [ -n "$FORCE_JAVA" ] && JAVA="$FORCE_JAVA" && JAVA_SOURCE="override in repos.txt"
  jdk=$(pick_jdk "$JAVA"); image=$(pick_image "$TOOL" "$WRAPPER" "$jdk")
  log "$name: tool=$TOOL wrapper=$WRAPPER java>=$JAVA ($JAVA_SOURCE) -> jdk $jdk image $image"
  facts=$(echo "$base" | jq --arg t "$TOOL" --arg w "$WRAPPER" --arg jr "$JAVA" --arg js "$JAVA_SOURCE" \
    --arg jdk "$jdk" --arg img "$image" --arg cpu "$BUILD_CPU" --arg mem "$BUILD_MEM" --arg dc "$DEP_CACHE" \
    '. + {tool:$t, wrapper:$w, java_required:$jr, java_source:$js, jdk:$jdk, image:$img, pod_cpu:$cpu, pod_mem:$mem, dep_cache:$dc}')

  if [ "$MAX_PARALLEL" -le 1 ]; then
    process_build "$name" "$image" "$workdir" "$TOOL" "$WRAPPER" "$jdk" "$facts"
  else
    while [ "$(jobs -rp | wc -l)" -ge "$MAX_PARALLEL" ]; do wait -n; done
    process_build "$name" "$image" "$workdir" "$TOOL" "$WRAPPER" "$jdk" "$facts" &
  fi
done < <(sed 's/\r$//' "$REPOS_FILE")
wait

merged=$(ls "$RESULTS"/*.json 2>/dev/null | grep -vE '\.(build|pod)\.json$')
total=$(echo "$merged" | grep -c . || true)
passed=$( [ -n "$merged" ] && jq -s '[.[] | select(.ok == true)] | length' $merged || echo 0)
log "done: $passed/$total passed"
date +%s > "$RESULTS/_orchestrator_done"
