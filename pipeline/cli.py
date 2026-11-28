"""jbp: build Java repos on Kubernetes, measure them, and run build-time experiments.

  jbp all [--keep] [--repos FILE]   cluster up -> run -> report -> cluster down (down always runs)
  jbp up                            create the EKS cluster (~15-20 min)
  jbp run [--repos FILE]            run the pipeline on the current kube context
  jbp down                          delete namespace + cluster, then verify nothing is left
  jbp status [RUN_ID]               where is a run right now (phase + stage of every repo)
  jbp leftovers [--delete]          anything still billing? (--delete removes orphaned volumes)
  jbp local-docker [--repos FILE]   no cluster: same orchestrator + build.sh with Docker
  jbp local-kind up|run|down        free Kubernetes rehearsal with kind
  jbp report RUN_DIR                (re)build report.html for a run
  jbp experiment [--reps N] [--only IDS] [--local] [--keep]
                                    cluster up once -> warm-up -> N rounds of every variant in
                                    config/experiments.txt -> comparison.html -> cluster down

Settings come from config/settings.env; any environment variable with the same name overrides it.
Every command logs to the console and to runs/<RUN_ID>/driver.log.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import signal
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cluster  # noqa: E402
import experiment  # noqa: E402
from jbpcore import (ROOT, CommandError, JbpError, Settings, StageTracker, fmt_duration, read_json,  # noqa: E402
                     require_tools, run, run_streaming, setup_logging)

log = logging.getLogger("driver")
PIPE = ROOT / "pipeline"
RUNS = ROOT / "runs"


class Run:
    """One run's folder: runs/<id>/ with driver.log, driver_status.json, events, phases.env, results/."""

    def __init__(self, run_id: str | None = None, parent: Path | None = None):
        self.id = run_id or os.environ.get("RUN_ID") or time.strftime("%Y%m%d-%H%M%S")
        self.dir = (parent or RUNS) / self.id
        self.dir.mkdir(parents=True, exist_ok=True)
        self.tracker: StageTracker | None = None

    def start_tracking(self):
        self.tracker = StageTracker(self.dir / "driver_status.json", self.dir / "driver_events.jsonl", self.id, "driver")

    def phase(self, name: str, status: str = "started", **kw):
        """Record a phase change (for `jbp status`) and its timestamp (for the cost calculation)."""
        if self.tracker:
            self.tracker.phase(name, status, **kw)
        key = {"started": "start", "ok": "end", "failed": "end"}.get(status)
        if key:
            with open(self.dir / "phases.env", "a") as fh:
                fh.write(f"{name.lower()}_{key}={int(time.time())}\n")
        log.info("=== %s %s %s", name, status.upper(), " ".join(f"{k}={v}" for k, v in kw.items()))


def effective_settings_text(cfg: Settings) -> str:
    return "".join(f"{k}={v}\n" for k, v in cfg.items())


# --------------------------------------------------------------------------- k8s run

def k(cfg, *args, **kw):
    return run(["kubectl", "-n", cfg["NAMESPACE"], *args], log=log, **kw)


def render(path: Path, values: dict) -> str:
    out = path.read_text()
    for key, val in values.items():
        out = out.replace(f"__{key}__", str(val))
    return out


def apply(text: str) -> None:
    run(["kubectl", "apply", "-f", "-"], input=text, log=log, retries=3, timeout=120)


def configmap(cfg, name: str, files: dict[str, str | Path]) -> None:
    # "create --dry-run=client -o yaml" only renders the ConfigMap; piping it to "apply" makes this
    # create-or-update, so re-running a deploy never fails with "already exists".
    args = ["kubectl", "-n", cfg["NAMESPACE"], "create", "configmap", name, "--dry-run=client", "-o", "yaml"]
    args += [f"--from-file={key}={path}" for key, path in files.items()]
    apply(run(args, log=log).stdout)


