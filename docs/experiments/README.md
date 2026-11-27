# Java build experiments

The goal of this repo is not the EKS cluster itself: it is a pipeline that builds Java repos inside Kubernetes,
used to find out **what drives Java build time** and which settings (pod CPU/memory, Maven, JVM, tests, caches)
make builds faster and cheaper without changing their test results. EKS is the test bed; OpenShift is the target.

This folder is the experiment plan, ranked by expected benefit. The live, editable copy is the
"Java Build Experiments" sheet in the project; these files are a snapshot of it, kept in version control.
Update both together.

| File | Contents |
|---|---|
| [experiments.csv](experiments.csv) | the ranked experiments with exact test specs (one column per spec field) |
| [baseline-spec.csv](baseline-spec.csv) | what stays fixed unless an experiment changes it |
| [prerequisites.csv](prerequisites.csv) | fixes to land before any EKS number is trusted |
| [test-projects.csv](test-projects.csv) | the p1 to p6 synthetic repos and the two real repos |
| [production-recipes.csv](production-recipes.csv) | exact Maven/Gradle command lines and settings per scenario, with evidence |

## Running experiments

`config/experiments.txt` holds experiments 1, 3 and 5 in a form the pipeline runs (format at the top of
the file). Each variant is an ordinary pipeline run with a few settings changed; all variants share one
cluster and one volume.

```bash
./jbp experiment --local --reps 1          # free rehearsal with Docker (no cluster)
./jbp experiment --reps 3                  # EKS: up -> warm-up -> 3 rounds of every variant -> down
./jbp experiment --reps 3 --only build-cache
```

Output: `runs/<session>/comparison.html` (per experiment: objective, expectation, a repo × variant table of
median build time and the change against the baseline variant, a verdict, and a detail table),
`comparison.csv`, and one ordinary run folder per variant and rep.

Switches the plan uses (also usable on a normal run): `MAVEN_OPTS_EXTRA` (JVM flags for Maven itself),
`BUILD_CACHE=on` (adds the Maven build-cache extension at build time, cache on the volume),
`BUILD_CACHE_EDIT=leaf|root` (edits one source file before building), `CLEAR_CACHE=deps,build` (empties
those caches when the run starts). With `DEP_CACHE=shared` the Maven wrapper's download now also lives on
the volume (`MAVEN_USER_HOME`).


## Production recipes

What to run in production for each case, from the experiments. Paths use `/cache/...` for the persistent volume.

### Baseline (what every number is compared to)

- **When:** Reference only.
- **Maven:**

```
mvn -B -ntp -e verify
```

- **JVM, env, pod:** Pod CPU request = limit = 2, memory 4Gi. MAVEN_OPTS unset.
- **Files:** None
- **Gradle:**

```
./gradlew build --no-daemon --console=plain
```

- **Evidence:** Pilot baseline: p3 26 s, p4 46 s, p5 106 s, p6 188 s, petclinic 34 s (warm deps).
- **Caveats:** -B (batch) and -ntp (no transfer progress) only quieten the log; -e prints errors in full.

### Dependency cache

- **When:** Always, in every pod-based build: fresh pods otherwise download every library, and the Maven or Gradle distribution too.
- **Maven:**

```
mvn -B -ntp -e -Dmaven.repo.local=/cache/m2 verify
(or ./mvnw with the same flags)
With an internal mirror: add -s /cache/settings.xml
Several pods writing one m2 at once: add -Daether.syncContext.named.factory=file-lock -Daether.syncContext.named.nameMapper=file-gav
```

- **JVM, env, pod:** Mount /cache from a persistent volume.
export MAVEN_USER_HOME=/cache/m2-home (the Maven wrapper keeps its Maven download there).
settings.xml mirror: <mirror><id>internal</id><mirrorOf>*</mirrorOf><url>https://<nexus>/repository/maven-public/</url></mirror>
- **Files:** None
- **Gradle:**

```
export GRADLE_USER_HOME=/cache/gradle
./gradlew build --no-daemon
(caches dependencies and the wrapper distribution)
Shared read-only seed for many pods: export GRADLE_RO_DEP_CACHE=/cache/gradle-ro
```

