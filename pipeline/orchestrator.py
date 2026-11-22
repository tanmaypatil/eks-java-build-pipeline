"""Orchestrator: for each repo in the list, one by one:

    CLONE   git clone --depth 1 onto the shared volume (/workspace/repos/<name>), retried on network errors
    DETECT  build tool + Java version (detect.py) -> JDK -> image
    LAUNCH  create the build Job (k8s) or container (docker) running build.sh in that image
    WAIT    until it succeeds / fails; fail early on image-pull or scheduling errors
    COLLECT merge orchestrator facts + the build's own measurements -> results/<name>.json
    DONE / FAILED

Every stage change is written to results/_status.json (current state) and results/events.jsonl
(history), and logged as  <time> <level> orchestrator [<repo>|<STAGE>] <message>.
A repo that fails never stops the run. On restart (e.g. spot reclaim), repos that already have a
result are skipped, so the run resumes where it stopped.

Runs in-cluster (LAUNCHER=k8s, image alpine/k8s) or on a laptop with Docker (LAUNCHER=docker).
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import signal
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from detect import detect  # noqa: E402
from jbpcore import (CommandError, JbpError, Settings, StageTracker, fmt_duration, load_images,  # noqa: E402
                     now_ms, read_json, repo_logger, run, setup_logging, utc_iso, write_json_atomic)

HERE = Path(__file__).resolve().parent

# Settings passed to build.sh as env vars: template placeholder -> settings key. Their values are free
# text, so render() escapes them for YAML.
FREE_TEXT_ENV = {"MAVEN_ARGS": "MAVEN_ARGS_EXTRA", "GRADLE_ARGS": "GRADLE_ARGS_EXTRA", "MAVEN_OPTS": "MAVEN_OPTS_EXTRA",
                 "BUILD_CACHE": "BUILD_CACHE", "BUILD_CACHE_EDIT": "BUILD_CACHE_EDIT"}
# ...and the env var name build.sh reads for each placeholder.
BUILD_ENV_NAME = {"MAVEN_ARGS": "MAVEN_ARGS_EXTRA", "GRADLE_ARGS": "GRADLE_ARGS_EXTRA", "MAVEN_OPTS": "MAVEN_OPTS",
                  "BUILD_CACHE": "BUILD_CACHE", "BUILD_CACHE_EDIT": "BUILD_CACHE_EDIT"}
# CLEAR_CACHE item -> folders under /workspace/cache that it empties.
CACHE_DIRS = {"deps": ("m2", "m2-home", "gradle"), "build": ("build-cache",)}


def yaml_dq(value: str) -> str:
    """Escape text for use inside a double-quoted YAML scalar."""
    return str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


def clear_caches(ws: Path, which: str) -> list[str]:
    """Empty the caches named in CLEAR_CACHE (e.g. "deps,build"), so an experiment starts cold."""
    cleared = []
    for item in (w.strip() for w in which.split(",") if w.strip()):
        if item not in CACHE_DIRS:
            raise JbpError(f"CLEAR_CACHE: unknown cache {item!r} (use {', '.join(CACHE_DIRS)})")
        for d in CACHE_DIRS[item]:
            shutil.rmtree(ws / "cache" / d, ignore_errors=True)
            cleared.append(d)
    return cleared


log = logging.getLogger("orchestrator")


# --------------------------------------------------------------------------- inputs

@dataclass
class RepoSpec:
    name: str
    url: str
    ref: str = ""
    subdir: str = ""
    tool: str | None = None
    java: int | None = None
    line: int = 0


def parse_repos(path: Path) -> tuple[list[RepoSpec], list[str]]:
    """repos.txt: `name url [ref|-] [subdir|-] [tool=maven|gradle] [java=N]`. Returns (specs, problems)."""
    specs, problems, seen = [], [], set()
    for n, raw in enumerate(path.read_text().splitlines(), 1):
        # Strip comments: whole-line "#..." and trailing "  # ..." (URLs never contain " #").
        line = raw.split("#", 1)[0].strip() if not raw.lstrip().startswith("#") else ""
        if not line:
            continue
        parts = line.split()
        if len(parts) < 2:
            problems.append(f"line {n}: need at least 'name url': {raw.strip()!r}")
            continue
        s = RepoSpec(parts[0], parts[1], line=n)
        # Tokens after the URL: those WITHOUT "=" are positional (ref, then subdir; "-" = default),
        # those WITH "=" are overrides. So "name url tool=gradle" works without writing "- -".
        pos = [p for p in parts[2:] if "=" not in p]
        if len(pos) > 0 and pos[0] != "-":
            s.ref = pos[0]
        if len(pos) > 1 and pos[1] != "-":
            s.subdir = pos[1]
        for kv in (p for p in parts[2:] if "=" in p):
            k, v = kv.split("=", 1)
            if k == "tool" and v in ("maven", "gradle"):
                s.tool = v
            elif k == "java" and v.isdigit():
                s.java = int(v)
            else:
                problems.append(f"line {n}: unknown override {kv!r} (use tool=maven|gradle or java=N)")
        # Problems are collected, not raised: one bad line is skipped and the rest of the list still runs.
        if s.name in seen:
            problems.append(f"line {n}: duplicate name {s.name!r}, skipped")
            continue
        seen.add(s.name)
        specs.append(s)
    return specs, problems


@dataclass
class Plan:
    tool: str
    wrapper: bool
    java_required: int
    java_source: str
    jdk: int
    image: str
    workdir: Path
    notes: list[str] = field(default_factory=list)


@dataclass
class Outcome:
    ok: bool
    failure_kind: str | None = None   # build | timeout | image | unschedulable | infra
    message: str = ""
    attempts: int = 1


# --------------------------------------------------------------------------- launchers

class K8sLauncher:
    """Creates one Job per repo from the template and watches it."""

    FATAL_WAIT_REASONS = {"ImagePullBackOff", "ErrImagePull", "InvalidImageName", "CreateContainerConfigError",
                          "CreateContainerError"}

    def __init__(self, cfg: Settings, template: Path, results: Path, run_id: str):
        self.cfg, self.template, self.results, self.run_id = cfg, template.read_text(), results, run_id
        self.ns = cfg["NAMESPACE"]

    def job_name(self, repo: str) -> str:
        # Kubernetes names must be lowercase DNS labels (a-z, 0-9, "-") of at most 63 characters.
        safe = "".join(c if c.isalnum() else "-" for c in repo.lower()).strip("-")
        return f"jbp-build-{safe}"[:63].rstrip("-")

    def render(self, spec: RepoSpec, plan: Plan) -> str:
        values = {
            "JOB": self.job_name(spec.name), "NAMESPACE": self.ns, "IMAGE": plan.image, "REPO": spec.name,
            "TOOL": plan.tool, "WRAPPER": "yes" if plan.wrapper else "no", "JAVA": str(plan.jdk),
            "WORKDIR": str(plan.workdir), "RUN_ID": self.run_id, "CPU": self.cfg["BUILD_CPU"],
            "MEM": self.cfg["BUILD_MEM"], "TIMEOUT": self.cfg["BUILD_TIMEOUT_S"], "DEP_CACHE": self.cfg["DEP_CACHE"],
            "PVC": self.cfg.get("PVC_NAME", "jbp-workspace"),
        }
        # Free text from settings lands inside a double-quoted YAML string, so a quote or backslash in it
        # (e.g. MAVEN_ARGS_EXTRA=-DargLine="-Xmx1g") must be escaped or the manifest breaks.
        for key, setting in FREE_TEXT_ENV.items():
            values[key] = yaml_dq(self.cfg.get(setting, ""))
        out = self.template
        for k, v in values.items():
            out = out.replace(f"__{k}__", v)
        return out

    def kubectl(self, *args, rlog, **kw):
        return run(["kubectl", "-n", self.ns, *args], log=rlog, **kw)

    def launch_and_wait(self, spec: RepoSpec, plan: Plan, rlog, tracker: StageTracker) -> Outcome:
        job = self.job_name(spec.name)
        manifest = self.render(spec, plan)
        (self.results / f"{spec.name}.job.yaml").write_text(manifest)   # keep exactly what ran, for debugging
        # A Job's pod template is immutable, so a Job left over from an earlier attempt must be deleted
        # (and gone) before the new one can be applied under the same name.
        self.kubectl("delete", "job", job, "--ignore-not-found", "--wait=true", rlog=rlog, check=False, timeout=120)
        self.kubectl("apply", "-f", "-", input=manifest, rlog=rlog, retries=3, timeout=60)
        rlog.info("job %s created (image %s, %s CPU / %s)", job, plan.image, self.cfg["BUILD_CPU"], self.cfg["BUILD_MEM"])
        tracker.repo(spec.name, "WAIT", job=job)
        rlog.extra["stage"] = "WAIT"

        # Our own safety-net timeout. The Job's activeDeadlineSeconds (BUILD_TIMEOUT_S) normally fires
        # first; the extra 10 min covers image pull and scheduling time, which that deadline also counts.
        timeout = self.cfg.int("BUILD_TIMEOUT_S") + 600
        pull_grace, sched_grace = self.cfg.int("IMAGE_PULL_GRACE_S"), self.cfg.int("UNSCHEDULABLE_GRACE_S")
        t0, api_errors, last_note, bad_since = time.time(), 0, "", {}
        outcome = None
        while outcome is None:
            time.sleep(5)
            elapsed = time.time() - t0
            try:
                j = json.loads(self.kubectl("get", "job", job, "-o", "json", rlog=rlog, timeout=30).stdout)
                pods = json.loads(self.kubectl("get", "pods", "-l", f"job-name={job}", "-o", "json",
                                               rlog=rlog, timeout=30).stdout)["items"]
                api_errors = 0
            except (CommandError, ValueError) as e:
                # A single failed API read is normal (API server restart, network blip). Only give up after
                # 12 in a row (~1 min at 5 s per poll), then report it as an infrastructure failure.
                api_errors += 1
                rlog.warning("could not read job status (%d in a row): %s", api_errors, e)
                if api_errors >= 12:
                    outcome = Outcome(False, "infra", f"lost contact with the Kubernetes API: {e}")
                continue

            st = j.get("status", {})
            conds = {c["type"]: c for c in st.get("conditions", []) if c.get("status") == "True"}
            # Decision ladder, first match wins:
            #   Job succeeded                    -> PASS
            #   Job Failed, DeadlineExceeded     -> timeout (activeDeadlineSeconds hit)
            #   Job Failed, any other reason     -> build   (build.sh exited non-zero -> podFailurePolicy FailJob)
            #   our own timeout passed           -> timeout
            #   otherwise inspect the pod        -> may fail early on image / scheduling problems
            if st.get("succeeded", 0) >= 1:
                outcome = Outcome(True)
            elif "Failed" in conds:
                reason = conds["Failed"].get("reason", "")
                kind = "timeout" if reason == "DeadlineExceeded" else "build"
                outcome = Outcome(False, kind, conds["Failed"].get("message", reason))
            elif elapsed > timeout:
                outcome = Outcome(False, "timeout", f"no result after {fmt_duration(elapsed)}")
            else:
                note, fatal = self._pod_problem(pods, bad_since, pull_grace, sched_grace)
                if fatal:
                    outcome = fatal
                elif note != last_note:
                    rlog.info("%s", note)
                    last_note = note

        if pods:
            pods.sort(key=lambda p: p["metadata"]["creationTimestamp"])
            (self.results / f"{spec.name}.pod.json").write_text(json.dumps(pods[-1]))
            # Every pod is one attempt: a spot reclaim (DisruptionTarget) makes the Job start a new pod.
            outcome.attempts = len(pods)
        # These failures leave the Job retrying or Pending in the background; delete it so it stops using
        # the node. Build/timeout failures are already terminal, so their Job is kept for inspection.
        if not outcome.ok and outcome.failure_kind in ("image", "unschedulable", "infra"):
            self.kubectl("delete", "job", job, "--wait=false", rlog=rlog, check=False, timeout=60)
        return outcome

    def _pod_problem(self, pods, bad_since, pull_grace, sched_grace):
        """Describe what the pod is doing; return (note, fatal Outcome or None)."""
        if not pods:
            return "waiting for the pod to be created", None
        pod = sorted(pods, key=lambda p: p["metadata"]["creationTimestamp"])[-1]   # newest attempt only
        name, now = pod["metadata"]["name"], time.time()
        # bad_since remembers when each (pod, problem) was first seen. A problem only becomes fatal after its
        # grace period, because both can clear on their own (node still joining, registry briefly slow).
        phase = pod.get("status", {}).get("phase", "?")
        for c in pod.get("status", {}).get("conditions", []):
            # Unschedulable: no node has room (CPU/memory request too big), or podAffinity can't be met.
            if c["type"] == "PodScheduled" and c.get("status") == "False" and c.get("reason") == "Unschedulable":
                first = bad_since.setdefault((name, "Unschedulable"), now)
                if now - first > sched_grace:
                    return "", Outcome(False, "unschedulable", c.get("message", "pod could not be scheduled"))
                return f"pod {name} unschedulable for {fmt_duration(now - first)}: {c.get('message', '')[:160]}", None
        for cs in pod.get("status", {}).get("containerStatuses", []) or []:
            waiting = cs.get("state", {}).get("waiting")
            # Image or container-config problems: a wrong image name never fixes itself, so without this the
            # repo would sit in ImagePullBackOff until the full build timeout.
            if waiting and waiting.get("reason") in self.FATAL_WAIT_REASONS:
                first = bad_since.setdefault((name, waiting["reason"]), now)
                msg = waiting.get("message", waiting["reason"])
                if now - first > pull_grace:
                    return "", Outcome(False, "image", f"{waiting['reason']}: {msg}")
                return f"pod {name} {waiting['reason']} for {fmt_duration(now - first)}: {msg[:160]}", None
            if "running" in cs.get("state", {}):
                return f"pod {name} running on {pod['spec'].get('nodeName')}", None
            if waiting:
                return f"pod {name} {waiting.get('reason', 'waiting')}", None
        return f"pod {name} {phase}", None


class DockerLauncher:
    """Local test path: same image + build.sh, a host folder stands in for the PVC."""

    def __init__(self, cfg: Settings, workspace: Path, results: Path):
        self.cfg, self.ws, self.results = cfg, workspace, results

    def launch_and_wait(self, spec: RepoSpec, plan: Plan, rlog, tracker: StageTracker) -> Outcome:
        cpu = self.cfg["BUILD_CPU"]
        # Translate Kubernetes quantities to Docker flags: "500m" -> "0.5" CPUs, "4Gi" -> "4g".
        cpus = str(int(cpu[:-1]) / 1000) if cpu.endswith("m") else cpu
        mem = self.cfg["BUILD_MEM"].replace("Gi", "g").replace("Mi", "m")
        rel = plan.workdir.relative_to(self.ws)
        name = f"jbp-build-{spec.name}"
        run(["docker", "rm", "-f", name], log=rlog, check=False)
        cmd = ["docker", "run", "--rm", "--name", name, *os.environ.get("DOCKER_EXTRA_ARGS", "").split(),
               "--cpus", cpus, "--memory", mem,
               "-v", f"{self.ws}:/workspace", "-v", f"{HERE}:/app:ro",
               "-e", f"REPO_NAME={spec.name}", "-e", f"TOOL={plan.tool}", "-e", f"WRAPPER={'yes' if plan.wrapper else 'no'}",
               "-e", f"JAVA={plan.jdk}", "-e", f"WORKDIR=/workspace/{rel}", "-e", "RESULT_DIR=/workspace/results",
               "-e", f"DEP_CACHE={self.cfg['DEP_CACHE']}",
               *[a for key, setting in FREE_TEXT_ENV.items()
                 for a in ("-e", f"{BUILD_ENV_NAME[key]}={self.cfg.get(setting, '')}")],
               plan.image, "bash", "/app/build.sh"]
        rlog.info("container started (image %s, %s CPU / %s)", plan.image, cpus, mem)
        tracker.repo(spec.name, "WAIT")
        rlog.extra["stage"] = "WAIT"
        cp = run(cmd, log=rlog, check=False, timeout=self.cfg.int("BUILD_TIMEOUT_S") + 300)
        (self.results / f"{spec.name}.docker.out").write_text((cp.stdout or "") + (cp.stderr or ""))
        if cp.returncode == 0:
            return Outcome(True)
        if cp.returncode == 124:   # run() reports its own timeout as rc 124
            return Outcome(False, "timeout", "build timed out")
        if "Unable to find image" in (cp.stderr or "") and "pull access denied" in (cp.stderr or ""):
            return Outcome(False, "image", (cp.stderr or "").strip().splitlines()[-1])
        return Outcome(False, "build", f"exit code {cp.returncode}")


# --------------------------------------------------------------------------- the run

def previous_run_id(results: Path) -> str | None:
    """The run that wrote the files in results/, or None if it is empty."""
    st = read_json(results / "_status.json")
    if isinstance(st, dict) and st.get("run_id"):
        return st["run_id"]
    for f in sorted(results.glob("*.json")):
        data = read_json(f)
        if isinstance(data, dict) and data.get("run_id"):
            return data["run_id"]
    return None


def archive_stale_results(ws: Path, run_id: str) -> str | None:
    """Move results/ aside if a DIFFERENT run wrote it. Returns that run's id, or None.

    DECISION: a restart is recognised by its RUN_ID, not by the mere presence of files. A pod restart
    reuses the Job spec, so it has the same RUN_ID and resumes. A new run on a volume that survived
    (jbp all --keep, then jbp run) has a new RUN_ID: its old results are kept in results.<old_id>/ but
    never reused, so stale timings can't leak into an experiment.
    """
    results = ws / "results"
    if not results.is_dir():
        return None
    old = previous_run_id(results)
    if not old or old == run_id:
        return None
    dest = ws / f"results.{old}"
    n = 1
    while dest.exists():
        n += 1
        dest = ws / f"results.{old}.{n}"
    results.rename(dest)
    return old


class Orchestrator:
    def __init__(self, cfg: Settings):
        self.cfg = cfg
        self.ws = Path(cfg.get("WORKSPACE", "/workspace"))
        self.results = self.ws / "results"
        self.repos_dir = self.ws / "repos"
        for d in (self.results, self.repos_dir, self.ws / "cache"):
            d.mkdir(parents=True, exist_ok=True)
        self.run_id = cfg.get("RUN_ID") or time.strftime("%Y%m%d-%H%M%S")
        self.images = load_images(cfg.get("IMAGES_FILE", "/config/images.env"), cfg.get("IMAGE_REGISTRY", ""))
        self.jdks = sorted(int(x) for x in self.images.get("SUPPORTED_JDKS", "17 21").split())
        self.tracker = StageTracker(self.results / "_status.json", self.results / "events.jsonl", self.run_id, "orchestrator")
        launcher = cfg.get("LAUNCHER", "k8s")
        if launcher == "k8s":
            self.launcher = K8sLauncher(cfg, Path(cfg.get("TEMPLATE", "/templates/build-job.tpl.yaml")), self.results, self.run_id)
        elif launcher == "docker":
            self.launcher = DockerLauncher(cfg, self.ws, self.results)
        else:
            raise JbpError(f"LAUNCHER must be k8s or docker, got {launcher!r}")

    # -- helpers
    def pick_jdk(self, need: int) -> int:
        # DECISION: which JDK builds this repo. Take the SMALLEST supported JDK that is >= the version the
        # build declares (needs 18 -> JDK 21, needs 8 -> JDK 11): closest to what the project tests against.
        # If the project needs more than the newest JDK we have, use the newest; plan() adds a warning note.
        return next((j for j in self.jdks if j >= need), self.jdks[-1])

    def pick_image(self, tool: str, wrapper: bool, jdk: int) -> str:
        # DECISION: which container image runs the build (keys in config/images.env).
        #   Maven (with or without mvnw)  -> MAVEN_IMAGE_<jdk>   maven:<ver>-eclipse-temurin-<jdk>
        #                                    (mvnw, if present, still downloads the Maven version it pins)
        #   Gradle WITH gradlew           -> JDK_IMAGE_<jdk>     plain eclipse-temurin; the wrapper fetches
        #                                    the exact Gradle version the project pins
        #   Gradle WITHOUT gradlew        -> GRADLE_IMAGE_<jdk>  image that ships a Gradle binary
        key = f"MAVEN_IMAGE_{jdk}" if tool == "maven" else f"JDK_IMAGE_{jdk}" if wrapper else f"GRADLE_IMAGE_{jdk}"
        img = self.images.get(key)
        if not img:
            raise JbpError(f"no image configured for {key} in images.env")
        return img

    def result_path(self, name: str) -> Path:
        return self.results / f"{name}.json"

    def save_result(self, name: str, facts: dict) -> None:
        build = read_json(self.results / f"{name}.build.json")
        if build is None and (self.results / f"{name}.build.json").exists():
            facts.setdefault("notes", []).append("build.json from the build pod was unreadable")
        # Merge order matters: build.sh's measurements first, then the orchestrator's facts on top, so
        # facts the orchestrator knows better (repo, commit, ok, failure_kind) win on any clash.
        merged = {**(build or {}), **facts}
        merged.setdefault("rc", 0 if merged.get("ok") else 1)
        write_json_atomic(self.result_path(name), merged)

    # -- stages
    def clone(self, spec: RepoSpec, rlog) -> dict:
        dest = self.repos_dir / spec.name
        shutil.rmtree(dest, ignore_errors=True)
        retries = self.cfg.int("CLONE_RETRIES", 3)
        net_errors = ("Could not resolve host", "Connection timed out", "Connection reset", "early EOF",
                      "RPC failed", "Failed to connect", "HTTP 429", "HTTP 5", "unable to access")
        t0 = time.time()
        # DECISION: how to clone.
        #   no ref         -> shallow clone (--depth 1) of the default branch: fastest
        #   ref given      -> shallow clone of that branch/tag; if git rejects it, the ref is probably a commit
        #                     SHA (--branch can't take one), so fall back to a full clone + checkout
        # Only the network errors listed above are retried; "repository not found" fails at once.
        if spec.ref:
            try:
                run(["git", "clone", "-q", "--depth", "1", "--branch", spec.ref, spec.url, str(dest)],
                    log=rlog, retries=retries, retry_on=net_errors, timeout=900)
            except CommandError:
                rlog.info("ref %s is not a branch/tag, doing a full clone + checkout", spec.ref)
                shutil.rmtree(dest, ignore_errors=True)
                run(["git", "clone", "-q", spec.url, str(dest)], log=rlog, retries=retries, retry_on=net_errors, timeout=1800)
                run(["git", "-C", str(dest), "checkout", "-q", spec.ref], log=rlog)
        else:
            run(["git", "clone", "-q", "--depth", "1", spec.url, str(dest)],
                log=rlog, retries=retries, retry_on=net_errors, timeout=900)
        commit = run(["git", "-C", str(dest), "rev-parse", "--short", "HEAD"], log=rlog).stdout.strip()
        size_kb = sum(f.stat().st_size for f in dest.rglob("*") if f.is_file()) // 1024
        return {"clone_s": round(time.time() - t0, 2), "commit": commit, "repo_kb": size_kb}

    def plan(self, spec: RepoSpec) -> Plan:
        workdir = self.repos_dir / spec.name / spec.subdir if spec.subdir else self.repos_dir / spec.name
        if not workdir.is_dir():
            raise JbpError(f"subdir {spec.subdir!r} does not exist in the repo")
        # DETECT: parse build files to find the tool, wrapper and required Java version (see detect.py).
        # A tool=... override in repos.txt is passed in and skips tool detection.
        d = detect(workdir, spec.tool)
        if d.tool == "unknown":
            raise JbpError("no Maven or Gradle build found (" + "; ".join(d.notes) + ")")
        # DECISION: a java=N override in repos.txt always beats what detection found in the build files.
        java, source = (spec.java, "override in repos.txt") if spec.java else (d.java, d.source)
        jdk = self.pick_jdk(java)
        notes = list(d.notes)
        if jdk < java:
            notes.append(f"needs Java {java} but the newest configured JDK is {jdk}")
        return Plan(d.tool, d.wrapper, java, source, jdk, self.pick_image(d.tool, d.wrapper, jdk), workdir, notes)

    def process(self, spec: RepoSpec, plan: Plan | None, facts: dict) -> None:
        """LAUNCH -> WAIT -> COLLECT for one repo. Never raises."""
        rlog = repo_logger(log, spec.name, "LAUNCH")
        t0 = now_ms()
        try:
            self.tracker.repo(spec.name, "LAUNCH", image=plan.image)
            outcome = self.launcher.launch_and_wait(spec, plan, rlog, self.tracker)
        except Exception as e:  # noqa: BLE001  - one repo must never stop the run
            rlog.exception("launch failed")
            outcome = Outcome(False, "infra", str(e))
        t1 = now_ms()
        rlog.extra["stage"] = "COLLECT"
        self.tracker.repo(spec.name, "COLLECT")
        facts.update(launch_ms=t0, done_ms=t1, job_wall_s=round((t1 - t0) / 1000, 1), ok=outcome.ok,
                     attempts=outcome.attempts)
        if not outcome.ok:
            # Report where it failed: "build" = the build itself ran and failed or timed out;
            # "launch" = it never really ran (image pull, scheduling, lost API contact).
            facts.update(stage="build" if outcome.failure_kind in ("build", "timeout") else "launch",
                         failure_kind=outcome.failure_kind, failure_message=outcome.message[:500])
        self.save_result(spec.name, facts)
        res = read_json(self.result_path(spec.name), {})
        summary = (f"build {fmt_duration(res.get('build_s'))}, tests {res.get('tests', '-')}"
                   + (f", {res.get('failures', 0) + res.get('errors', 0)} failed" if res.get('failures') or res.get('errors') else ""))
        if outcome.ok:
            self.tracker.repo(spec.name, "DONE", "ok", result="PASS")
            rlog.extra["stage"] = "DONE"
            rlog.info("PASS (%s; total %s)", summary, fmt_duration((t1 - t0) / 1000))
        else:
            self.tracker.repo(spec.name, "FAILED", "failed", result="FAIL", error=f"{outcome.failure_kind}: {outcome.message[:200]}")
            rlog.extra["stage"] = "FAILED"
            rlog.error("FAIL [%s] %s (%s)", outcome.failure_kind, outcome.message[:300], summary)

    def run(self, specs: list[RepoSpec]) -> int:
        total = len(specs)
        self.tracker.phase("ORCHESTRATE", "started", repos=total, launcher=self.cfg.get("LAUNCHER", "k8s"))
        log.info("run %s: %d repos, launcher=%s, max_parallel=%s, pod=%s CPU/%s, dep_cache=%s",
                 self.run_id, total, self.cfg.get("LAUNCHER", "k8s"), self.cfg["MAX_PARALLEL"],
                 self.cfg["BUILD_CPU"], self.cfg["BUILD_MEM"], self.cfg["DEP_CACHE"])
        parallel = max(1, self.cfg.int("MAX_PARALLEL", 1))
        # MAX_PARALLEL > 1 only overlaps the build Jobs; clone and detect stay sequential in this thread.
        # All build pods must fit on the PVC's node (RWO volume + podAffinity), so keep
        # MAX_PARALLEL x BUILD_CPU below the node's allocatable CPU.
        pool = ThreadPoolExecutor(max_workers=parallel) if parallel > 1 else None
        futures = []
        for i, spec in enumerate(specs, 1):
            rlog = repo_logger(log, spec.name, "CLONE")
            # DECISION: resume after a restart (spot reclaim kills this pod; the Job starts a new one).
            # A repo whose result file says it passed IN THIS RUN is skipped; one that failed is cleaned and
            # retried. A result from another run is never trusted (see archive_stale_results).
            # SKIP_DONE=0 rebuilds everything.
            prev = read_json(self.result_path(spec.name)) if self.cfg.get("SKIP_DONE", "1") == "1" else None
            if prev is not None and (not isinstance(prev, dict) or prev.get("run_id") != self.run_id):
                rlog.warning("[%d/%d] ignoring a result left by run %s", i, total,
                             prev.get("run_id") if isinstance(prev, dict) else "?")
                for f in self.results.glob(f"{spec.name}.*"):
                    f.unlink(missing_ok=True)
                prev = None
            if prev and prev.get("ok"):
                rlog.extra["stage"] = "SKIPPED"
                rlog.info("[%d/%d] already passed earlier in this run, skipping", i, total)
                self.tracker.repo(spec.name, "SKIPPED", "skipped", result="PASS")
                continue
            if prev:
                rlog.info("[%d/%d] failed in an earlier attempt of this run (%s), trying again", i, total,
                          prev.get("failure_kind") or prev.get("stage") or "build")
                for f in self.results.glob(f"{spec.name}.*"):
                    f.unlink(missing_ok=True)
                self.result_path(spec.name).unlink(missing_ok=True)
            base = {"repo": spec.name, "url": spec.url, "ref": spec.ref, "subdir": spec.subdir, "run_id": self.run_id,
                    "launcher": self.cfg.get("LAUNCHER", "k8s"), "pod_cpu": self.cfg["BUILD_CPU"],
                    "pod_mem": self.cfg["BUILD_MEM"], "dep_cache": self.cfg["DEP_CACHE"],
                    "maven_args": self.cfg.get("MAVEN_ARGS_EXTRA", ""), "started": utc_iso()}
            # CLONE
            rlog.info("[%d/%d] cloning %s%s", i, total, spec.url, f" @ {spec.ref}" if spec.ref else "")
            self.tracker.repo(spec.name, "CLONE")
            try:
                base.update(self.clone(spec, rlog))
                rlog.info("cloned %s in %s (%d KB)", base["commit"], fmt_duration(base["clone_s"]), base["repo_kb"])
            except Exception as e:  # noqa: BLE001
                rlog.error("clone failed: %s", e)
                base.update(ok=False, rc=128, stage="clone", failure_kind="clone", failure_message=str(e)[:500])
                self.save_result(spec.name, base)
                self.tracker.repo(spec.name, "FAILED", "failed", result="FAIL", error=f"clone: {str(e)[:200]}")
                continue
            # DETECT
            rlog.extra["stage"] = "DETECT"
            self.tracker.repo(spec.name, "DETECT")
            try:
                plan = self.plan(spec)
            except Exception as e:  # noqa: BLE001
                rlog.error("detection failed: %s", e)
                base.update(ok=False, rc=2, stage="detect", failure_kind="detect", failure_message=str(e)[:500])
                self.save_result(spec.name, base)
                self.tracker.repo(spec.name, "FAILED", "failed", result="FAIL", error=f"detect: {str(e)[:200]}")
                continue
            rlog.info("tool=%s%s, needs Java %s (%s) -> JDK %s, image %s", plan.tool, " (wrapper)" if plan.wrapper else "",
                      plan.java_required, plan.java_source, plan.jdk, plan.image)
            for note in plan.notes:
                rlog.warning("%s", note)
            base.update(tool=plan.tool, wrapper="yes" if plan.wrapper else "no", java_required=plan.java_required,
                        java_source=plan.java_source, jdk=plan.jdk, image=plan.image, notes=plan.notes)
            self.tracker.repo(spec.name, "DETECT", "ok", tool=plan.tool, java=plan.java_required, image=plan.image)
            # LAUNCH / WAIT / COLLECT
            if pool:
                futures.append(pool.submit(self.process, spec, plan, base))
            else:
                self.process(spec, plan, base)
        for f in futures:
            f.result()
        if pool:
            pool.shutdown()

        s = self.tracker.summary()
        self.tracker.phase("ORCHESTRATE", "ok", **{k: s[k] for k in ("passed", "failed")})
        log.info("finished: %d passed, %d failed, %d total", s["passed"], s["failed"], total)
        # Exit 0 even if repos failed: failed builds are results for the report, not a crash. A non-zero
        # exit is reserved for the orchestrator itself breaking, which the driver then reports.
        (self.results / "_orchestrator_done").write_text(utc_iso() + "\n")
        return 0


def main() -> int:
    cfg = Settings.load(os.environ.get("SETTINGS_FILE", HERE.parent / "config" / "settings.env"))
    # Orchestrator-only knobs that are not in settings.env
    for k in ("WORKSPACE", "RUN_ID", "LAUNCHER", "IMAGES_FILE", "TEMPLATE", "REPOS_FILE", "SKIP_DONE", "PVC_NAME"):
        if k in os.environ:
            cfg[k] = os.environ[k]
    ws = Path(cfg.get("WORKSPACE", "/workspace"))
    cfg["RUN_ID"] = cfg.get("RUN_ID") or time.strftime("%Y%m%d-%H%M%S")
    # Before logging starts: the log file lives in results/, which may be moved aside.
    stale = archive_stale_results(ws, cfg["RUN_ID"])
    setup_logging("orchestrator", ws / "results" / "orchestrator.log", verbose=os.environ.get("VERBOSE") == "1")
    if stale:
        log.warning("new run %s: results/ held files from run %s; moved to results.%s/ (not reused)",
                    cfg["RUN_ID"], stale, stale)
    else:
        prev = previous_run_id(ws / "results")
        log.info("%s run %s", "resuming" if prev == cfg["RUN_ID"] else "starting", cfg["RUN_ID"])
    # Only when the run starts, never when it resumes: a restart must not throw away what it built.
    if cfg.get("CLEAR_CACHE") and previous_run_id(ws / "results") != cfg["RUN_ID"]:
        log.info("CLEAR_CACHE=%s: emptied %s", cfg["CLEAR_CACHE"], ", ".join(clear_caches(ws, cfg["CLEAR_CACHE"])))

    # Kubernetes sends SIGTERM when it evicts the pod (spot reclaim, node drain). Exit cleanly with 143;
    # the Job's podFailurePolicy (DisruptionTarget -> Ignore) then starts a new pod, which resumes.
    def on_term(signum, _frame):
        log.warning("received signal %s: stopping; a restart resumes from the first repo without a result", signum)
        sys.exit(143)
    signal.signal(signal.SIGTERM, on_term)

    try:
        repos_file = Path(cfg.get("REPOS_FILE", "/config/repos.txt"))
        specs, problems = parse_repos(repos_file)
        for p in problems:
            log.warning("repos.txt %s", p)
        if not specs:
            log.error("no repos to build in %s", repos_file)
            return 2
        return Orchestrator(cfg).run(specs)
    except JbpError as e:
        log.error("%s", e)
        return 2


if __name__ == "__main__":
    sys.exit(main())