def run_on_cluster(cfg: Settings, r: Run, repos: Path) -> bool:
    require_tools("kubectl")
    ns = cfg["NAMESPACE"]
    ctx = run(["kubectl", "config", "current-context"], log=log, timeout=20).stdout.strip()
    log.info("run %s on kube context %s, namespace %s, repos %s", r.id, ctx, ns, repos)
    # Values for the __NAME__ placeholders in k8s/*.yaml. STORAGE_CLASS_LINE is a whole YAML line, so when
    # no class is set the field disappears and the cluster's default StorageClass is used.
    values = {**cfg, "RUN_ID": r.id,
              "STORAGE_CLASS_LINE": f"storageClassName: {cfg['STORAGE_CLASS']}" if cfg["STORAGE_CLASS"] else ""}

    r.phase("DEPLOY")
    (r.dir / "settings.effective.env").write_text(effective_settings_text(cfg))
    apply(run(["kubectl", "create", "namespace", ns, "--dry-run=client", "-o", "yaml"], log=log).stdout)
    # Ship our code as a ConfigMap mounted at /app: stock images (alpine/k8s, maven, temurin) can then run
    # it, so the project never has to build or push an image of its own.
    code = {p.name: p for p in PIPE.glob("*.py")}
    code["build.sh"] = PIPE / "build.sh"
    configmap(cfg, "jbp-code", code)
    configmap(cfg, "jbp-config", {"repos.txt": repos, "images.env": ROOT / "config" / "images.env",
                                  "settings.env": r.dir / "settings.effective.env"})
    configmap(cfg, "jbp-templates", {"build-job.tpl.yaml": ROOT / "k8s" / "build-job.tpl.yaml"})
    apply(render(ROOT / "k8s" / "rbac.yaml", values))
    apply(render(ROOT / "k8s" / "pvc.yaml", values))
    # Remove the previous run's Jobs/pod first: a Job's template can't be changed by "apply".
    for obj in (["job", "jbp-orchestrator"], ["pod", "jbp-collector"]):
        k(cfg, "delete", *obj, "--ignore-not-found", "--wait=true", timeout=180)
    k(cfg, "delete", "jobs", "-l", "app=jbp-build", "--ignore-not-found", timeout=180)
    apply(render(ROOT / "k8s" / "orchestrator-job.yaml", values))
    r.phase("DEPLOY", "ok")

    r.phase("ORCHESTRATE")
    ok = follow_orchestrator(cfg, r)
    r.phase("ORCHESTRATE", "ok" if ok else "failed")

    r.phase("COLLECT")
    collect(cfg, r, values)
    r.phase("COLLECT", "ok")
    return ok


def follow_orchestrator(cfg: Settings, r: Run) -> bool:
    """Stream the orchestrator's log until its Job completes or fails (reconnecting if the stream drops)."""
    ns, done = cfg["NAMESPACE"], threading.Event()
    deadline = time.time() + 6 * 3600

    def pod_phase():
        cp = k(cfg, "get", "pods", "-l", "job-name=jbp-orchestrator", "-o",
               "jsonpath={.items[-1:].status.phase}{' '}{.items[-1:].status.containerStatuses[0].state.waiting.reason}",
               check=False, timeout=30)
        return cp.stdout.strip()

    def stream():
        while not done.is_set():
            st = pod_phase()
            if st.startswith(("Running", "Succeeded", "Failed")):
                try:
                    run_streaming(["kubectl", "-n", ns, "logs", "-f", "job/jbp-orchestrator"], log=log, raw=True,
                                  prefix="  | ")
                except (CommandError, JbpError) as e:
                    log.debug("log stream ended: %s", e)
                if st.startswith(("Succeeded", "Failed")):
                    return
            elif st:
                log.info("orchestrator pod: %s", st)
            done.wait(10)

    # The log stream (thread) is only for watching. The Job's Complete/Failed condition, polled below, is
    # what decides the outcome, so a dropped log stream can never end the run early.
    t = threading.Thread(target=stream, daemon=True)
    t.start()
    result = None
    while result is None:
        time.sleep(10)
        cp = k(cfg, "get", "job", "jbp-orchestrator", "-o", "json", check=False, timeout=30)
        if cp.returncode != 0:
            log.warning("could not read orchestrator job: %s", cp.stderr.strip()[:200])
            continue
        conds = {c["type"] for c in json.loads(cp.stdout).get("status", {}).get("conditions", []) if c["status"] == "True"}
        if "Complete" in conds:
            result = True
        elif "Failed" in conds:
            result = False
        elif time.time() > deadline:
            log.error("orchestrator still running after 6 h; giving up waiting")
            result = False
    t.join(timeout=30)
    done.set()
    return result