- **Evidence:** Pilot 22 Nov, cold vs warm: synthetic +2 to +40%; petclinic (mvnw) +34 to +42%; Gradle guide +165 to +292%; about 220 to 250 MB per real repo.
- **Caveats:** A version bump's new artifact always downloads once; everything else stays cached. Prefer an in-cluster repository manager (Nexus or Artifactory proxy) for many parallel pods, and to avoid Maven Central rate limits (HTTP 429).

### Build cache (Maven)

- **When:** Multi-module repos rebuilt often with small changes, e.g. PR builds of the same repo. Not for remediation verify-builds, single-module repos or release builds.
- **Maven:**

```
mvn -B -ntp -e -Dmaven.repo.local=/cache/m2 -Dmaven.build.cache.location=/cache/build-cache verify
Turn it off for one build: -Dmaven.build.cache.enabled=false
```

- **JVM, env, pod:** The cache location must persist between builds (volume or remote cache).
- **Files:** .mvn/extensions.xml: extension org.apache.maven.extensions:maven-build-cache-extension:1.2.0
.mvn/maven-build-cache-config.xml: <enabled>true</enabled>, <hashAlgorithm>XX</hashAlgorithm>, <local><maxBuildsCached>3</maxBuildsCached></local>, input glob {*.java,*.xml,*.properties}
- **Gradle:**

```
gradle.properties: org.gradle.caching=true
./gradlew build --build-cache
Shared between pods: a remote build cache in settings.gradle (buildCache { remote(HttpBuildCache) { url = ... } })
```

- **Evidence:** Pilot 22 Nov: warm, no change −90 to −98% (about 3 s); leaf-module edit −66 to −93%; root-module edit about 0%; empty cache +0 to +6%.
- **Caveats:** A cache hit skips that module's tests. Safe only when every input is declared; builds that read undeclared files or env vars can restore wrongly. A change to a shared or root module (or a parent POM / BOM version bump) rebuilds everything.

### More CPUs, if available

- **When:** Multi-module repos when a pod can have 4 or more CPUs and build latency matters more than cost.
- **Maven:**

```
mvn -B -ntp -e -Dmaven.repo.local=/cache/m2 -T1C verify
(-T1C = one build thread per CPU)
```

- **JVM, env, pod:** Pod CPU request = limit = 4. The JVM reads the CPU limit itself (JDK 17.0.5+), so no flag is needed. To override what it sees: MAVEN_OPTS=-XX:ActiveProcessorCount=4
- **Files:** None
- **Gradle:**

```
./gradlew build --parallel --max-workers=4
(or org.gradle.parallel=true in gradle.properties)
```

- **Evidence:** Pilot 22 Nov: 4 CPU alone −13 to −22%; 4 CPU + -T1C −26 to −30%; -T1C at 2 CPU 5 to 11% slower; 1 CPU is about 2x slower than 2.
- **Caveats:** Do not use -T1C below 4 CPU. Single-module repos gain nothing from -T. Plugins that aren't thread-safe print a warning. For throughput across many repos, more 2-CPU pods beat fewer 4-CPU pods.

### Skip plugins (verify-build)

- **When:** Builds that only prove compile + tests pass, such as remediation verify-builds. Never for release builds.
- **Maven:**

```
mvn -B -ntp -e -Dmaven.repo.local=/cache/m2 -Dmaven.javadoc.skip=true -Dmaven.source.skip=true -Djacoco.skip=true -Dcheckstyle.skip=true -Dspotbugs.skip=true -Dpmd.skip=true -Dgpg.skip=true -Djib.skip=true verify
Stop at verify (no install or deploy).
```

- **JVM, env, pod:** None
- **Files:** None
- **Gradle:**

```
./gradlew build --no-daemon -x javadoc -x jacocoTestReport -x checkstyleMain -x checkstyleTest -x spotbugsMain
(-x fails on a task the build doesn't have: list tasks first with ./gradlew tasks --all)
```

