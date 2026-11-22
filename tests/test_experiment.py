"""Unit tests for the experiment tooling: plan parsing, run IDs, YAML escaping, AZ pin, comparison page."""
import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pipeline"))
import experiment  # noqa: E402
from jbpcore import ConfigError, JbpError, Settings  # noqa: E402
from orchestrator import K8sLauncher, Plan, RepoSpec, clear_caches, yaml_dq  # noqa: E402

KNOWN = ["perf-p1-tiny-j17", "perf-p3-medium-j25", "perf-p6-xlarge-j25", "petclinic"]


def settings(**over):
    s = Settings.load(ROOT / "config" / "settings.env", environ={})
    s.update(over)
    return s


class PlanTest(unittest.TestCase):
    def test_default_plan_parses(self):
        known = [ln.split()[0] for ln in (ROOT / "config" / "repos.txt").read_text().splitlines()
                 if ln.split() and not ln.startswith("#")]
        plan = experiment.parse_plan((ROOT / "config" / "experiments.txt").read_text(), known)
        self.assertEqual([e.id for e in plan.experiments], ["cpu-scaling", "build-cache", "dep-cache"])
        self.assertEqual(plan.settings["INSTANCE_TYPES"], "m6a.2xlarge")
        self.assertEqual(len(plan.experiments[2].variants[0].repos), 8)
        for e in plan.experiments:
            self.assertTrue(e.objective and e.expect, e.id)

    def test_repos_and_baseline(self):
        plan = experiment.parse_plan('experiment x fixed objective="o w"\n'
                                     "variant x a BUILD_CPU=1 REPOS=p1,petclinic\nvariant x b MAVEN_ARGS_EXTRA='-T 1C'\n", KNOWN)
        e = plan.experiments[0]
        self.assertEqual(e.baseline, "a")
        self.assertEqual(e.objective, "o w")
        self.assertEqual(e.variants[0].repos, ["perf-p1-tiny-j17", "petclinic"])
        self.assertEqual(e.variants[1].repos, KNOWN)
        self.assertEqual(e.variants[1].settings, {"MAVEN_ARGS_EXTRA": "-T 1C"})

    def test_errors(self):
        for text in ("variant x a\n",                                             # no experiment line
                     "experiment x sometimes\nvariant x a\n",                     # bad order
                     "experiment x fixed\nvariant x a NODE_COUNT=3\n",            # not a per-variant key
                     "experiment x fixed\nvariant x a REPOS=p9\n",                # unknown repo
                     "experiment x fixed baseline=zz\nvariant x a\n",             # baseline not a variant
                     "experiment x fixed\nvariant x Big\n",                       # not label-safe
                     "experiment x fixed\n"):                                     # no variants
            with self.assertRaises(ConfigError, msg=text):
                experiment.parse_plan(text, KNOWN)

    def test_schedule_fixed_keeps_order_shuffle_keeps_set(self):
        plan = experiment.parse_plan("experiment s shuffle\n" + "".join(f"variant s v{i}\n" for i in range(6)) +
                                     "experiment f fixed\nvariant f a\nvariant f b\nvariant f c\n", KNOWN)
        order = experiment.schedule(plan, 2, seed=1)
        self.assertEqual(len(order), 18)
        for rep in (1, 2):
            names = [v.name for r, v in order if r == rep]
            self.assertEqual(sorted(names[:6]), [f"v{i}" for i in range(6)])
            self.assertEqual(names[6:], ["a", "b", "c"])

    def test_run_id_is_label_safe(self):
        v = experiment.Variant("build-cache", "warm-leaf", {}, [])
        rid = experiment.run_id("20261122-141000", 3, v)
        self.assertEqual(rid, "20261122-141000-r3-build-cache-warm-leaf")
        long = experiment.run_id("x" * 80, 1, v)
        self.assertLessEqual(len(long), 63)
        self.assertRegex(long, r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$")

    def test_filtered_repo_file(self):
        d = Path(tempfile.mkdtemp())
        src = d / "repos.txt"
        src.write_text("# c\na u1 - -\nb u2 - -\nc u3 - -\n")
        out = experiment.filtered_repo_file(src, ["c", "a"], d / "out.txt")
        self.assertEqual(out.read_text(), "a u1 - -\nc u3 - -\n")


class RenderEscapeTest(unittest.TestCase):
    def test_free_text_is_yaml_escaped(self):
        try:
            import yaml
        except ImportError:
            self.skipTest("PyYAML not installed")
        cfg = settings(MAVEN_ARGS_EXTRA='-DargLine="-Xmx1g -Dx=a\\b"', MAVEN_OPTS_EXTRA="-XX:TieredStopAtLevel=1",
                       BUILD_CACHE="on", BUILD_CACHE_EDIT="leaf")
        l = K8sLauncher(cfg, ROOT / "k8s" / "build-job.tpl.yaml", Path(tempfile.mkdtemp()), "r1")
        plan = Plan("maven", False, 21, "x", 21, "maven:3.9.11-eclipse-temurin-21", Path("/workspace/repos/a"))
        doc = yaml.safe_load(l.render(RepoSpec("a", "u"), plan))
        env = {e["name"]: e["value"] for e in doc["spec"]["template"]["spec"]["containers"][0]["env"]}
        self.assertEqual(env["MAVEN_ARGS_EXTRA"], '-DargLine="-Xmx1g -Dx=a\\b"')
        self.assertEqual(env["MAVEN_OPTS"], "-XX:TieredStopAtLevel=1")
        self.assertEqual(env["BUILD_CACHE"], "on")
        self.assertEqual(env["BUILD_CACHE_EDIT"], "leaf")

    def test_yaml_dq(self):
        self.assertEqual(yaml_dq('a"b\\c'), 'a\\"b\\\\c')


class ClearCacheTest(unittest.TestCase):
    def test_clears_only_named_caches(self):
        ws = Path(tempfile.mkdtemp())
        for d in ("m2", "m2-home", "gradle", "build-cache"):
            (ws / "cache" / d).mkdir(parents=True)
        self.assertEqual(clear_caches(ws, "build"), ["build-cache"])
        self.assertTrue((ws / "cache" / "m2").exists())
        self.assertFalse((ws / "cache" / "build-cache").exists())
        clear_caches(ws, "deps")
        self.assertFalse((ws / "cache" / "m2").exists())
        with self.assertRaises(JbpError):
            clear_caches(ws, "everything")


class AzPinTest(unittest.TestCase):
    def test_node_group_pinned_to_one_zone(self):
        try:
            import yaml
        except ImportError:
            self.skipTest("PyYAML not installed")
        import cluster
        zones = ["ap-south-1a", "ap-south-1b", "ap-south-1c"]
        doc = yaml.safe_load(cluster.eksctl_config(settings(NODE_AZ=""), "1.35", ["m6a.2xlarge"], zones))
        self.assertEqual(doc["availabilityZones"], zones)
        self.assertEqual(doc["managedNodeGroups"][0]["availabilityZones"], ["ap-south-1a"])
        doc = yaml.safe_load(cluster.eksctl_config(settings(NODE_AZ="ap-south-1b"), "1.35", ["m6a.2xlarge"], zones))
        self.assertEqual(doc["managedNodeGroups"][0]["availabilityZones"], ["ap-south-1b"])
        with self.assertRaises(JbpError):
            cluster.eksctl_config(settings(NODE_AZ="ap-south-1z"), "1.35", ["m6a.2xlarge"], zones)


class CompareReportTest(unittest.TestCase):
    def test_comparison_page(self):
        d = Path(tempfile.mkdtemp())
        meta = {"session": "s", "reps": 2, "local": True, "settings": {"BUILD_CPU": "2"}, "runs": [],
                "experiments": [{"id": "cache", "order": "fixed", "objective": "obj <x>", "expect": "exp", "baseline": "off",
                                 "variants": [{"name": "off", "settings": {}, "repos": ["a"]},
                                              {"name": "warm", "settings": {"BUILD_CACHE": "on"}, "repos": ["a"]}]}]}
        for rep, (off, warm) in enumerate([(100, 5), (110, 6)], 1):
            for vn, secs, tests in (("off", off, 50), ("warm", warm, 0)):
                rid = f"s-r{rep}-cache-{vn}"
                (d / rid / "results").mkdir(parents=True)
                (d / rid / "results" / "a.json").write_text(json.dumps({"ok": True, "build_s": secs, "tests": tests}))
                meta["runs"].append({"rep": rep, "experiment": "cache", "variant": vn, "run_id": rid, "ok": True})
        (d / "experiment.json").write_text(json.dumps(meta))
        subprocess.run([sys.executable, str(ROOT / "report" / "compare.py"), str(d)], check=True, capture_output=True)
        page = (d / "comparison.html").read_text()
        self.assertIn("obj &lt;x&gt;", page)
        self.assertIn("105 s", page)          # median of 100 and 110
        self.assertIn("-95%", page)           # 5.5 vs 105
        self.assertIn("Ran fewer tests", page)
        rows = (d / "comparison.csv").read_text().splitlines()
        self.assertEqual(len(rows), 3)


if __name__ == "__main__":
    unittest.main()