def collect(cfg: Settings, r: Run, values: dict) -> None:
    # The orchestrator pod has exited by now and "kubectl cp" needs a running container, so start a
    # short-lived pod that mounts the same PVC and copy the results out of it.
    apply(render(ROOT / "k8s" / "collector-pod.yaml", values))
    k(cfg, "wait", "--for=condition=Ready", "pod/jbp-collector", "--timeout=300s", timeout=330, retries=1)
    k(cfg, "cp", "jbp-collector:/workspace/results", str(r.dir / "results"), timeout=600, retries=2)
    k(cfg, "delete", "pod", "jbp-collector", "--wait=false", check=False, timeout=60)
    nodes = run(["kubectl", "get", "nodes", "-o", "json"], log=log, check=False, timeout=60).stdout
    (r.dir / "nodes.json").write_text(nodes or "{}")
    n = len(list((r.dir / "results").glob("*.build.log")))
    log.info("copied results for %d builds to %s", n, r.dir / "results")


# --------------------------------------------------------------------------- report

def make_report(r: Run, cfg: Settings) -> Path | None:
    meta = {"context": cfg.get("_context", ""), "RUN_ID": r.id}
    for key in ("BUILD_CPU", "BUILD_MEM", "MAX_PARALLEL", "DEP_CACHE", "MAVEN_ARGS_EXTRA", "GRADLE_ARGS_EXTRA", "DEP_BUMP",
                "INSTANCE_TYPES", "NODE_COUNT", "AWS_REGION"):
        meta[key] = cfg.get(key, "")
    (r.dir / "run.env").write_text("".join(f"{k}={v}\n" for k, v in meta.items()))
    # Run the report as a separate process: a bug in it must not crash the driver (cmd_all calls this
    # from its teardown path).
    cp = run([sys.executable, str(ROOT / "report" / "make_report.py"), str(r.dir)], log=log, check=False, timeout=120)
    if cp.returncode != 0:
        log.error("report failed: %s", (cp.stderr or cp.stdout).strip()[-500:])
        return None
    path = r.dir / "report.html"
    log.info("report: %s", path)
    return path


# --------------------------------------------------------------------------- commands

def cmd_up(cfg, r, args):
    require_tools("aws", "eksctl", "kubectl")
    r.phase("UP")
    cluster.up(cfg)
    r.phase("UP", "ok")


def cmd_down(cfg, r, args):
    require_tools("aws", "eksctl", "kubectl")
    r.phase("DOWN")
    clean = cluster.down(cfg)
    r.phase("DOWN", "ok" if clean else "failed")
    if not clean:
        raise JbpError("teardown left billable resources (listed above)")


def cmd_run(cfg, r, args):
    cfg["_context"] = run(["kubectl", "config", "current-context"], log=log, check=False).stdout.strip()
    # DECISION: which StorageClass the workspace PVC uses. An explicit STORAGE_CLASS wins. Otherwise, if
    # the jbp-gp3 class from `jbp up` exists (an EKS cluster), use it. Otherwise leave it empty so the
    # cluster default is used (kind: "standard").
    if not cfg["STORAGE_CLASS"] and run(["kubectl", "get", "storageclass", "jbp-gp3"], log=log, check=False,
                                        timeout=30).returncode == 0:
        # EKS has no default StorageClass, so a PVC without one stays Pending forever
        log.info("using StorageClass jbp-gp3 (created by `jbp up`)")
        cfg["STORAGE_CLASS"] = "jbp-gp3"
    ok = run_on_cluster(cfg, r, Path(args.repos))
    r.phase("REPORT")
    r.phase("REPORT", "ok" if make_report(r, cfg) else "failed")
    if not ok:
        raise JbpError("the orchestrator job failed; see its log above and results/orchestrator.log")