- **Evidence:** Not measured yet: the synthetic repos have none of these plugins. Planned in experiment 13.
- **Caveats:** Keep the compiler, surefire/failsafe (tests) and the enforcer (its dependency-convergence rules matter for a version bump). Skip properties differ between plugins and versions, and a repo can hard-code <skip>false</skip>.

### JIT tiering (local benchmark only)

- **When:** Short-lived build JVMs, to cut build time and CPU-seconds. Not yet tested on EKS (experiment 2).
- **Maven:**

```
mvn -B -ntp -e -Dmaven.repo.local=/cache/m2 verify
Tests too: add -DargLine="-XX:TieredStopAtLevel=1"
```

- **JVM, env, pod:** MAVEN_OPTS="-XX:TieredStopAtLevel=1"
- **Files:** None
- **Gradle:**

```
gradle.properties: org.gradle.jvmargs=-XX:TieredStopAtLevel=1
Tests: tasks.test { jvmArgs '-XX:TieredStopAtLevel=1' }
```

- **Evidence:** Local 6 Nov on p4: −16% (Maven JVM only), −30% with the test JVMs too.
- **Caveats:** -DargLine replaces a repo's own argLine (e.g. a JaCoCo agent or memory settings); check before using it on real repos.

### Recommended: remediation verify-build

- **When:** The real use case: bump a vulnerable library, prove the build passes, raise a PR.
- **Maven:**

```
mvn -B -ntp -e -Dmaven.repo.local=/cache/m2 -Dmaven.javadoc.skip=true -Dmaven.source.skip=true -Djacoco.skip=true -Dcheckstyle.skip=true -Dspotbugs.skip=true -Dpmd.skip=true verify
No -T1C, no build cache.
```

- **JVM, env, pod:** Pod CPU request = limit = 2, memory 4Gi. MAVEN_USER_HOME=/cache/m2-home. Shared dependency cache or in-cluster mirror. Several such pods per node.
- **Files:** None
- **Gradle:**

```
GRADLE_USER_HOME=/cache/gradle ./gradlew build --no-daemon -x <report and analysis tasks the build has>
```

- **Evidence:** Combines the dependency-cache result (+2 to 42% saved on Maven, more on Gradle) with 2 CPU being the best time per CPU. Plugin skipping and parallel pods to be measured in experiment 13.
- **Caveats:** Know each repo's status before the bump (CI history or one baseline build): if it was already failing, a failed verify-build says nothing about the bump.

## Experiments (ranked)

| Rank | Experiment | Objective | Status | EKS result |
|---|---|---|---|---|
| 1 | Pod CPU size × Maven module parallelism | Find the pod CPU size where build time stops improving, and whether -T1C only pays off once the pod has spare CPUs (4+). | Done | Pilot 22 Nov (1 rep, 30/30 pass): 1→2 CPU halves time (1 CPU is +77 to +107%). 2→4 CPU only −13 to −22% (build uses 2.3 to 3.1 cores). 4 CPU + -T1C −26 to −30% (3.9 cores). -T1C at 2 CPU is 5 to 11% slower; at 1 CPU no effect. Verdict: 2 CPU for cost; 4 CPU only together with -T1C. |
| 2 | JIT tiering (stop at the fast C1 compiler) | Confirm that stopping the JIT at its fast C1 compiler cuts build time and CPU use in pods, with identical test results. | Planned | – |
| 3 | Incremental build cache | Measure how much a warm build cache saves when the same repo is built again, and prove it never skips tests that should run. | Done | Pilot 22 Nov (1 rep, 15/15 pass): warm, no change −90 to −98% (about 3 s at any size); leaf edit −66 to −93%; root edit −7 to +2%; empty cache +0 to +6%. Matches the local benchmark. Not useful for remediation verify-builds (cold repos; a version bump acts like a root edit). |
| 4 | Test execution settings | Find test-runner settings that shorten the test phase without losing tests, and name the settings that must be avoided. | Planned | – |
| 5 | Dependency and Maven-wrapper cache: cold vs warm | Measure how much of a build is spent downloading libraries and Maven/Gradle themselves, and how much a persistent cache saves a fresh pod. | Done | Pilot 22 Nov (1 rep, 24/24 pass): cold vs warm cache: synthetic repos +2 to +40% (most on small repos); petclinic (Maven wrapper) +34 to +42%; Gradle guide +165 to +292%; about 220 to 250 MB downloaded per real repo; no 429 errors. Verdict: keep a shared cache, wrapper distribution included. |
| 6 | CPU requests vs limits | Find whether letting a pod burst above its CPU request (limit > request) speeds builds, and how many CPUs the JVM then thinks it has. | Planned | – |
| 7 | javac fork check | Confirm in-process javac beats a forked javac, and find real repos that fork it. | Planned | – |
| 8 | Pod memory and the SerialGC threshold | Find the smallest pod memory that avoids the slow SerialGC and OOM kills. | Later | – |
| 9 | Image pre-pull | Measure how long image pulls add to a build and whether pre-pulling is worth it. | Later | – |
| 10 | Build daemon (mvnd) | Check whether mvnd's warm daemon helps at all when every build runs in a fresh pod. | Later | – |
| 11 | Garbage collector choice | Confirm GC choice barely matters for builds, so we can stop tuning it. | Deprioritised | – |
| 12 | JDK version running Maven | Confirm the JDK version running Maven doesn't change build time. | Deprioritised | – |
| 13 | Remediation verify-build (dependency bump) | For the real use case (bump a vulnerable library, build to prove it still compiles and tests pass, then raise a PR): find the fastest correct verify-build and how many repos one node verifies per hour. | Planned | – |

