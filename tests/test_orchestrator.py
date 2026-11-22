"""Unit tests for orchestrator logic that doesn't need a cluster."""
import json
import re
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pipeline"))
from jbpcore import Settings, StageTracker, read_env_file  # noqa: E402
from orchestrator import (K8sLauncher, Plan, RepoSpec, archive_stale_results, parse_repos,  # noqa: E402
                          previous_run_id)


def settings():
    return Settings.load(ROOT / "config" / "settings.env", environ={})


class ParseReposTest(unittest.TestCase):
    def parse(self, text):
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as fh:
            fh.write(text)
        return parse_repos(Path(fh.name))

    def test_full_line(self):
        specs, problems = self.parse("a https://x/a.git v1 sub tool=gradle java=21  # comment\n")
        self.assertEqual(problems, [])
        s = specs[0]
        self.assertEqual((s.name, s.url, s.ref, s.subdir, s.tool, s.java), ("a", "https://x/a.git", "v1", "sub", "gradle", 21))

    def test_dashes_and_comments(self):
        specs, _ = self.parse("# header\n\nb https://x/b.git - -\n")
        self.assertEqual((specs[0].ref, specs[0].subdir), ("", ""))

    def test_problems_reported_not_fatal(self):
        specs, problems = self.parse("onlyname\nc https://x/c.git colour=blue\nc https://x/dup.git\n")
        self.assertEqual([s.name for s in specs], ["c"])
        self.assertEqual(len(problems), 3)

    def test_default_repo_list_is_valid(self):
        specs, problems = parse_repos(ROOT / "config" / "repos.txt")
        self.assertEqual(problems, [])
        self.assertGreaterEqual(len(specs), 6)


class K8sLauncherTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.l = K8sLauncher(settings(), ROOT / "k8s" / "build-job.tpl.yaml", self.tmp, "r1")

    def test_job_name_is_dns_safe(self):
        self.assertEqual(self.l.job_name("My_Repo.v2"), "jbp-build-my-repo-v2")
        self.assertLessEqual(len(self.l.job_name("x" * 100)), 63)

    def test_render_fills_every_placeholder(self):
        plan = Plan("maven", True, 21, "maven.compiler.release", 21, "maven:3.9.11-eclipse-temurin-21", Path("/workspace/repos/a"))
        out = self.l.render(RepoSpec("a", "u"), plan)
        self.assertEqual(re.findall(r"__[A-Z_]+__", out), [])
        self.assertIn("image: maven:3.9.11-eclipse-temurin-21", out)
        self.assertIn('value: "yes"', out)

    def pod(self, waiting=None, sched=None, running=False):
        cs = {"state": {"running": {}} if running else {"waiting": waiting} if waiting else {}}
        conds = [sched] if sched else []
        return [{"metadata": {"name": "p1", "creationTimestamp": "2026-01-01T00:00:00Z"}, "spec": {"nodeName": "n1"},
                 "status": {"phase": "Pending", "conditions": conds, "containerStatuses": [cs]}}]

    def test_image_pull_fails_after_grace(self):
        bad = {}
        note, fatal = self.l._pod_problem(self.pod(waiting={"reason": "ImagePullBackOff", "message": "not found"}), bad, 180, 600)
        self.assertIsNone(fatal)
        self.assertIn("ImagePullBackOff", note)
        bad[("p1", "ImagePullBackOff")] = time.time() - 200
        _, fatal = self.l._pod_problem(self.pod(waiting={"reason": "ImagePullBackOff", "message": "not found"}), bad, 180, 600)
        self.assertEqual(fatal.failure_kind, "image")

    def test_unschedulable_fails_after_grace(self):
        cond = {"type": "PodScheduled", "status": "False", "reason": "Unschedulable", "message": "Insufficient cpu"}
        bad = {("p1", "Unschedulable"): time.time() - 700}
        _, fatal = self.l._pod_problem(self.pod(sched=cond), bad, 180, 600)
        self.assertEqual(fatal.failure_kind, "unschedulable")
        self.assertIn("Insufficient cpu", fatal.message)

    def test_running_is_not_a_problem(self):
        note, fatal = self.l._pod_problem(self.pod(running=True), {}, 180, 600)
        self.assertIsNone(fatal)
        self.assertIn("running on n1", note)