def cmd_all(cfg, r, args):
    require_tools("aws", "eksctl", "kubectl")
    cfg["STORAGE_CLASS"] = cfg["STORAGE_CLASS"] or "jbp-gp3"   # `jbp all` always creates the EKS cluster
    (r.dir / "cluster.env").write_text(f"INSTANCE_TYPES={cfg['INSTANCE_TYPES']}\nNODE_COUNT={cfg['NODE_COUNT']}\n"
                                       f"AWS_REGION={cfg['AWS_REGION']}\n")
    interrupted = {"sig": None}

    # Turn SIGTERM (e.g. the session being stopped) into KeyboardInterrupt, so it takes the same path as
    # Ctrl-C and the finally block below still tears the cluster down.
    def on_signal(signum, _f):
        interrupted["sig"] = signum
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, on_signal)
    try:
        cmd_up(cfg, r, args)
        cmd_run(cfg, r, args)
    except KeyboardInterrupt:
        log.warning("interrupted: tearing down now")
        raise
    finally:
        # Teardown ALWAYS runs (success, error, Ctrl-C, SIGTERM) unless --keep: a forgotten EKS cluster
        # costs ~$75/month idle. Teardown errors are logged, not raised, so the report still gets written.
        if args.keep:
            log.warning("--keep: cluster %s left running (~$0.10/h + node). Run `jbp down` when finished.", cfg["CLUSTER_NAME"])
        else:
            try:
                cmd_down(cfg, r, args)
            except Exception as e:  # noqa: BLE001
                log.error("TEARDOWN PROBLEM: %s -> run `jbp leftovers`", e)
            if (r.dir / "results").exists():
                make_report(r, cfg)  # again, now including teardown time in the cost


def cmd_status(cfg, r, args):
    rid = args.run_id or (sorted(p.name for p in RUNS.iterdir() if p.is_dir())[-1] if RUNS.exists() else None)
    if not rid:
        raise JbpError("no runs yet")
    d = RUNS / rid
    drv = read_json(d / "driver_status.json", {}) or {}
    ph = drv.get("phase") or {}
    print(f"run {rid}: phase {ph.get('name', '?')} {ph.get('status', '')} since {ph.get('since', '?')}")
    status = None
    pod = run(["kubectl", "-n", cfg["NAMESPACE"], "get", "pods", "-l", "job-name=jbp-orchestrator", "-o",
               "jsonpath={.items[-1:].metadata.name}"], log=log, check=False, timeout=20).stdout.strip()
    # Where to read the orchestrator's progress from, best first: live from the running pod, then from
    # results already collected, then from a local-docker workspace.
    if pod:
        cp = run(["kubectl", "-n", cfg["NAMESPACE"], "exec", pod, "--", "cat", "/workspace/results/_status.json"],
                 log=log, check=False, timeout=30)
        if cp.returncode == 0:
            status, source = json.loads(cp.stdout), f"live from pod {pod}"
    if status is None:
        for f, label in ((d / "results" / "_status.json", "from collected results"),
                         (d / "workspace" / "results" / "_status.json", "local workspace")):
            status, source = read_json(f), label
            if status:
                break
    if not status:
        print("  no orchestrator status yet")
        return
    s = status.get("summary", {})
    print(f"orchestrator ({source}, updated {status.get('updated')}): "
          f"{s.get('passed', 0)} passed, {s.get('failed', 0)} failed, {s.get('finished', 0)}/{s.get('total_seen', 0)} finished")
    for name, rep in status.get("repos", {}).items():
        extra = rep.get("error") or rep.get("image") or ""
        print(f"  {name:24} {rep.get('stage', ''):8} {rep.get('result', ''):5} since {rep.get('since', '')}  {extra}")


def cmd_leftovers(cfg, r, args):
    require_tools("aws")
    cluster.check_credentials(cfg)
    left = cluster.leftovers(cfg)
    if not left:
        log.info("nothing left for cluster %s in %s", cfg["CLUSTER_NAME"], cfg["AWS_REGION"])
        return
    for kk, v in left.items():
        log.warning("STILL BILLING - %s: %s", kk, "; ".join(v))
    if args.delete:
        cluster.delete_orphan_volumes(cfg)
    raise JbpError("leftovers found")


