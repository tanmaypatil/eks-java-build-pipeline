# Ephemeral EKS Java build pipeline

Spin up a throwaway Kubernetes cluster on AWS, build a list of Java repos inside it, get an HTML
report, tear the cluster down. Built to make each run as cheap as possible first, so build
optimisations (`-T1C`, JVM flags, caches, pod sizes) can be compared later on the same rig.

```
./jbp all
  ├─ UP           eksctl: EKS + 1 spot node (public subnets, no NAT) + EBS CSI            ~15-20 min
  ├─ DEPLOY       ConfigMaps (code, repo list, settings), RBAC, PVC, orchestrator Job
  ├─ ORCHESTRATE  Job jbp-orchestrator (alpine/k8s, python3 pipeline/orchestrator.py)
  │                 for each repo in config/repos.txt, one by one:
  │                   CLONE    git clone --depth 1 -> PVC /workspace/repos/<name>   (retried on network errors)
  │                   DETECT   detect.py: maven|gradle, wrapper?, Java version needed -> JDK -> image
  │                   LAUNCH   Job jbp-build-<name> in the matching image
  │                   WAIT     build.sh: mvn -B verify / gradle build, timings, cgroup CPU + memory, JUnit counts
  │                            (fails early on ImagePullBackOff / Unschedulable instead of waiting for the timeout)
  │                   COLLECT  results/<name>.json
  ├─ COLLECT      copy /workspace/results off the PVC
  ├─ REPORT       runs/<RUN_ID>/report.html + summary.csv
  └─ DOWN         delete namespace (frees EBS) -> eksctl delete -> poll AWS until nothing is left  ~10 min
```

Full architecture write-up: [`docs/architecture.html`](docs/architecture.html) (download and open in a browser).

DOWN always runs (`try/finally` plus a SIGTERM handler), also after a failure or Ctrl-C.

## Where is my run? (logging and tracing)

- **Console + `runs/<RUN_ID>/driver.log`**: every line is `time level component [repo|STAGE] message`, e.g.
  `2026-11-14T11:28:26Z INFO  orchestrator [perf-p2-small-j21|DETECT] tool=maven, needs Java 21 (.mvn/jdk) -> JDK 21`.
  The orchestrator's lines are streamed from the pod with a `  | ` prefix. The log file also has every command run, with retries.
- **`./jbp status [RUN_ID]`**: the driver's phase plus the stage of every repo, read live from the orchestrator pod
  (or from the collected results once the run is over).
- **Files**: `driver_status.json` / `driver_events.jsonl` (driver phases) and `results/_status.json` /
  `results/events.jsonl` / `results/orchestrator.log` (per-repo stages, timestamps, errors).

## Errors and recovery

| What fails | What happens |
|---|---|
| git clone network error / HTTP 5xx / 429 | retried `CLONE_RETRIES` times with growing back-off, then the repo is marked FAILED (clone) |
| `kubectl` call to the API | retried; the build watch tolerates 12 failed reads in a row before giving up |
| image can't be pulled | build failed after `IMAGE_PULL_GRACE_S` (180 s) as `image`, not after the 30 min timeout |
| pod can't be scheduled | failed after `UNSCHEDULABLE_GRACE_S` (600 s) as `unschedulable`, with the scheduler's reason |
| build or tests fail / time out | `build` / `timeout`, with the first error lines in the report |
| one repo fails | logged and recorded; the run carries on with the next repo |
| spot reclaim / orchestrator restart | Job `podFailurePolicy` retries it; repos that passed are skipped, failed ones are retried (resume) |
| teardown incomplete | `jbp down` polls AWS for up to 20 min and lists whatever is still billing |

## Quick start

Prerequisites wherever you run `jbp` (your laptop, or the Claude cloud session): `python3` ≥ 3.9, `aws` CLI with
credentials, `eksctl` ≥ 0.200, `kubectl`. No Python packages needed (standard library only).

```bash
# 1. free dry run with Docker only (no cluster): same orchestrator + build.sh, a local folder stands in for the PVC
./jbp local-docker

# 2. free rehearsal on real Kubernetes (Docker + kind)
./jbp local-kind up && ./jbp local-kind run && ./jbp local-kind down

# 3. the real thing on AWS (costs money, see below)
./jbp all                 # up -> run -> report -> down
./jbp all --keep          # leave the cluster up for several runs; then ./jbp down
./jbp status              # where is the latest run right now
./jbp leftovers           # anything still billing? (--delete removes orphaned volumes)

# any setting can be overridden per run
BUILD_CPU=4 BUILD_MEM=8Gi MAVEN_ARGS_EXTRA=-T1C ./jbp run --repos config/my-repos.txt
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

`pipeline/detect.py` (try it: `python3 pipeline/detect.py <repo-dir>`), first hit wins:

1. `.java-version`, `.sdkmanrc`, `.tool-versions`, `.mvn/jdk`
2. Maven, `pom.xml` parsed as XML with `${property}` references resolved (also from a local parent pom),
   highest across all modules: `maven.compiler.release`, compiler-plugin `<release>`,
   `java.version` (Spring Boot), `maven.compiler.target/source`, `.mvn/toolchains.xml`
3. Gradle: `JavaLanguageVersion.of(N)`, `jvmToolchain(N)`, `options.release`,
   `JavaVersion.VERSION_N`, `sourceCompatibility`
4. Default 17

The build then runs on the **smallest supported JDK ≥ the version needed** (11/17/21/25; edit
`config/images.env`). `1.8` maps to JDK 11, which still compiles `--release 8`.
Tests: `python3 -m unittest discover -s tests` (39 tests: detection, repo-list parsing, pod-failure
classification, stage tracking, manifest rendering).

## Settings (`config/settings.env`, plain `KEY=value`, each overridable by an env var of the same name)

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
that already passed and retries the ones that failed.

## Report

`runs/<RUN_ID>/report.html` (self-contained) and `summary.csv`. Per repo: pass/fail, tool,
Java needed and where that came from, image, clone time, wait + image pull time, build time,
test counts, average cores used vs the CPU limit, CPU throttling %, peak memory vs the limit,
and a link to the full build log. Cost section: measured cost of the run (when it went through
`jbp all`) and a projection for an ephemeral EKS run of the same workload.

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
(`jbp leftovers` finds them, they're tagged `jbp-cluster=<name>`).

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
jbp                 entry point (./jbp --help)
pipeline/           cli.py (driver) · cluster.py (eksctl up/down, leftovers poller) · orchestrator.py ·
                    detect.py · jbpcore.py (settings, logging, retries, stage tracking) · build.sh
                    -> everything in pipeline/ is mounted into the pods via the jbp-code ConfigMap
k8s/                orchestrator-job.yaml build-job.tpl.yaml rbac.yaml pvc.yaml storageclass-eks.yaml collector-pod.yaml
config/             repos.txt images.env settings.env
report/             make_report.py
tests/              unit tests (python3 -m unittest discover -s tests)
docs/               architecture.html
```

`build.sh` stays a shell script on purpose: it runs inside the stock Maven/Temurin images, which have no Python.
