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

## Experiments (ranked)

| Rank | Experiment | Repos | Pod (CPU / memory) | Status | Effort |
|---|---|---|---|---|---|
| 1 | Pod CPU size × Maven module parallelism | p1 (single-module control), p3, p4, p5, p6 | CPU 1 / 2 / 4 (request = limit), memory 4Gi | Planned | Low |
| 2 | JIT tiering (stop at the fast C1 compiler) | p2, p4, p6, petclinic | 2 / 4Gi | Planned | Low |
| 3 | Incremental build cache | p3, p5, p6 (re-add .mvn/extensions.xml with maven-build-cache-extension 1.2.0) | 2 / 4Gi | Needs design | High |
| 4 | Test execution settings | p2, p4, p5 | 2 / 4Gi | Planned | Low |
| 5 | Dependency and Maven-wrapper cache: cold vs warm | all 8 in config/repos.txt | 2 / 4Gi | Blocked | Medium |
| 6 | CPU requests vs limits | p4, p6 | CPU request 2 with limit 2 / 4 / none, memory 4Gi | Planned | Low |
| 7 | javac fork check | p4, then scan the real repos' pom.xml | 2 / 4Gi | Planned | Low |
| 8 | Pod memory and the SerialGC threshold | p4, p6 | CPU 2, memory 1536Mi / 2Gi / 4Gi / 8Gi | Later | Low |
| 9 | Image pre-pull | all 8 | 2 / 4Gi | Later | Medium |
| 10 | Build daemon (mvnd) | p4, p6 | 2 / 4Gi | Later | Medium |
| 11 | Garbage collector choice | p4, p6 | 2 / 4Gi | Deprioritised | Low |
| 12 | JDK version running Maven | p4 (Java 17 code) | 2 / 4Gi | Deprioritised | Low |

## Test spec per experiment

### 1. Pod CPU size × Maven module parallelism

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
- **Status:** Planned · **Effort:** Low
- **Measured locally:** p4: 101 s at 1 CPU, 50 s at 2, 29 s at 4. -T1C gave −45% on p6 with 4 CPUs but about 0% in a 2-CPU pod (52 s vs 51 s).
- **Expected:** Biggest speed lever. Shows the right pod size for OpenShift; cost per build stays roughly flat while feedback gets about 2x faster per CPU doubling.

### 2. JIT tiering (stop at the fast C1 compiler)

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
- **Status:** Needs design · **Effort:** High
- **Measured locally:** p6: 108 s → 2.8 s with no changes (−97%), 10 s with a leaf module edited (−90%); a root-module edit gives no gain.
- **Expected:** Largest number we have, for repeated builds of the same repo.

### 4. Test execution settings

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
- **Status:** Blocked · **Effort:** Medium
- **Measured locally:** Not measured on EKS. Maven Central returned 429s from the sandbox.
- **Expected:** Removes download time and rate-limit failures; matters most for fresh OpenShift pods.

### 6. CPU requests vs limits

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
| Resume only within the same run (issue #8) | PR open | A second run on a surviving volume reused old timings as new ones. | PR #9 |
| Pin the spot node group to one AZ | To do | A replacement spot node in the other AZ can't attach the EBS volume, so the run hangs until the 6 h deadline. | cluster.py eksctl_config |
| Cache the Maven wrapper download (MAVEN_USER_HOME on the PVC) | To do | mvnw re-downloads Maven into the container on every build, adding noise to timings. | build.sh |
| MAVEN_OPTS passthrough to build pods | To do | Needed for the JIT tiering, GC and memory experiments. | build.sh, build-job template |
| Experiment runner: experiments.txt, jbp experiment, compare.py | To do | Runs variants on one cluster with 3 shuffled reps and compares median, spread, cost and test counts. | config/, pipeline/cli.py, report/ |
| Escape free-text values in YAML templates (issue #7) | To do | A quote in MAVEN_ARGS_EXTRA breaks the build Job manifest; experiments will pass many such args. | cli.render, orchestrator render |

## Test projects

| Project | Repo | Java | Modules | Layer widths | Classes | Tests | EKS build s (2 CPU, 15 Nov) | Notes |
|---|---|---|---|---|---|---|---|---|
| p1 tiny | perf-p1-tiny-j17 | 17 | 1 | 1 | 30 | 276 | 10.2 | Single module: -T can't help |
| p2 small | perf-p2-small-j21 | 21 | 1 | 1 | 300 | 2760 | 19.5 | Single module, test-heavy |
| p3 medium | perf-p3-medium-j25 | 25 | 6 | 1, 3, 2 | 480 | 4416 | 32.7 |  |
| p4 medium | perf-p4-medium-j17 | 17 | 12 | 1, 4, 4, 3 | 960 | 8832 | 56.2 | Workhorse for most local experiments |
| p5 large | perf-p5-large-j21 | 21 | 24 | 2, 6, 8, 6, 2 | 2880 | 26496 | 130.0 |  |
| p6 xlarge | perf-p6-xlarge-j25 | 25 | 40 | 2, 8, 12, 10, 6, 2 | 6000 | 55200 | 229.7 | Up to 12 modules can build side by side |
| petclinic | spring-petclinic | 17 | 1 | 1 |  | 79 | 55.0 | Real Spring Boot app (Maven wrapper) |
| rest-svc-gradle | Gradle guide project | 17 |  |  |  | 2 | 36.6 | Real Gradle build (gradlew) |

Local numbers come from the 2026-11-06 benchmark (4 vCPU cloud VM, cgroup-limited "pods");
EKS numbers from the 2026-11-15 run in ap-south-1 (spot m6a.xlarge, 2 CPU / 4Gi build pods).
