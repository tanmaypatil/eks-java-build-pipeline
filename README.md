# Ephemeral EKS Java build pipeline

Spin up a throwaway Kubernetes cluster on AWS, build a list of Java repos inside it, get an HTML
report, tear the cluster down. Built to make each run as cheap as possible first, so build
optimisations (`-T1C`, JVM flags, caches, pod sizes) can be compared later on the same rig.

```
bin/all.sh
  ├─ bin/up.sh        eksctl: EKS + 1 spot node (public subnets, no NAT) + EBS CSI      ~15-20 min
  ├─ bin/run.sh       on the cluster:
  │    └─ Job jbp-orchestrator (alpine/k8s)
  │         for each repo in config/repos.txt, one by one:
  │           git clone --depth 1  ->  PVC /workspace/repos/<name>
  │           scripts/detect.sh    ->  maven|gradle, wrapper?, Java version needed
  │           Job jbp-build-<name> in the matching image
  │              maven:3.9.11-eclipse-temurin-{11,17,21,25} | eclipse-temurin:<v>-jdk (gradlew) | gradle:<ver>-jdk<v>
  │              scripts/build.sh: mvn -B verify / gradle build, timings, cgroup CPU + memory, JUnit counts
  │    └─ copy /workspace/results off the PVC  ->  runs/<RUN_ID>/report.html + summary.csv
  └─ bin/down.sh      delete namespace (frees EBS) -> eksctl delete cluster -> leftover check  ~10 min
```

Teardown is in a `trap`, so it runs on failure and on Ctrl-C too.

## Quick start

Prerequisites on your machine: `aws` (configured credentials), `eksctl` ≥ 0.200, `kubectl`, `python3`.

```bash
# 1. free dry run with Docker only (no cluster): same scripts, a local folder stands in for the PVC
bin/local-docker.sh config/repos.txt

# 2. free rehearsal on real Kubernetes (Docker + kind)
bin/local-kind.sh up && bin/local-kind.sh run && bin/local-kind.sh down

# 3. the real thing on AWS (costs money, see below)
bin/all.sh                # up -> run -> report -> down
bin/all.sh --keep         # leave the cluster up for several runs; then bin/down.sh
bin/leftovers.sh          # anything still billing? (--delete removes orphaned volumes)
```

## The repo list

`config/repos.txt`, one repo per line:

```
# name           url                                               ref  subdir    overrides
petclinic        https://github.com/spring-projects/spring-petclinic.git  -  -
rest-svc-gradle  https://github.com/spring-guides/gs-rest-service.git     -  complete  tool=gradle
```

`ref` is a branch/tag/sha (`-` = default branch). `subdir` builds a sub-folder. Overrides:
`tool=maven|gradle` (for repos that have both, Maven wins by default) and `java=21`.
Private repos: put a token in the URL via a Kubernetes Secret (not wired yet; easy to add).

The default list builds the six synthetic benchmark repos (`tanmaypatil/perf-p1-tiny-j17` … `perf-p6-xlarge-j25`)
plus Spring PetClinic (Maven wrapper) and a Gradle-wrapper guide in a subfolder.

## Java version detection

`scripts/detect.sh`, first hit wins:

1. `.java-version`, `.sdkmanrc`, `.tool-versions`, `.mvn/jdk`
2. Maven (highest across all modules): `maven.compiler.release`, compiler-plugin `<release>`,
   `java.version` (Spring Boot), `maven.compiler.target/source`, `.mvn/toolchains.xml`
3. Gradle: `JavaLanguageVersion.of(N)`, `jvmToolchain(N)`, `options.release`,
   `JavaVersion.VERSION_N`, `sourceCompatibility`
4. Default 17

The build then runs on the **smallest supported JDK ≥ the version needed** (11/17/21/25; edit
`config/images.env`). `1.8` maps to JDK 11, which still compiles `--release 8`.
`test/detect_test.sh` covers 17 cases.

## Settings (`config/run.env`, all overridable by env var)

