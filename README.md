# Java build optimisation

**Goal:** after a dependency upgrade in `pom.xml` or `build.gradle` (for example a vulnerability fix), prove
that the repo still builds and its tests pass, **as fast and as cheaply as possible**. The build is a check, not
a release: if it passes, a PR is raised.

This repo is the pipeline that runs those builds in Kubernetes pods (target: OpenShift) and the experiments
that measure what makes them faster.

## The four questions

| # | Question | Answer so far | Status |
|---|---|---|---|
| 1 | **The pod has more CPU: can the build use it?** | Up to 2 CPU, yes: 1 → 2 CPU halves build time. Beyond that only with Maven module parallelism: 4 CPU alone −13 to −22%, 4 CPU with `-T1C` −26 to −30%. `-T1C` makes a 2-CPU pod 5 to 11% *slower*. `-T1C` does nothing for single-module repos. | Measured on EKS |
| 2 | **Can we avoid downloading third-party packages every time?** | Yes: keep them on a **PVC** mounted into every build pod (`-Dmaven.repo.local=/cache/m2`, `MAVEN_USER_HOME` and `GRADLE_USER_HOME` on it). Cold downloads cost +2 to 40% on the synthetic repos, +34 to 42% on Spring PetClinic, +165 to 292% on a Gradle project. After an upgrade only the new artifact downloads. | Measured on EKS |
| 3 | **Can we use the build cache?** | Not for this use case. It saves −90 to −98% when the same repo is rebuilt with few changes, but upgrade builds hit cold repos, and a version bump in a parent POM/BOM invalidates every module (measured: ~0% gain). A cache hit also skips tests, which defeats the proof. | Measured on EKS |
| 4 | **Can we skip plugins that don't prove anything?** | Likely yes: javadoc, source jars, coverage, static analysis, signing, image builds. Keep compiler, tests and enforcer. | Not measured yet (experiment 13) |

### The resulting build command

```bash
# pod: CPU request = limit = 2, memory 4Gi; /cache is a PVC that keeps third-party packages between builds
export MAVEN_USER_HOME=/cache/m2-home          # Maven wrapper download, cached
mvn -B -ntp -e -Dmaven.repo.local=/cache/m2 \
    -Dmaven.javadoc.skip=true -Dmaven.source.skip=true -Djacoco.skip=true \
    -Dcheckstyle.skip=true -Dspotbugs.skip=true -Dpmd.skip=true \
    verify
# no -T1C at 2 CPU, no build cache

# Gradle
GRADLE_USER_HOME=/cache/gradle ./gradlew build --no-daemon -x javadoc -x jacocoTestReport
```

Every flag, the variants for other cases (more CPU, build cache) and the evidence behind each:
[docs/experiments/production-recipes.csv](docs/experiments/production-recipes.csv) (also written out in
[docs/experiments/README.md](docs/experiments/README.md#production-recipes)).

## How it measures

```
./jbp experiment --reps 3
  cluster up (1 spot node) -> warm-up -> each variant in config/experiments.txt, repeated -> comparison.html -> cluster down
```

Each build runs in its own pod with a fixed CPU/memory limit and records build time, CPU used, CPU throttling,
peak memory, tests run and MB downloaded. Variants change one setting at a time; the report compares medians
against a baseline and checks the test count didn't drop.

| Test bed | Command | Cost |
|---|---|---|
| Docker on your machine | `./jbp experiment --local` or `./jbp local-docker` | free |
| kind (Kubernetes in Docker) | `./jbp local-kind up / run / down` | free |
| Ephemeral EKS on AWS | `./jbp experiment`, `./jbp all` | ~$0.22/h while up; the 3-experiment pilot cost $0.65 |

The cluster is always torn down at the end, also after a failure or Ctrl-C; `./jbp leftovers` checks nothing
is still billing.

## Results

- [EKS pilot, 22 Nov 2026](docs/experiments/results/2026-11-22-eks-pilot.html): experiments 1, 3, 5, 59/59 builds passed (1 rep; download and open in a browser).
- [Experiment plan and tracker](docs/experiments/README.md): every experiment with objective, exact flags and result.

## Quick start

Needs `python3` ≥ 3.9 (standard library only); for EKS also `aws` (with credentials), `eksctl` ≥ 0.200 and `kubectl`.

```bash
./jbp experiment --local --reps 1    # free rehearsal of the experiment plan with Docker
./jbp experiment --reps 3            # the same on EKS (costs money; tears down at the end)
./jbp leftovers                      # anything still billing?
python3 -m unittest discover -s tests
```

Repos to build: `config/repos.txt`. Defaults: `config/settings.env` (any key can be overridden by an env var).

## Layout

```
jbp                  entry point (./jbp --help)
pipeline/            cli.py (driver) · experiment.py (plans) · cluster.py (EKS up/down) · orchestrator.py ·
                     detect.py (Maven/Gradle, Java version) · jbpcore.py · build.sh (runs inside the build pod)
config/              repos.txt · experiments.txt · settings.env · images.env
k8s/                 Job, PVC, RBAC and StorageClass manifests
report/              make_report.py (one run) · compare.py (an experiment session)
docs/                experiments/ (plan, recipes, results) · operations.md (logging, errors, settings, cost) ·
                     architecture.html · code-reference.html
```
