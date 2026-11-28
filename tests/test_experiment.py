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
        self.assertEqual([e.id for e in plan.experiments], ["cpu-scaling", "build-cache", "dep-cache", "remediation"])
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
                       BUILD_CACHE="on", BUILD_CACHE_EDIT="leaf", DEP_BUMP="junit-bom=5.14.1 prop:tomcat.version=11.0.29")
        l = K8sLauncher(cfg, ROOT / "k8s" / "build-job.tpl.yaml", Path(tempfile.mkdtemp()), "r1")
        plan = Plan("maven", False, 21, "x", 21, "maven:3.9.11-eclipse-temurin-21", Path("/workspace/repos/a"))
        doc = yaml.safe_load(l.render(RepoSpec("a", "u"), plan))
        env = {e["name"]: e["value"] for e in doc["spec"]["template"]["spec"]["containers"][0]["env"]}
        self.assertEqual(env["MAVEN_ARGS_EXTRA"], '-DargLine="-Xmx1g -Dx=a\\b"')
        self.assertEqual(env["MAVEN_OPTS"], "-XX:TieredStopAtLevel=1")
        self.assertEqual(env["BUILD_CACHE"], "on")
        self.assertEqual(env["BUILD_CACHE_EDIT"], "leaf")
        self.assertEqual(env["DEP_BUMP"], "junit-bom=5.14.1 prop:tomcat.version=11.0.29")

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


POM_BOM = """<project>
  <properties>
    <maven.compiler.release>17</maven.compiler.release>
  </properties>
  <dependencyManagement><dependencies>
    <dependency>
      <groupId>org.junit</groupId>
      <artifactId>junit-bom</artifactId>
      <version>5.13.4</version>
      <type>pom</type><scope>import</scope>
    </dependency>
  </dependencies></dependencyManagement>
</project>
"""
POM_PARENT = """<project>
  <parent>
    <groupId>org.springframework.boot</groupId>
    <artifactId>spring-boot-starter-parent</artifactId>
    <version>4.2.0</version>
  </parent>
  <properties>
    <java.version>17</java.version>
  </properties>
</project>
"""


class DepBumpTest(unittest.TestCase):
    """build.sh DEP_BUMP rules, with a stub mvn so nothing is downloaded."""

    def build(self, pom, bump):
        import subprocess, json, os, stat
        d = Path(tempfile.mkdtemp())
        (d / "repo").mkdir()
        (d / "repo" / "pom.xml").write_text(pom)
        (d / "bin").mkdir()
        mvn = d / "bin" / "mvn"
        mvn.write_text("#!/bin/sh\necho stub mvn \"$@\"\n")
        mvn.chmod(mvn.stat().st_mode | stat.S_IEXEC)
        env = dict(os.environ, PATH=f"{d / 'bin'}:{os.environ['PATH']}", REPO_NAME="r", TOOL="maven", JAVA="17",
                   WORKDIR=str(d / "repo"), RESULT_DIR=str(d / "res"), DEP_CACHE="none", DEP_BUMP=bump)
        cp = subprocess.run(["bash", str(ROOT / "pipeline" / "build.sh")], env=env, capture_output=True, text=True)
        out = json.loads((d / "res" / "r.build.json").read_text())
        return cp.returncode, out, (d / "repo" / "pom.xml").read_text()

    RULES = "junit-bom=5.14.1 prop:tomcat.version=11.0.29"

    def test_artifact_rule_bumps_the_bom_only(self):
        rc, out, pom = self.build(POM_BOM, self.RULES)
        self.assertEqual(rc, 0)
        self.assertEqual(out["dep_bump"], "junit-bom=5.14.1")
        self.assertIn("<version>5.14.1</version>", pom)
        self.assertNotIn("tomcat.version", pom)   # no <parent>: the property can't be inherited, so not added

    def test_property_rule_overrides_a_parent_managed_version(self):
        rc, out, pom = self.build(POM_PARENT, self.RULES)
        self.assertEqual(rc, 0)
        self.assertEqual(out["dep_bump"], "prop:tomcat.version=11.0.29")
        self.assertIn("<tomcat.version>11.0.29</tomcat.version>", pom)
        self.assertIn("<version>4.2.0</version>", pom)   # the parent itself is untouched

    def test_existing_property_is_replaced(self):
        rc, out, pom = self.build(POM_PARENT, "prop:java.version=21")
        self.assertEqual(rc, 0)
        self.assertIn("<java.version>21</java.version>", pom)
        self.assertNotIn("<java.version>17</java.version>", pom)

    def test_no_match_fails_without_building(self):
        rc, out, pom = self.build(POM_BOM, "jackson-bom=9.9.9")
        self.assertEqual(rc, 3)
        self.assertIn("matched nothing", out["error_excerpt"])
        self.assertEqual(pom, POM_BOM)


class PlanFileTest(unittest.TestCase):
    def test_repo_plan_parses(self):
        known = ["perf-p1-tiny-j17", "perf-p3-medium-j25", "perf-p4-medium-j17", "perf-p5-large-j21",
                 "perf-p6-xlarge-j25", "petclinic", "rest-svc-gradle"]
        plan = experiment.parse_plan((ROOT / "config" / "experiments.txt").read_text(), known)
        rem = next(e for e in plan.experiments if e.id == "remediation")
        self.assertEqual(rem.baseline, "nobump")
        par3 = next(v for v in rem.variants if v.name == "bump-skip-par3")
        self.assertEqual(par3.settings["MAX_PARALLEL"], "3")
        self.assertEqual(par3.repos, ["perf-p4-medium-j17", "perf-p6-xlarge-j25", "petclinic"])