| Setting | Default | Notes |
|---|---|---|
| `INSTANCE_TYPES` | m7a/m6a/m7i/m6i/m5/m5a `.xlarge` | 4 vCPU / 16 GiB, several types so spot capacity is easy |
| `NODE_COUNT` | 1 | the PVC is EBS (ReadWriteOnce) so builds share one node |
| `K8S_VERSION` | auto | newest version in EKS **standard** support. Extended support costs $0.60/h instead of $0.10/h |
| `BUILD_CPU` / `BUILD_MEM` | 2 / 4Gi | request = limit, like a Guaranteed pod on OpenShift |
| `MAX_PARALLEL` | 1 | builds strictly one by one. 2-3 on a 2xlarge node shortens wall time |
| `DEP_CACHE` | shared | `~/.m2` and `GRADLE_USER_HOME` on the PVC, reused across repos. `none` = cold builds |
| `MAVEN_ARGS_EXTRA` | | e.g. `-T1C`, `-DskipTests` for optimisation runs |
| `IMAGE_REGISTRY` | `public.ecr.aws/docker/library/` | AWS mirror of Docker official images: no Docker Hub rate limit |

Spot reclaims are handled: Jobs use a `podFailurePolicy` that retries pods hit by a
`DisruptionTarget` without counting it as a failure, and a restarted orchestrator skips repos
that already have results.

## Report

`runs/<RUN_ID>/report.html` (self-contained) and `summary.csv`. Per repo: pass/fail, tool,
Java needed and where that came from, image, clone time, wait + image pull time, build time,
test counts, average cores used vs the CPU limit, CPU throttling %, peak memory vs the limit,
and a link to the full build log. Cost section: measured cost of the run (when it went through
`bin/all.sh`) and a projection for an ephemeral EKS run of the same workload.

## Cost per run (us-east-1, on-demand list prices Nov 2026, spot is approximate)

What a typical run bills: control plane for the whole up → down window, the spot node from
when it starts to when it's drained, EBS, a public IPv4 per node. No NAT gateway, no load
balancer, no CloudWatch logs.

| Run | Control plane ($0.10/h) | 1 spot xlarge (~$0.07/h) | EBS + IPv4 | **Total** |
|---|---|---|---|---|
| 4 small repos, ~10 min of builds (≈ 45 min end to end) | $0.075 | $0.03 | < $0.01 | **≈ $0.11** |
| 10 medium repos, ~45 min of builds (≈ 80 min) | $0.13 | $0.07 | < $0.01 | **≈ $0.21** |
| same, node on-demand instead of spot (~$0.19/h) | $0.13 | $0.18 | < $0.01 | ≈ $0.32 |

Fixed overhead per run is ~30 min of cluster create + delete (≈ $0.06), which is mostly
waiting rather than money. Keeping a cluster up permanently costs ~$73/month for the
control plane alone, so ephemeral is cheaper unless you run many times a day; in that case use
`--keep` and scale the node group to 0 between runs.

Watch-outs that cost real money: an EKS version that has dropped into extended support (6x control
plane price), a NAT gateway (avoided here), EBS volumes left after a failed teardown
(`bin/leftovers.sh` finds them, they're tagged `jbp-cluster=<name>`).

## Next optimisations (phase 2)

- Feed in the earlier benchmark's wins: `MAVEN_ARGS_EXTRA=-T1C`, `MAVEN_OPTS=-XX:TieredStopAtLevel=1`, JUnit parallel.
- Size pods from the report: avg cores ≪ limit means the limit can shrink, throttling ≫ 0 means it's too small.
- Pre-pull build images (DaemonSet) or bake an AMI so wait+pull drops to ~0.
- Maven Central rate-limits (HTTP 429) bursts from one IP; we hit it in testing. Put a mirror in front
  (CodeArtifact / Nexus) or persist the dependency cache between runs (EBS snapshot or S3) instead of re-downloading.
- Parallel builds on a bigger node (`MAX_PARALLEL=3`, `INSTANCE_TYPES=m7a.2xlarge,...`), or EFS for multi-node.
- Cheaper still for experiments only: k3s on a single spot EC2 instance (no $0.10/h control plane, ~2 min start).

## Layout

```
bin/        up.sh run.sh down.sh all.sh leftovers.sh local-docker.sh local-kind.sh
scripts/    orchestrator.sh detect.sh build.sh     (mounted into pods via ConfigMap)
k8s/        orchestrator-job.yaml build-job.tpl.yaml rbac.yaml pvc.yaml storageclass-eks.yaml collector-pod.yaml
config/     repos.txt images.env run.env
report/     make_report.py                            (stdlib only)
test/       detect_test.sh render_test.sh
```
