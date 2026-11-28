#!/usr/bin/env bash
# Runs inside the build container (maven:*-temurin-* / gradle:* / eclipse-temurin:*).
# Required env: REPO_NAME TOOL WRAPPER JAVA WORKDIR RESULT_DIR
# Optional env: MAVEN_ARGS_EXTRA GRADLE_ARGS_EXTRA MAVEN_GOALS GRADLE_TASKS DEP_CACHE (shared|none)
#               MAVEN_OPTS (JVM flags for Maven itself) BUILD_CACHE (off|on) BUILD_CACHE_EDIT (leaf|root)
#               DEP_BUMP (simulated dependency upgrade, see below)
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
  # shared: point the local repository at the PVC so later repos reuse jars earlier ones downloaded,
  # and MAVEN_USER_HOME too, so ./mvnw finds the Maven distribution it downloaded last time instead of
  # fetching it again into the throwaway container.
  if [ "${DEP_CACHE:-shared}" = shared ]; then
    CACHE_ARGS="-Dmaven.repo.local=$CACHE_ROOT/m2"
    export MAVEN_USER_HOME="$CACHE_ROOT/m2-home"
  fi
  # DECISION: incremental build cache (experiment). BUILD_CACHE=on adds the Maven build-cache extension
  # at build time, so the repos themselves stay plain; the cache lives on the PVC so the next build pod
  # can reuse it. A repo that already has .mvn/extensions.xml is left alone (we don't merge XML).
  if [ "${BUILD_CACHE:-off}" = on ]; then
    if [ ! -f .mvn/extensions.xml ]; then
      mkdir -p .mvn
      cat > .mvn/extensions.xml <<'XML'
<?xml version="1.0" encoding="UTF-8"?>
<extensions>
  <extension>
    <groupId>org.apache.maven.extensions</groupId>
    <artifactId>maven-build-cache-extension</artifactId>
    <version>1.2.0</version>
  </extension>
</extensions>
XML
      cat > .mvn/maven-build-cache-config.xml <<'XML'
<?xml version="1.0" encoding="UTF-8"?>
<cache xmlns="http://maven.apache.org/BUILD-CACHE-CONFIG/1.2.0">
  <configuration>
    <enabled>true</enabled>
    <hashAlgorithm>XX</hashAlgorithm>
    <local><maxBuildsCached>3</maxBuildsCached></local>
  </configuration>
  <input><global><glob>{*.java,*.xml,*.properties}</glob></global></input>