## Test spec per experiment

### 1. Pod CPU size × Maven module parallelism

- **Objective:** Find the pod CPU size where build time stops improving, and whether -T1C only pays off once the pod has spare CPUs (4+).
- **Repos:** p1 (single-module control), p3, p4, p5, p6
- **Node:** m6a.2xlarge spot (8 vCPU, 32 GiB) for every variant, so a 4-CPU pod fits
- **Pod (CPU / memory):** CPU 1 / 2 / 4 (request = limit), memory 4Gi
- **Variants:**

```
6 variants: BUILD_CPU=1|2|4 × MAVEN_ARGS_EXTRA="" | "-T1C"
MAVEN_OPTS unset
```

- **Command:** `mvn -B -ntp -e -Dmaven.repo.local=/workspace/cache/m2 [-T1C] verify`
- **Measure / check:** build_s, avg_cores, throttled_pct; find where time stops dropping as CPU grows
- **Status:** Done · **Effort:** Low
- **Measured locally:** p4: 101 s at 1 CPU, 50 s at 2, 29 s at 4. -T1C gave −45% on p6 with 4 CPUs but about 0% in a 2-CPU pod (52 s vs 51 s).
- **Expected:** Biggest speed lever. Shows the right pod size for OpenShift; cost per build stays roughly flat while feedback gets about 2x faster per CPU doubling.
- **EKS result:** Pilot 22 Nov (1 rep, 30/30 pass): 1→2 CPU halves time (1 CPU is +77 to +107%). 2→4 CPU only −13 to −22% (build uses 2.3 to 3.1 cores). 4 CPU + -T1C −26 to −30% (3.9 cores). -T1C at 2 CPU is 5 to 11% slower; at 1 CPU no effect. Verdict: 2 CPU for cost; 4 CPU only together with -T1C.

### 2. JIT tiering (stop at the fast C1 compiler)

- **Objective:** Confirm that stopping the JIT at its fast C1 compiler cuts build time and CPU use in pods, with identical test results.
- **Repos:** p2, p4, p6, petclinic
- **Node:** m6a.xlarge spot (4 vCPU, 16 GiB)
- **Pod (CPU / memory):** 2 / 4Gi
- **Variants:**

```
A) MAVEN_OPTS unset
B) MAVEN_OPTS="-XX:TieredStopAtLevel=1"
C) B + MAVEN_ARGS_EXTRA="-DargLine=-XX:TieredStopAtLevel=1" (forked test JVMs too)
```