def run_local_docker(cfg: Settings, r: Run, repos: Path, ws: Path) -> bool:
    """Run the orchestrator on this machine with Docker; `ws` stands in for the PVC. Results are copied
    into the run folder, because the next run on the same workspace moves results/ aside."""
    ws.mkdir(parents=True, exist_ok=True)
    cfg["IMAGE_REGISTRY"] = os.environ.get("LOCAL_IMAGE_REGISTRY", "")   # Docker Hub locally
    (r.dir / "settings.effective.env").write_text(effective_settings_text(cfg))
    cfg["_context"] = "local-docker"
    r.phase("ORCHESTRATE")
    env = {"LAUNCHER": "docker", "WORKSPACE": str(ws), "RUN_ID": r.id, "REPOS_FILE": str(repos.resolve()),
           "IMAGES_FILE": str(ROOT / "config" / "images.env"), "SETTINGS_FILE": str(r.dir / "settings.effective.env")}
    ok = True
    try:
        run_streaming([sys.executable, str(PIPE / "orchestrator.py")], log=log, env=env, raw=True, prefix="  | ")
    except CommandError:
        ok = False
    r.phase("ORCHESTRATE", "ok" if ok else "failed")
    if (ws / "results").exists():
        shutil.copytree(ws / "results", r.dir / "results", dirs_exist_ok=True)
    return ok


def cmd_local_docker(cfg, r, args):
    require_tools("docker", "git")
    ok = run_local_docker(cfg, r, Path(args.repos), Path(os.environ.get("WORKSPACE", r.dir / "workspace")))
    r.phase("REPORT")
    r.phase("REPORT", "ok" if make_report(r, cfg) else "failed")
    if not ok:
        raise JbpError("orchestrator exited with an error (see above)")


def cmd_local_kind(cfg, r, args):
    require_tools("kind", "kubectl")
    if args.action == "up":
        run_streaming(["kind", "create", "cluster", "--name", "jbp-local", "--wait", "120s"], log=log, prefix="kind: ")
    elif args.action == "down":
        run_streaming(["kind", "delete", "cluster", "--name", "jbp-local"], log=log, prefix="kind: ")
    else:
        run(["kubectl", "config", "use-context", "kind-jbp-local"], log=log)
        cfg["IMAGE_REGISTRY"] = os.environ.get("IMAGE_REGISTRY", "")
        cfg["STORAGE_CLASS"] = os.environ.get("STORAGE_CLASS", "standard")
        cfg["BUILD_CPU"] = os.environ.get("BUILD_CPU", "1")
        cfg["BUILD_MEM"] = os.environ.get("BUILD_MEM", "3Gi")
        cmd_run(cfg, r, args)