</cache>
XML
    else
      echo "[jbp] BUILD_CACHE=on but the repo has its own .mvn/extensions.xml: not adding the cache extension"
    fi
    CACHE_ARGS="$CACHE_ARGS -Dmaven.build.cache.location=$CACHE_ROOT/build-cache"
  fi
  # BUILD_CACHE_EDIT simulates a developer change before a cached rebuild: one line appended to a
  # source file in the last module (leaf: nothing depends on it) or the first module (root: everything
  # depends on it). Modules are the mod-* folders of the synthetic perf repos.
  if [ -n "${BUILD_CACHE_EDIT:-}" ]; then
    mods=$(ls -d mod-*/ 2>/dev/null | sort)
    m=$( [ "$BUILD_CACHE_EDIT" = root ] && echo "$mods" | head -1 || echo "$mods" | tail -1 )
    f=$( [ -n "$m" ] && find "$m" -path '*/src/main/java/*' -name '*.java' | sort | head -1 )
    if [ -n "$f" ]; then echo "// jbp edit-marker $(date +%s%N)" >> "$f"; echo "[jbp] edited $f ($BUILD_CACHE_EDIT)"
    else echo "[jbp] BUILD_CACHE_EDIT=$BUILD_CACHE_EDIT: no mod-* module found, nothing edited"; fi
  fi
  # DEP_BUMP simulates the remediation change: a library version raised in the root pom.xml before the
  # build. Rules, separated by spaces or commas; each applies only where it matches, so one setting can
  # cover repos with different build files:
  #   ARTIFACT=VERSION      the <version> right after <artifactId>ARTIFACT</artifactId> (a dependency, a
  #                         BOM import in dependencyManagement, a plugin or the <parent>)
  #   prop:NAME=VERSION     the property <NAME>; if the POM doesn't define it but has a <parent> (e.g.
  #                         spring-boot-starter-parent, which manages tomcat.version), it is added to
  #                         <properties>, which overrides the parent's value: the usual CVE fix there
  # If DEP_BUMP is set and no rule matched, the build is not run (rc 3): the experiment would be invalid.
  BUMP_APPLIED=""
  if [ -n "${DEP_BUMP:-}" ]; then
    cp pom.xml "$RESULT_DIR/$REPO_NAME.pom.orig"
    for rule in $(echo "$DEP_BUMP" | tr ',' ' '); do
      key=${rule%%=*}; ver=${rule#*=}
      case "$key" in
        prop:*)
          p=${key#prop:}
          if grep -q "<$p>" pom.xml; then
            sed -i "0,/<$p>[^<]*<\/$p>/s//<$p>$ver<\/$p>/" pom.xml && BUMP_APPLIED="$BUMP_APPLIED $rule"
          elif grep -q "<parent>" pom.xml && grep -q "<properties>" pom.xml; then
            sed -i "0,/<properties>/s//<properties>\n    <$p>$ver<\/$p>/" pom.xml && BUMP_APPLIED="$BUMP_APPLIED $rule"
          fi ;;
        *)
          if awk -v a="$key" -v v="$ver" '
              index($0, "<artifactId>" a "</artifactId>") { hit = 1 }
              hit && /<version>/ { sub(/<version>[^<]*<\/version>/, "<version>" v "</version>"); hit = 0; done = 1 }
              /<\/(dependency|parent|plugin)>/ { hit = 0 }
              { print }
              END { exit done ? 0 : 1 }' pom.xml > pom.xml.jbp; then
            mv pom.xml.jbp pom.xml; BUMP_APPLIED="$BUMP_APPLIED $rule"
          else rm -f pom.xml.jbp; fi ;;
      esac
    done
    BUMP_APPLIED=${BUMP_APPLIED# }
    if [ -n "$BUMP_APPLIED" ]; then
      echo "[jbp] DEP_BUMP applied: $BUMP_APPLIED"
      diff "$RESULT_DIR/$REPO_NAME.pom.orig" pom.xml | grep -E '^[<>]' | sed 's/^/[jbp]   /'
    fi
  fi
  CMD="$MVN -B -ntp -e $CACHE_ARGS ${MAVEN_ARGS_EXTRA:-} ${MAVEN_GOALS:-verify}"
  if [ -n "${DEP_BUMP:-}" ] && [ -z "$BUMP_APPLIED" ]; then
    CMD="echo '[ERROR] DEP_BUMP=$DEP_BUMP matched nothing in pom.xml, build not run'; exit 3"
  fi
elif [ "$TOOL" = gradle ]; then
  GR=gradle; [ "${WRAPPER:-no}" = yes ] && chmod +x gradlew && GR=./gradlew
  [ "${DEP_CACHE:-shared}" = shared ] && export GRADLE_USER_HOME="$CACHE_ROOT/gradle"
  CMD="$GR --no-daemon --console=plain ${GRADLE_ARGS_EXTRA:-} ${GRADLE_TASKS:-build}"
  BUMP_APPLIED=""
  [ -n "${DEP_BUMP:-}" ] && CMD="echo '[ERROR] DEP_BUMP is Maven-only for now, build not run'; exit 3"
else
  echo "unknown tool '$TOOL'" | tee "$LOG"; CMD="false"; BUMP_APPLIED=""
fi

echo "[jbp] repo=$REPO_NAME tool=$TOOL java=$JAVA wrapper=${WRAPPER:-no}" | tee "$LOG"
echo "[jbp] cmd: $CMD" | tee -a "$LOG"
echo "[jbp] MAVEN_OPTS=${MAVEN_OPTS:-} BUILD_CACHE=${BUILD_CACHE:-off} BUILD_CACHE_EDIT=${BUILD_CACHE_EDIT:-} DEP_BUMP=${DEP_BUMP:-}" | tee -a "$LOG"
java -version 2>&1 | tee -a "$LOG"

# Snapshot the counters immediately before and after the build: the deltas measure the build alone,
# not this script or the container start-up.
# How much the build downloaded: growth of the dependency + wrapper caches it uses (-ntp hides the
# transfer log lines, so we measure the folders instead). Taken outside the timed section.
if [ "${DEP_CACHE:-shared}" = shared ]; then DL_DIRS="$CACHE_ROOT/m2 $CACHE_ROOT/m2-home $CACHE_ROOT/gradle"
else DL_DIRS="$HOME/.m2 $HOME/.gradle"; fi
dl_kb() { du -sk $DL_DIRS 2>/dev/null | awk '{s+=$1} END{print s+0}'; }
dl0=$(dl_kb)
cpu0=$(cg_cpu_usec); read -r p0 t0 u0 < <(cg_throttled)
t_start=$(now_ms)
bash -c "$CMD" >>"$LOG" 2>&1
rc=$?
t_end=$(now_ms)
dl1=$(dl_kb)
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
 "cache_hits":$(grep -c 'Found cached build' "$LOG" || true),"downloaded_kb":$((dl1 - dl0)),
 "maven_opts":"$(echo "${MAVEN_OPTS:-}" | tr '"\\' "'/")","build_cache":"${BUILD_CACHE:-off}","build_cache_edit":"${BUILD_CACHE_EDIT:-}",
 "dep_bump":"$BUMP_APPLIED",
 "error_excerpt":"$last_err"}
EOF
echo "[jbp] rc=$rc build_s=$((dur_ms/1000)) tests=$tests failures=$failures errors=$errors" | tee -a "$LOG"
# Exit with the build's own code: non-zero fails the pod, and podFailurePolicy (FailJob) fails the Job.
exit $rc