- **Command:** `mvn -B -ntp -e -Dmaven.repo.local=/workspace/cache/m2 verify`
- **Measure / check:** build_s, cpu_s; test count must match A (petclinic's own argLine must not be lost)
- **Status:** Planned · **Effort:** Low
- **Measured locally:** p4: 31.4 s → 26.3 s in MAVEN_OPTS (−16%), 21.8 s with surefire argLine too (−30%). 1-CPU pod: 100 s → 79 s (−21%).
- **Expected:** −15 to −30%, and it cuts CPU-seconds, so cost drops too. Best value for effort.

### 3. Incremental build cache

- **Objective:** Measure how much a warm build cache saves when the same repo is built again, and prove it never skips tests that should run.
- **Repos:** p3, p5, p6 (re-add .mvn/extensions.xml with maven-build-cache-extension 1.2.0)
- **Node:** m6a.xlarge spot
- **Pod (CPU / memory):** 2 / 4Gi
- **Variants:**

```
1) extension off
2) on, empty cache
3) on, warm, no change
4) on, warm, one leaf-module class edited
5) on, warm, root-module class edited
All: -Dmaven.build.cache.location=/workspace/cache/build-cache
```

- **Command:** `mvn -B -ntp -e -Dmaven.repo.local=/workspace/cache/m2 verify`
- **Measure / check:** build_s; test count per variant (a cache hit skips surefire)
- **Status:** Done · **Effort:** High
- **Measured locally:** p6: 108 s → 2.8 s with no changes (−97%), 10 s with a leaf module edited (−90%); a root-module edit gives no gain.
- **Expected:** Largest number we have, for repeated builds of the same repo.
- **EKS result:** Pilot 22 Nov (1 rep, 15/15 pass): warm, no change −90 to −98% (about 3 s at any size); leaf edit −66 to −93%; root edit −7 to +2%; empty cache +0 to +6%. Matches the local benchmark. Not useful for remediation verify-builds (cold repos; a version bump acts like a root edit).

### 4. Test execution settings

- **Objective:** Find test-runner settings that shorten the test phase without losing tests, and name the settings that must be avoided.
- **Repos:** p2, p4, p5
- **Node:** m6a.xlarge spot
- **Pod (CPU / memory):** 2 / 4Gi
- **Variants:**

```
A) default (forkCount=1, reuseForks=true)
B) -Djunit.jupiter.execution.parallel.enabled=true -Djunit.jupiter.execution.parallel.mode.classes.default=concurrent
C) -DforkCount=2
D) -DforkCount=1C -DreuseForks=false (anti-pattern reference, 1 rep only)
All via MAVEN_ARGS_EXTRA
```

- **Command:** `mvn -B -ntp -e -Dmaven.repo.local=/workspace/cache/m2 [flags] verify`
- **Measure / check:** build_s; test count and failures must match A
- **Status:** Planned · **Effort:** Low
- **Measured locally:** JUnit 5 parallel classes −5 to −12%. forkCount=1C +70 to 80%, reuseForks=false about 10x slower.
- **Expected:** Modest gain; the real value is spotting the anti-patterns in real repos.

### 5. Dependency and Maven-wrapper cache: cold vs warm

- **Objective:** Measure how much of a build is spent downloading libraries and Maven/Gradle themselves, and how much a persistent cache saves a fresh pod.
- **Repos:** all 8 in config/repos.txt
- **Node:** m6a.xlarge spot
- **Pod (CPU / memory):** 2 / 4Gi
- **Variants:**

```
1) DEP_CACHE=none (downloads everything)
2) DEP_CACHE=shared, empty
3) DEP_CACHE=shared, warm
All with MAVEN_USER_HOME=/workspace/cache/m2-home and GRADLE_USER_HOME on the PVC
```

- **Command:** `mvn -B -ntp -e [-Dmaven.repo.local=/workspace/cache/m2] verify (./mvnw or ./gradlew where present)`
- **Measure / check:** build_s, bytes downloaded (from the build log), any 429 errors
- **Status:** Done · **Effort:** Medium
- **Measured locally:** Not measured on EKS. Maven Central returned 429s from the sandbox.
- **Expected:** Removes download time and rate-limit failures; matters most for fresh OpenShift pods.
- **EKS result:** Pilot 22 Nov (1 rep, 24/24 pass): cold vs warm cache: synthetic repos +2 to +40% (most on small repos); petclinic (Maven wrapper) +34 to +42%; Gradle guide +165 to +292%; about 220 to 250 MB downloaded per real repo; no 429 errors. Verdict: keep a shared cache, wrapper distribution included.

### 6. CPU requests vs limits

- **Objective:** Find whether letting a pod burst above its CPU request (limit > request) speeds builds, and how many CPUs the JVM then thinks it has.
- **Repos:** p4, p6
- **Node:** m6a.2xlarge spot
- **Pod (CPU / memory):** CPU request 2 with limit 2 / 4 / none, memory 4Gi
- **Variants:**

```
1) request 2, limit 2 (today)
2) request 2, limit 4 (burstable)
3) request 2, no limit
All with MAVEN_ARGS_EXTRA="-T1C"
Needs separate CPU request and limit in the build Job template
```

- **Command:** `mvn -B -ntp -e -Dmaven.repo.local=/workspace/cache/m2 -T1C verify`
- **Measure / check:** build_s, throttled_pct, CPU count the JVM reports (ActiveProcessorCount)
- **Status:** Planned · **Effort:** Low
- **Measured locally:** Not tested. Build pods use request = limit today.
- **Expected:** Shows how the JVM sizes itself from the limit; relevant for OpenShift quotas.

### 7. javac fork check

- **Objective:** Confirm in-process javac beats a forked javac, and find real repos that fork it.
- **Repos:** p4, then scan the real repos' pom.xml
- **Node:** m6a.xlarge spot
- **Pod (CPU / memory):** 2 / 4Gi
- **Variants:**

```
1) -Dmaven.compiler.fork=false (default)
2) -Dmaven.compiler.fork=true
```

- **Command:** `mvn -B -ntp -e -Dmaven.repo.local=/workspace/cache/m2 [flag] verify`
- **Measure / check:** build_s; list repos that set <fork>true</fork>
- **Status:** Planned · **Effort:** Low
- **Measured locally:** p4: fork=true 61 s vs fork=false 29 s.
- **Expected:** Detect and flag in real repos rather than tune.

### 8. Pod memory and the SerialGC threshold

- **Objective:** Find the smallest pod memory that avoids the slow SerialGC and OOM kills.
- **Repos:** p4, p6
- **Node:** m6a.xlarge spot
- **Pod (CPU / memory):** CPU 2, memory 1536Mi / 2Gi / 4Gi / 8Gi
- **Variants:**

```
BUILD_MEM=1536Mi (JVM picks SerialGC, under 1792 MB) | 2Gi | 4Gi | 8Gi
```

- **Command:** `mvn -B -ntp -e -Dmaven.repo.local=/workspace/cache/m2 verify`
- **Measure / check:** build_s, peak_mem_mib, oom_kills, GC the JVM picked
- **Status:** Later · **Effort:** Low
- **Measured locally:** Not tested.
- **Expected:** Below 1792 MB the JVM picks SerialGC and slows down; only that threshold is worth testing.

### 9. Image pre-pull

- **Objective:** Measure how long image pulls add to a build and whether pre-pulling is worth it.
- **Repos:** all 8
- **Node:** m6a.xlarge spot
- **Pod (CPU / memory):** 2 / 4Gi
- **Variants:**

```
1) cold node (first image pull)
2) images pre-pulled by a DaemonSet
```

- **Command:** `unchanged`
- **Measure / check:** startup_s (not build_s)
- **Status:** Later · **Effort:** Medium
- **Measured locally:** EKS startup_s was 0 to 14 s, mostly the first pull of each image.
- **Expected:** Saves about 10 to 30 s per new image, one-off.

### 10. Build daemon (mvnd)

- **Objective:** Check whether mvnd's warm daemon helps at all when every build runs in a fresh pod.
- **Repos:** p4, p6
- **Node:** m6a.xlarge spot
- **Pod (CPU / memory):** 2 / 4Gi
- **Variants:**

```
1) mvn 3.9.11
2) mvnd 1.x in a custom image, one build per pod
```

- **Command:** `mvnd -B -Dmaven.repo.local=/workspace/cache/m2 verify`
- **Measure / check:** build_s
- **Status:** Later · **Effort:** Medium
- **Measured locally:** Could not run in the sandbox.
- **Expected:** Helps repeated builds; little in one-shot pods.

### 11. Garbage collector choice

- **Objective:** Confirm GC choice barely matters for builds, so we can stop tuning it.
- **Repos:** p4, p6
- **Node:** m6a.xlarge spot
- **Pod (CPU / memory):** 2 / 4Gi
- **Variants:**

```
MAVEN_OPTS unset (G1) | -XX:+UseParallelGC | -XX:+UseSerialGC
```

- **Command:** `mvn -B -ntp -e -Dmaven.repo.local=/workspace/cache/m2 verify`
- **Measure / check:** build_s, peak_mem_mib
- **Status:** Deprioritised · **Effort:** Low
- **Measured locally:** p4: ParallelGC 28.5 s vs default 31.4 s, SerialGC 31.1 s.
- **Expected:** Small; not worth a run of its own.

### 12. JDK version running Maven

- **Objective:** Confirm the JDK version running Maven doesn't change build time.
- **Repos:** p4 (Java 17 code)
- **Node:** m6a.xlarge spot
- **Pod (CPU / memory):** 2 / 4Gi
- **Variants:**

```
image maven:3.9.11-eclipse-temurin-17 | -21 | -25, via java=17|21|25 override in repos.txt
```

- **Command:** `mvn -B -ntp -e -Dmaven.repo.local=/workspace/cache/m2 verify`
- **Measure / check:** build_s
- **Status:** Deprioritised · **Effort:** Low
- **Measured locally:** p4: JDK 17 / 21 / 25 = 29.8 / 29.3 / 29.5 s.
- **Expected:** No difference.

### 13. Remediation verify-build (dependency bump)

- **Objective:** For the real use case (bump a vulnerable library, build to prove it still compiles and tests pass, then raise a PR): find the fastest correct verify-build and how many repos one node verifies per hour.
- **Repos:** p4, p6, petclinic, each with one real library version bumped (e.g. a Jackson or Spring version, in the POM or BOM)
- **Node:** m6a.2xlarge spot
- **Pod (CPU / memory):** 2 / 4Gi
- **Variants:**

```
1) bump, DEP_CACHE=shared and warm (only the bumped artifact downloads)
2) bump, DEP_CACHE=none (cold reference)
3) 1 + plugin skips: -Dmaven.javadoc.skip=true -Dmaven.source.skip=true -Djacoco.skip=true -Dcheckstyle.skip=true -Dspotbugs.skip=true -Dpmd.skip=true
4) 3 with MAX_PARALLEL=1 | 2 | 3 builds side by side
Build cache off (it can't help here)
```

- **Command:** `mvn -B -ntp -e -Dmaven.repo.local=/workspace/cache/m2 [skip flags] verify`
- **Measure / check:** build_s; repos verified per hour per node; downloaded_mb; test count must match the pre-bump baseline
- **Status:** Planned · **Effort:** Medium
- **Measured locally:** Not measured. Related: the pilot measured cold dependencies at +2 to 40% (petclinic +34 to 42%).
- **Expected:** Sizes the remediation pipeline directly: the cheapest correct verify-build and repos per hour per node.

## Baseline spec

| Setting | Value used unless an experiment changes it |
|---|---|
| Cluster | EKS, region ap-south-1, Kubernetes version in standard support |
| Node | 1 spot node, m6a.xlarge (4 vCPU, 16 GiB); m6a.2xlarge (8 vCPU, 32 GiB) when a pod needs 4 CPUs |
| Build pod CPU | 2 (request = limit) |
| Build pod memory | 4Gi (request = limit) |
| Builds at a time | MAX_PARALLEL=1 (one build pod on the node) |
| Build image | maven:3.9.11-eclipse-temurin-<JDK picked by detect.py> (Gradle with gradlew: eclipse-temurin:<JDK>-jdk) |
| Maven command | mvn -B -ntp -e -Dmaven.repo.local=/workspace/cache/m2 verify |
| MAVEN_OPTS / argLine | unset (JVM defaults) |
| Dependency cache | DEP_CACHE=shared on the PVC, warmed by one unmeasured pass first |
| Workspace volume | EBS gp3 PVC, 3000 IOPS / 125 MB/s baseline |
| Repetitions | 3 per variant × repo, variant order shuffled each round |
| Correctness gate | Every repo passes with the same test count as the baseline |
| Recorded per build | build_s, cpu_s, avg_cores, throttled_pct, peak_mem_mib, oom_kills, tests, startup_s |

## Before we trust numbers

| Prerequisite | Status | Why it matters | Where |
|---|---|---|---|
| Resume only within the same run (issue #8) | Done | A second run on a surviving volume reused old timings as new ones. | PR #9 (merged 22 Nov) |
| Pin the spot node group to one AZ | Done | A replacement spot node in the other AZ can't attach the EBS volume, so the run hangs until the 6 h deadline. | PR #10 (merged 22 Nov) |
| Cache the Maven wrapper download (MAVEN_USER_HOME on the PVC) | Done | mvnw re-downloads Maven into the container on every build, adding noise to timings. | PR #10 (merged 22 Nov) |
| MAVEN_OPTS passthrough to build pods | Done | Needed for the JIT tiering, GC and memory experiments. | PR #10 (merged 22 Nov) |
| Experiment runner: experiments.txt, jbp experiment, compare.py | Done | Runs variants on one cluster with 3 shuffled reps and compares median, spread, cost and test counts. | PR #10 (merged 22 Nov) |
| Escape free-text values in YAML templates (issue #7) | Done | A quote in MAVEN_ARGS_EXTRA breaks the build Job manifest; experiments will pass many such args. | PR #10 (merged 22 Nov) |

## Test projects

| Project | Repo | Java | Modules | Layer widths | Classes | Tests | EKS build s (2 CPU, 15 Nov) | Notes |
|---|---|---|---|---|---|---|---|---|
| p1 tiny | perf-p1-tiny-j17 | 17 | 1 | 1 | 30 | 276 | 10.2 | Single module: -T can't help |
| p2 small | perf-p2-small-j21 | 21 | 1 | 1 | 300 | 2760 | 19.5 | Single module, test-heavy |
| p3 medium | perf-p3-medium-j25 | 25 | 6 | 1, 3, 2 | 480 | 4416 | 32.7 |  |
| p4 medium | perf-p4-medium-j17 | 17 | 12 | 1, 4, 4, 3 | 960 | 8832 | 56.2 | Workhorse for most local experiments |
| p5 large | perf-p5-large-j21 | 21 | 24 | 2, 6, 8, 6, 2 | 2880 | 26496 | 130 |  |
| p6 xlarge | perf-p6-xlarge-j25 | 25 | 40 | 2, 8, 12, 10, 6, 2 | 6000 | 55200 | 229.7 | Up to 12 modules can build side by side |
| petclinic | spring-petclinic | 17 | 1 | 1 |  | 79 | 55 | Real Spring Boot app (Maven wrapper) |
| rest-svc-gradle | Gradle guide project | 17 |  |  |  | 2 | 36.6 | Real Gradle build (gradlew) |

Local numbers come from the 2026-11-06 benchmark (4 vCPU cloud VM, cgroup-limited "pods").
EKS numbers come from the 2026-11-15 run in ap-south-1 (spot m6a.xlarge, 2 CPU / 4Gi build pods) and from the
2026-11-22 pilot of experiments 1, 3 and 5 (1 rep, spot m6a.2xlarge): [results/2026-11-22-eks-pilot.html](results/2026-11-22-eks-pilot.html)
(download and open in a browser) and [its CSV](results/2026-11-22-eks-pilot.csv).
