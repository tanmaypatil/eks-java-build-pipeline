#!/usr/bin/env bash
# Runs inside the build container (maven:*-temurin-* / gradle:* / eclipse-temurin:*).
# Required env: REPO_NAME TOOL WRAPPER JAVA WORKDIR RESULT_DIR
# Optional env: MAVEN_ARGS_EXTRA GRADLE_ARGS_EXTRA MAVEN_GOALS GRADLE_TASKS DEP_CACHE (shared|none)
# No "set -e" on purpose: a failing build must still reach the measurement and JSON-writing code below.
set -uo pipefail
: "${REPO_NAME:?}" "${TOOL:?}" "${WORKDIR:?}" "${RESULT_DIR:?}"
mkdir -p "$RESULT_DIR"
LOG="$RESULT_DIR/$REPO_NAME.build.log"
OUT="$RESULT_DIR/$REPO_NAME.build.json"
# Dependency cache on the shared PVC (/workspace/cache/m2, /workspace/cache/gradle) when DEP_CACHE=shared.
CACHE_ROOT="${CACHE_ROOT:-/workspace/cache}"
cd "$WORKDIR" || { echo "no workdir $WORKDIR"; exit 2; }

# ---- cgroup accounting (v2 on EKS AL2023, v1 fallback) ----
# The numbers come from the container's own cgroup, i.e. exactly what Kubernetes limits and bills:
# CPU time used, how often the CPU limit throttled us, peak memory, the CPU limit itself, OOM kills.
# Each helper tries the cgroup v2 file first and falls back to the v1 path (older nodes, Docker Desktop).
cg_cpu_usec() {
  if [ -f /sys/fs/cgroup/cpu.stat ]; then awk '/^usage_usec/{print $2}' /sys/fs/cgroup/cpu.stat
  elif [ -f /sys/fs/cgroup/cpuacct/cpuacct.usage ]; then echo $(( $(cat /sys/fs/cgroup/cpuacct/cpuacct.usage) / 1000 ))
  else echo 0; fi
}
cg_throttled() { # prints "nr_periods nr_throttled throttled_usec"
  if [ -f /sys/fs/cgroup/cpu.stat ]; then
    awk '/^nr_periods/{p=$2}/^nr_throttled/{t=$2}/^throttled_usec/{u=$2}END{print p+0, t+0, u+0}' /sys/fs/cgroup/cpu.stat
  elif [ -f /sys/fs/cgroup/cpu/cpu.stat ]; then
    awk '/^nr_periods/{p=$2}/^nr_throttled/{t=$2}/^throttled_time/{u=int($2/1000)}END{print p+0, t+0, u+0}' /sys/fs/cgroup/cpu/cpu.stat
  else echo "0 0 0"; fi
}
cg_mem_peak() {
  for f in /sys/fs/cgroup/memory.peak /sys/fs/cgroup/memory/memory.max_usage_in_bytes; do [ -f "$f" ] && { cat "$f"; return; }; done; echo 0
}
cg_cpu_limit() {
  if [ -f /sys/fs/cgroup/cpu.max ]; then awk '{ if ($1=="max") print 0; else printf "%.2f", $1/$2 }' /sys/fs/cgroup/cpu.max
  elif [ -f /sys/fs/cgroup/cpu/cpu.cfs_quota_us ]; then q=$(cat /sys/fs/cgroup/cpu/cpu.cfs_quota_us); p=$(cat /sys/fs/cgroup/cpu/cpu.cfs_period_us); [ "$q" -gt 0 ] && awk -v q="$q" -v p="$p" 'BEGIN{printf "%.2f", q/p}' || echo 0
  else echo 0; fi
}
cg_oom() { [ -f /sys/fs/cgroup/memory.events ] && awk '/^oom_kill /{print $2}' /sys/fs/cgroup/memory.events || echo 0; }

now_ms() { date +%s%3N; }

# ---- command line ----
# DECISION: what to run (TOOL and WRAPPER come from detect.py via the orchestrator).
#   Maven:  ./mvnw if the repo has a wrapper (its pinned Maven version), else the image's mvn.
#           -B batch, -ntp no download progress, -e full stack traces. Default goal: verify
#           (compile + unit tests + integration tests, no install/deploy).
#   Gradle: ./gradlew if present, else the image's gradle. --no-daemon: one-shot container, a daemon
#           would only cost memory. Default task: build (compile + test + assemble).
# chmod +x because a wrapper committed without the exec bit is common.
if [ "$TOOL" = maven ]; then
  MVN=mvn; [ "${WRAPPER:-no}" = yes ] && chmod +x mvnw && MVN=./mvnw
  CACHE_ARGS=""
  # shared: point the local repository at the PVC so later repos reuse jars earlier ones downloaded.
  [ "${DEP_CACHE:-shared}" = shared ] && CACHE_ARGS="-Dmaven.repo.local=$CACHE_ROOT/m2"
  CMD="$MVN -B -ntp -e $CACHE_ARGS ${MAVEN_ARGS_EXTRA:-} ${MAVEN_GOALS:-verify}"