def cmd_experiment(cfg, r, args):
    """Run an experiment plan: cluster up once, warm-up pass, REPS rounds of every variant, comparison.html.

    Each variant is an ordinary run in runs/<session>/<RUN_ID>/ on the same cluster and volume, so caches
    carry over between variants exactly as the plan's order says. Down always runs (unless --keep/--local).
    """
    from jbpcore import read_env_file
    plan_file = Path(args.plan)
    repos_txt = Path(args.repos)
    known = [ln.split()[0] for ln in repos_txt.read_text().splitlines() if ln.split() and not ln.lstrip().startswith("#")]
    plan = experiment.select(experiment.parse_plan(plan_file.read_text(), known), args.only)
    cfg.update(plan.settings)
    order = experiment.schedule(plan, args.reps, seed=args.seed)
    session = r.id
    log.info("experiment session %s: %d experiments, %d reps, %d runs + warm-up (%s)", session, len(plan.experiments),
             args.reps, len(order), "local Docker" if args.local else f"EKS {cfg['AWS_REGION']} {cfg['INSTANCE_TYPES']}")
    meta = {"session": session, "plan_file": str(plan_file), "reps": args.reps, "local": args.local,
            "settings": dict(cfg), "started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "experiments": [{"id": e.id, "order": e.order, "objective": e.objective, "expect": e.expect,
                             "baseline": e.baseline, "variants": [{"name": v.name, "settings": v.settings, "repos": v.repos}
                                                                  for v in e.variants]} for e in plan.experiments],
            "runs": []}
    meta_path = r.dir / "experiment.json"
    # Same --run-id again (e.g. the plan run in chunks): keep the runs already recorded, so one
    # comparison page covers every chunk.
    if meta_path.exists():
        old = json.loads(meta_path.read_text())
        meta["runs"] = old.get("runs", [])
        meta["started"] = old.get("started", meta["started"])
        ids = {e["id"] for e in meta["experiments"]}
        meta["experiments"] = [e for e in old.get("experiments", []) if e["id"] not in ids] + meta["experiments"]

    def save():
        meta_path.write_text(json.dumps(meta, indent=1))
    save()
    ws = r.dir / "workspace"   # --local only: the stand-in for the PVC, shared by every variant

    def one_run(rid, settings, repos, label):
        child = Run(rid, parent=r.dir)
        vcfg = Settings(cfg)
        vcfg.update(settings)
        rf = experiment.filtered_repo_file(repos_txt, repos, child.dir / "repos.txt")
        log.info("----- %s: %s on %s", label, " ".join(f"{k}={v}" for k, v in settings.items()) or "baseline settings",
                 ", ".join(repos))
        t0 = time.time()
        try:
            if args.local:
                ok = run_local_docker(vcfg, child, rf, ws)
            else:
                vcfg["_context"] = cfg.get("_context", "")
                ok = run_on_cluster(vcfg, child, rf)
            make_report(child, vcfg)
        except JbpError as e:
            log.error("%s failed: %s (carrying on with the next run)", label, e)
            ok = False
        return ok, round(time.time() - t0)

    interrupted = {"sig": None}

    def on_signal(signum, _f):
        interrupted["sig"] = signum
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, on_signal)
    try:
        if args.local:
            require_tools("docker", "git")
        else:
            require_tools("aws", "eksctl", "kubectl")
            cfg["STORAGE_CLASS"] = cfg["STORAGE_CLASS"] or "jbp-gp3"
            (r.dir / "cluster.env").write_text(f"INSTANCE_TYPES={cfg['INSTANCE_TYPES']}\nNODE_COUNT={cfg['NODE_COUNT']}\n"
                                               f"AWS_REGION={cfg['AWS_REGION']}\n")
            cmd_up(cfg, r, args)
            cfg["_context"] = run(["kubectl", "config", "current-context"], log=log, check=False).stdout.strip()
        # Warm-up: pulls every image and fills the dependency cache, so the first measured variant isn't
        # charged for it. Not part of the results.
        if not args.no_warmup:
            union = [n for n in known if any(n in v.repos for e in plan.experiments for v in e.variants)]
            r.phase("WARMUP")
            ok, secs = one_run(f"{session}-warmup"[:63], {}, union, "warm-up (not measured)")
            r.phase("WARMUP", "ok" if ok else "failed")
        for i, (rep, v) in enumerate(order, 1):
            rid = experiment.run_id(session, rep, v)
            ok, secs = one_run(rid, v.settings, v.repos, f"[{i}/{len(order)}] rep {rep} {v.experiment}/{v.name}")
            meta["runs"].append({"rep": rep, "experiment": v.experiment, "variant": v.name, "run_id": rid,
                                 "ok": ok, "wall_s": secs})
            save()
    except KeyboardInterrupt:
        log.warning("interrupted: %s", "tearing down now" if not (args.local or args.keep) else "stopping")
        raise
    finally:
        if not args.local:
            if args.keep:
                log.warning("--keep: cluster %s left running. Run `jbp down` when finished.", cfg["CLUSTER_NAME"])
            else:
                try:
                    cmd_down(cfg, r, args)
                except Exception as e:  # noqa: BLE001
                    log.error("TEARDOWN PROBLEM: %s -> run `jbp leftovers`", e)
        meta["finished"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        meta["phases"] = read_env_file(r.dir / "phases.env") if (r.dir / "phases.env").exists() else {}
        save()
        cp = run([sys.executable, str(ROOT / "report" / "compare.py"), str(r.dir)], log=log, check=False, timeout=120)
        if cp.returncode == 0:
            log.info("comparison: %s", r.dir / "comparison.html")
        else:
            log.error("comparison report failed: %s", (cp.stderr or cp.stdout).strip()[-500:])


def cmd_report(cfg, r, args):
    rr = Run.__new__(Run)
    rr.id, rr.dir, rr.tracker = Path(args.run_dir).name, Path(args.run_dir), None
    make_report(rr, cfg)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="jbp", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("-v", "--verbose", action="store_true", help="show every command on the console")
    p.add_argument("--run-id", help="name of the run folder under runs/ (default: timestamp)")
    sub = p.add_subparsers(dest="cmd", required=True)
    default_repos = str(ROOT / "config" / "repos.txt")
    for name in ("all", "run", "local-docker"):
        sp = sub.add_parser(name)
        sp.add_argument("--repos", default=default_repos)
        if name == "all":
            sp.add_argument("--keep", action="store_true", help="leave the cluster running afterwards")
    sub.add_parser("up")
    sub.add_parser("down")
    sp = sub.add_parser("status")
    sp.add_argument("run_id", nargs="?")
    sp = sub.add_parser("leftovers")
    sp.add_argument("--delete", action="store_true")
    sp = sub.add_parser("local-kind")
    sp.add_argument("action", choices=["up", "run", "down"])
    sp.add_argument("--repos", default=default_repos)
    sp = sub.add_parser("report")
    sp.add_argument("run_dir")
    sp = sub.add_parser("experiment", help="run the experiment plan (config/experiments.txt)")
    sp.add_argument("--plan", default=str(ROOT / "config" / "experiments.txt"))
    sp.add_argument("--repos", default=default_repos)
    sp.add_argument("--reps", type=int, default=3, help="rounds of every variant (default 3)")
    sp.add_argument("--only", default="", help="comma list of experiment IDs to run (default all)")
    sp.add_argument("--seed", type=int, default=None, help="shuffle seed, to repeat a run order")
    sp.add_argument("--local", action="store_true", help="local Docker instead of EKS (free rehearsal)")
    sp.add_argument("--keep", action="store_true", help="leave the cluster running afterwards")
    sp.add_argument("--no-warmup", action="store_true", help="skip the unmeasured warm-up pass")
    args = p.parse_args(argv)

    cfg = Settings.load()
    # Read-only commands don't create a run folder; everything else gets runs/<RUN_ID>/ with its own log.
    if args.cmd in ("status", "report"):
        setup_logging("driver", verbose=args.verbose)
        r = None
    else:
        r = Run(args.run_id)
        setup_logging("driver", r.dir / "driver.log", verbose=args.verbose)
        r.start_tracking()
        log.info("jbp %s (run %s, log %s)", args.cmd, r.id, r.dir / "driver.log")
    handler = {"all": cmd_all, "up": cmd_up, "down": cmd_down, "run": cmd_run, "status": cmd_status,
               "leftovers": cmd_leftovers, "local-docker": cmd_local_docker, "local-kind": cmd_local_kind,
               "report": cmd_report, "experiment": cmd_experiment}[args.cmd]
    t0 = time.time()
    # Exit codes: 0 ok, 1 expected failure (JbpError: message only, no traceback), 130 interrupted.
    # Any other exception is a bug and is allowed to show its traceback.
    try:
        handler(cfg, r, args)
    except KeyboardInterrupt:
        log.error("interrupted after %s", fmt_duration(time.time() - t0))
        return 130
    except JbpError as e:
        log.error("%s", e)
        log.error("failed after %s", fmt_duration(time.time() - t0))
        return 1
    if r:
        log.info("jbp %s finished in %s", args.cmd, fmt_duration(time.time() - t0))
    return 0


if __name__ == "__main__":
    sys.exit(main())