class TrackerTest(unittest.TestCase):
    def test_status_and_events(self):
        d = Path(tempfile.mkdtemp())
        t = StageTracker(d / "s.json", d / "e.jsonl", "r1", "orchestrator")
        t.phase("ORCHESTRATE")
        t.repo("a", "CLONE")
        t.repo("a", "DONE", "ok", result="PASS")
        t.repo("b", "WAIT")
        st = json.loads((d / "s.json").read_text())
        self.assertEqual(st["repos"]["a"]["stage"], "DONE")
        self.assertEqual(st["summary"]["passed"], 1)
        self.assertIn("b", st["summary"]["active"])
        self.assertEqual(len((d / "e.jsonl").read_text().splitlines()), 4)
        # a restart with the same run id keeps earlier repo state (resume)
        t2 = StageTracker(d / "s.json", d / "e.jsonl", "r1", "orchestrator")
        self.assertIn("a", t2.state["repos"])


class ResumeTest(unittest.TestCase):
    """A restart (same RUN_ID) resumes; a new run never reuses another run's results (issue #8)."""

    def workspace(self, run_id=None, repo_run_id=None):
        ws = Path(tempfile.mkdtemp())
        res = ws / "results"
        res.mkdir()
        if run_id:
            (res / "_status.json").write_text(json.dumps({"run_id": run_id, "repos": {}}))
        if repo_run_id:
            (res / "p1.json").write_text(json.dumps({"repo": "p1", "ok": True, "run_id": repo_run_id}))
        return ws

    def test_same_run_is_resumed_in_place(self):
        ws = self.workspace("run-A", "run-A")
        self.assertIsNone(archive_stale_results(ws, "run-A"))
        self.assertTrue((ws / "results" / "p1.json").exists())

    def test_new_run_moves_old_results_aside(self):
        ws = self.workspace("run-A", "run-A")
        self.assertEqual(archive_stale_results(ws, "run-B"), "run-A")
        self.assertFalse((ws / "results").exists())
        self.assertTrue((ws / "results.run-A" / "p1.json").exists())

    def test_archive_name_never_overwrites(self):
        ws = self.workspace("run-A")
        (ws / "results.run-A").mkdir()
        archive_stale_results(ws, "run-B")
        self.assertTrue((ws / "results.run-A.2" / "_status.json").exists())

    def test_empty_or_missing_results_is_a_fresh_start(self):
        self.assertIsNone(archive_stale_results(self.workspace(), "run-B"))
        self.assertIsNone(archive_stale_results(Path(tempfile.mkdtemp()), "run-B"))

    def test_run_id_falls_back_to_repo_result(self):
        ws = self.workspace(repo_run_id="run-A")
        self.assertEqual(previous_run_id(ws / "results"), "run-A")


class SettingsTest(unittest.TestCase):
    def test_env_overrides_file(self):
        s = Settings.load(ROOT / "config" / "settings.env", environ={"BUILD_CPU": "4"})
        self.assertEqual(s["BUILD_CPU"], "4")
        self.assertEqual(s.int("NODE_COUNT"), 1)

    def test_images_cover_every_supported_jdk(self):
        imgs = read_env_file(ROOT / "config" / "images.env")
        for jdk in imgs["SUPPORTED_JDKS"].split():
            for kind in ("MAVEN", "JDK", "GRADLE"):
                self.assertIn(f"{kind}_IMAGE_{jdk}", imgs)


class ManifestTest(unittest.TestCase):
    def test_manifests_render_and_parse(self):
        try:
            import yaml
        except ImportError:
            self.skipTest("PyYAML not installed")
        values = {**settings(), "RUN_ID": "t", "STORAGE_CLASS_LINE": "storageClassName: jbp-gp3", "CLUSTER_NAME": "jbp"}
        for f in ("rbac", "pvc", "orchestrator-job", "collector-pod", "storageclass-eks"):
            text = (ROOT / "k8s" / f"{f}.yaml").read_text()
            for k, v in values.items():
                text = text.replace(f"__{k}__", str(v))
            self.assertEqual(re.findall(r"__[A-Z_]+__", text), [], f)
            for doc in yaml.safe_load_all(text):
                self.assertTrue(doc["apiVersion"] and doc["kind"] and doc["metadata"]["name"], f)


class ClusterConfigTest(unittest.TestCase):
    def test_eksctl_config_is_valid_yaml(self):
        try:
            import yaml
        except ImportError:
            self.skipTest("PyYAML not installed")
        import cluster
        doc = yaml.safe_load(cluster.eksctl_config(settings(), "1.35"))
        ng = doc["managedNodeGroups"][0]
        self.assertTrue(ng["spot"])
        self.assertEqual(doc["vpc"]["nat"]["gateway"], "Disable")
        self.assertEqual(len(ng["instanceTypes"]), 6)
        self.assertEqual(doc["metadata"]["version"], "1.35")


if __name__ == "__main__":
    unittest.main()
