"""Unit tests for pipeline/detect.py.   python3 -m unittest discover -s tests"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "pipeline"))
from detect import detect, normalise  # noqa: E402

POM = '<project xmlns="http://maven.apache.org/POM/4.0.0">{}</project>'


class DetectTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def repo(self, name, files):
        d = self.root / name
        for rel, text in files.items():
            (d / rel).parent.mkdir(parents=True, exist_ok=True)
            (d / rel).write_text(text)
        return d

    def check(self, files, tool, java, source=None, force=None):
        d = detect(self.repo(self.id().split(".")[-1], files), force)
        self.assertEqual((d.tool, d.java), (tool, java), d)
        if source:
            self.assertEqual(d.source, source)
        return d

    # --- Maven
    def test_release(self):
        self.check({"pom.xml": POM.format("<properties><maven.compiler.release>21</maven.compiler.release></properties>")},
                   "maven", 21, "maven.compiler.release")

    def test_source_1_8(self):
        self.check({"pom.xml": POM.format("<properties><maven.compiler.source>1.8</maven.compiler.source></properties>")}, "maven", 8)

    def test_spring_java_version(self):
        self.check({"pom.xml": POM.format("<properties><java.version>17</java.version></properties>")}, "maven", 17)

    def test_compiler_plugin_release(self):
        self.check({"pom.xml": POM.format("<build><plugins><plugin><artifactId>maven-compiler-plugin</artifactId>"
                                          "<configuration><release>11</release></configuration></plugin></plugins></build>")},
                   "maven", 11, "compiler plugin <release>")

    def test_property_reference_resolved(self):
        self.check({"pom.xml": POM.format("<properties><jdk>21</jdk><maven.compiler.release>${jdk}</maven.compiler.release></properties>")},
                   "maven", 21)

    def test_property_from_local_parent(self):
        self.check({"pom.xml": POM.format("<properties><java.version>17</java.version></properties><modules><module>app</module></modules>"),
                    "app/pom.xml": POM.format("<parent><artifactId>root</artifactId></parent>"
                                              "<properties><maven.compiler.release>${java.version}</maven.compiler.release></properties>")},
                   "maven", 17, "maven.compiler.release")

    def test_highest_module_wins(self):
        self.check({"pom.xml": POM.format("<properties><maven.compiler.release>17</maven.compiler.release></properties>"),
                    "sub/pom.xml": POM.format("<properties><maven.compiler.release>21</maven.compiler.release></properties>")},
                   "maven", 21)

    def test_nothing_declared_defaults(self):
        self.check({"pom.xml": POM.format("")}, "maven", 17, "default (nothing declared)")

    def test_unresolvable_property_noted(self):
        d = self.check({"pom.xml": POM.format("<properties><maven.compiler.release>${missing}</maven.compiler.release></properties>")},
                       "maven", 17)
        self.assertTrue(any("could not be resolved" in n for n in d.notes))

    def test_bad_xml_noted_not_fatal(self):
        d = self.check({"pom.xml": "<project><unclosed></project>"}, "maven", 17)
        self.assertTrue(any("could not parse" in n for n in d.notes))

    def test_java_version_file_wins(self):
        self.check({"pom.xml": POM.format("<properties><maven.compiler.release>11</maven.compiler.release></properties>"),
                    ".java-version": "21.0.4\n"}, "maven", 21, ".java-version")

    def test_sdkmanrc(self):
        self.check({"pom.xml": POM.format(""), ".sdkmanrc": "java=25.0.1-tem\n"}, "maven", 25)

    def test_mvn_jdk_file(self):
        self.check({"pom.xml": POM.format(""), ".mvn/jdk": "21\n"}, "maven", 21, ".mvn/jdk")

    def test_wrapper(self):
        self.assertTrue(self.check({"pom.xml": POM.format(""), "mvnw": "#!/bin/sh"}, "maven", 17).wrapper)

    # --- Gradle
    def test_gradle_toolchain(self):
        self.check({"build.gradle": "java { toolchain { languageVersion = JavaLanguageVersion.of(21) } }"}, "gradle", 21)

    def test_gradle_kts_jvmtoolchain(self):
        self.check({"build.gradle.kts": "kotlin { jvmToolchain(17) }"}, "gradle", 17)

    def test_gradle_source_compat(self):
        self.check({"build.gradle": "sourceCompatibility = '11'"}, "gradle", 11)

    def test_gradle_enum(self):
        self.check({"build.gradle.kts": "java { sourceCompatibility = JavaVersion.VERSION_17 }"}, "gradle", 17)

    def test_gradle_enum_1_8(self):
        self.check({"build.gradle": "sourceCompatibility = JavaVersion.VERSION_1_8"}, "gradle", 8)

    def test_gradle_release(self):
        self.check({"build.gradle": "tasks.withType(JavaCompile) { options.release = 21 }"}, "gradle", 21)

    def test_settings_only(self):
        self.check({"settings.gradle": "rootProject.name='x'"}, "gradle", 17)

    # --- both / none / override
    def test_both_prefers_maven_and_notes(self):
        d = self.check({"pom.xml": POM.format("<properties><java.version>17</java.version></properties>"),
                        "build.gradle": "x"}, "maven", 17)
        self.assertTrue(any("both" in n for n in d.notes))

    def test_force_gradle(self):
        self.check({"pom.xml": POM.format(""), "build.gradle": "jvmToolchain(21)", "gradlew": ""}, "gradle", 21, force="gradle")

    def test_unknown(self):
        self.assertEqual(detect(self.repo("empty", {"README": "x"})).tool, "unknown")

    def test_normalise(self):
        for raw, want in [("1.8", 8), ("17.0.2", 17), ("21-tem", 21), ("temurin-21.0.1", 21), ("abc", None), ("", None)]:
            self.assertEqual(normalise(raw), want, raw)


if __name__ == "__main__":
    unittest.main()