elif [ "$TOOL" = gradle ]; then
  GR=gradle; [ "${WRAPPER:-no}" = yes ] && chmod +x gradlew && GR=./gradlew
  [ "${DEP_CACHE:-shared}" = shared ] && export GRADLE_USER_HOME="$CACHE_ROOT/gradle"
  CMD="$GR --no-daemon --console=plain ${GRADLE_ARGS_EXTRA:-} ${GRADLE_TASKS:-build}"
else
  echo "unknown tool '$TOOL'" | tee "$LOG"; CMD="false"
fi

echo "[jbp] repo=$REPO_NAME tool=$TOOL java=$JAVA wrapper=${WRAPPER:-no}" | tee "$LOG"
echo "[jbp] cmd: $CMD" | tee -a "$LOG"
java -version 2>&1 | tee -a "$LOG"

# Snapshot the counters immediately before and after the build: the deltas measure the build alone,
# not this script or the container start-up.
cpu0=$(cg_cpu_usec); read -r p0 t0 u0 < <(cg_throttled)
t_start=$(now_ms)
bash -c "$CMD" >>"$LOG" 2>&1
rc=$?
t_end=$(now_ms)
cpu1=$(cg_cpu_usec); read -r p1 t1 u1 < <(cg_throttled)

# ---- test results (surefire/failsafe/gradle all write JUnit XML) ----
# Sum the counts from the <testsuite ...> tag of every TEST-*.xml. No XML parser in these images,
# so grep the attributes. Works the same for Maven (surefire/failsafe) and Gradle (build/test-results).
tests=0; failures=0; errors=0; skipped=0
while IFS= read -r f; do
  line=$(grep -m1 -oE '<testsuite [^>]*' "$f") || continue
  g() { echo "$line" | grep -oE " $1=\"[0-9]+\"" | grep -oE '[0-9]+' | head -1; }
  tests=$((tests + $(g tests || echo 0))); failures=$((failures + $(g failures || echo 0)))
  errors=$((errors + $(g errors || echo 0))); skipped=$((skipped + $(g skipped || echo 0)))
done < <(find . \( -path '*/surefire-reports/TEST-*.xml' -o -path '*/failsafe-reports/TEST-*.xml' -o -path '*/test-results/*/TEST-*.xml' \) 2>/dev/null)

dur_ms=$((t_end - t_start))
cpu_s=$(awk -v a="$cpu0" -v b="$cpu1" 'BEGIN{printf "%.1f", (b-a)/1e6}')
avg_cores=$(awk -v c="$cpu_s" -v d="$dur_ms" 'BEGIN{ if (d>0) printf "%.2f", c/(d/1000); else print 0 }')
# throttled_pct = share of 100 ms CFS periods in which the CPU limit stopped us. High = the build wanted
# more CPU than BUILD_CPU allows (raising the limit would speed it up).
thr_pct=$(awk -v p=$((p1-p0)) -v t=$((t1-t0)) 'BEGIN{ if (p>0) printf "%.1f", 100*t/p; else print 0 }')
thr_s=$(awk -v u=$((u1-u0)) 'BEGIN{printf "%.1f", u/1e6}')
# A short reason for the report: Gradle's "What went wrong" block, or Maven's first [ERROR] lines.
# Quotes/backslashes/tabs are replaced so the text is safe inside the JSON string below.
last_err=$( { grep -m1 -A4 'What went wrong' "$LOG"; grep -m4 -E '^\[ERROR\]' "$LOG"; } | grep -vE '^\* What went wrong|^\[ERROR\] *$' | head -4 | tr '"\\\t' "'/ " | cut -c1-300 | paste -sd '|' -)

# Written by hand (no jq in the images). The orchestrator merges this file into results/<repo>.json.
cat > "$OUT" <<EOF
{"repo":"$REPO_NAME","tool":"$TOOL","java":"$JAVA","wrapper":"${WRAPPER:-no}",
 "cmd":"$(echo "$CMD" | tr '"' "'")","rc":$rc,
 "build_start_ms":$t_start,"build_end_ms":$t_end,"build_s":$(awk -v d=$dur_ms 'BEGIN{printf "%.1f", d/1000}'),
 "cpu_s":$cpu_s,"avg_cores":$avg_cores,"cpu_limit":$(cg_cpu_limit),
 "throttled_pct":$thr_pct,"throttled_s":$thr_s,
 "peak_mem_mib":$(( $(cg_mem_peak) / 1048576 )),"oom_kills":$(cg_oom),
 "tests":$tests,"failures":$failures,"errors":$errors,"skipped":$skipped,
 "error_excerpt":"$last_err"}
EOF
echo "[jbp] rc=$rc build_s=$((dur_ms/1000)) tests=$tests failures=$failures errors=$errors" | tee -a "$LOG"
# Exit with the build's own code: non-zero fails the pod, and podFailurePolicy (FailJob) fails the Job.
exit $rc
