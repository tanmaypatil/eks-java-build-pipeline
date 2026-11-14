"""Work out which build tool a repo uses and which Java version it needs.

Usage:  python3 detect.py <repo-dir> [--tool maven|gradle]   -> prints JSON

Order (first hit wins):
  1. explicit version files: .java-version, .sdkmanrc, .tool-versions, .mvn/jdk
  2. Maven: pom.xml parsed as XML, ${property} references resolved (incl. a local parent pom).
     Highest value across all modules of, in priority order:
     maven.compiler.release, compiler-plugin <release>, java.version, maven.compiler.target,
     compiler-plugin <target>, maven.compiler.source, compiler-plugin <source>, toolchains.xml
  3. Gradle (regex over *.gradle / *.gradle.kts / gradle.properties): toolchain languageVersion,
     jvmToolchain(N), options.release, JavaVersion.VERSION_N, source/targetCompatibility
  4. DEFAULT_JAVA (17)
"""
from __future__ import annotations

import json
import os
import re
import sys
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from pathlib import Path

SKIP_DIRS = {"target", "build", "node_modules", ".git", ".gradle", ".idea", ".mvn"}
DEFAULT_JAVA = int(os.environ.get("DEFAULT_JAVA", "17"))


@dataclass
class Detection:
    tool: str            # maven | gradle | unknown
    wrapper: bool        # mvnw / gradlew present
    java: int            # minimum Java version the build needs
    source: str          # where that number came from (shown in the report)
    notes: list[str] = field(default_factory=list)


def normalise(v: str | None) -> int | None:
    """'1.8' -> 8, '17.0.2' -> 17, '21-tem' -> 21, 'temurin-21.0.1' -> 21; None if no number."""
    if not v:
        return None
    m = re.search(r"(\d+)(?:\.(\d+))?", str(v))
    if not m:
        return None
    major = int(m.group(1))
    if major == 1 and m.group(2):
        major = int(m.group(2))
    return major if 6 <= major <= 99 else None


def _walk(root: Path, names: set[str] | None = None, suffixes: tuple[str, ...] = ()) -> list[Path]:
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for f in filenames:
            if (names and f in names) or (suffixes and f.endswith(suffixes)):
                out.append(Path(dirpath) / f)
    return sorted(out)


# --------------------------------------------------------------------------- version files

def from_version_files(root: Path) -> tuple[int, str] | None:
    checks = [
        (".java-version", lambda t: t.strip().splitlines()[0] if t.strip() else None),
        (".sdkmanrc", lambda t: next((l.split("=", 1)[1] for l in t.splitlines() if l.startswith("java=")), None)),
        (".tool-versions", lambda t: next((l.split()[1] for l in t.splitlines() if l.startswith("java ") and len(l.split()) > 1), None)),
        (".mvn/jdk", lambda t: t.strip().splitlines()[0] if t.strip() else None),
    ]
    for name, extract in checks:
        p = root / name
        if p.is_file():
            v = normalise(extract(p.read_text(errors="replace")))
            if v:
                return v, name
    return None


# --------------------------------------------------------------------------- maven

def _strip_ns(tree: ET.Element) -> ET.Element:
    for el in tree.iter():
        if isinstance(el.tag, str) and "}" in el.tag:
            el.tag = el.tag.split("}", 1)[1]
    return tree


class Pom:
    def __init__(self, path: Path):
        self.path = path
        self.root = _strip_ns(ET.parse(path).getroot())
        self.props = {c.tag: (c.text or "").strip() for c in self.root.findall("properties/*")}
        self.parent: Pom | None = None

    def parent_path(self) -> Path | None:
        par = self.root.find("parent")
        if par is None:
            return None
        rel = (par.findtext("relativePath") or "../pom.xml").strip()
        if not rel:
            return None
        p = (self.path.parent / rel)
        p = p / "pom.xml" if p.is_dir() else p
        return p.resolve() if p.is_file() else None

    def prop(self, name: str) -> str | None:
        pom: Pom | None = self
        while pom:
            if name in pom.props:
                return pom.props[name]
            pom = pom.parent
        return None

    def resolve(self, value: str | None, depth: int = 0) -> str | None:
        if value is None or depth > 10:
            return value
        def sub(m):
            return self.resolve(self.prop(m.group(1)), depth + 1) or m.group(0)
        return re.sub(r"\$\{([^}]+)\}", sub, value)

    def compiler_config(self, key: str) -> list[str]:
        vals = []
        for plugin in self.root.iter("plugin"):
            if (plugin.findtext("artifactId") or "").strip() != "maven-compiler-plugin":
                continue
            for cfg in plugin.iter("configuration"):
                t = cfg.findtext(key)
                if t:
                    vals.append(t.strip())
        return vals


def from_maven(root: Path, notes: list[str]) -> tuple[int, str] | None:
    poms: dict[Path, Pom] = {}
    for p in _walk(root, names={"pom.xml"}):
        try:
            poms[p.resolve()] = Pom(p)
        except ET.ParseError as e:
            notes.append(f"could not parse {p.relative_to(root)}: {e}")
    for pom in poms.values():  # link local parents so ${java.version} defined in a parent resolves
        pp = pom.parent_path()
        if pp and pp in poms:
            pom.parent = poms[pp]
        elif pp:
            try:
                pom.parent = Pom(pp)
            except (ET.ParseError, OSError):
                pass

    rules = [
        ("maven.compiler.release", lambda pom: [pom.prop("maven.compiler.release")]),
        ("compiler plugin <release>", lambda pom: pom.compiler_config("release")),
        ("java.version property", lambda pom: [pom.prop("java.version")]),
        ("maven.compiler.target", lambda pom: [pom.prop("maven.compiler.target")]),
        ("compiler plugin <target>", lambda pom: pom.compiler_config("target")),
        ("maven.compiler.source", lambda pom: [pom.prop("maven.compiler.source")]),
        ("compiler plugin <source>", lambda pom: pom.compiler_config("source")),
    ]
    for label, get in rules:
        found = []
        for pom in poms.values():
            for raw in get(pom):
                v = normalise(pom.resolve(raw))
                if v:
                    found.append(v)
                elif raw:
                    notes.append(f"{label}={raw!r} in {pom.path.relative_to(root)} could not be resolved")
        if found:
            return max(found), label
    tc = root / ".mvn" / "toolchains.xml"
    if tc.is_file():
        try:
            versions = [normalise(e.text) for e in _strip_ns(ET.parse(tc).getroot()).iter("version")]
            versions = [v for v in versions if v]
            if versions:
                return max(versions), ".mvn/toolchains.xml"
        except ET.ParseError:
            pass
    return None


# --------------------------------------------------------------------------- gradle

GRADLE_RULES = [
    ("toolchain languageVersion", r"JavaLanguageVersion\.of\(\s*(\d+)"),
    ("jvmToolchain", r"jvmToolchain\(\s*(\d+)"),
    ("options.release", r"release(?:\.set\()?\s*=?\s*\(?\s*(\d+)"),
    ("JavaVersion.VERSION_x", r"JavaVersion\.VERSION_((?:1_)?\d+)"),
    ("sourceCompatibility", r"(?:source|target)Compatibility\s*=\s*['\"]?([\d.]+)"),
    ("gradle.properties java version", r"(?m)^\s*(?:java\.?[vV]ersion|javaVersion)\s*=\s*([\d.]+)"),
]


def from_gradle(root: Path) -> tuple[int, str] | None:
    texts = [p.read_text(errors="replace") for p in
             _walk(root, names={"gradle.properties", "libs.versions.toml"}, suffixes=(".gradle", ".gradle.kts"))]
    for label, pattern in GRADLE_RULES:
        vals = [normalise(m.replace("_", ".")) for t in texts for m in re.findall(pattern, t)]
        vals = [v for v in vals if v]
        if vals:
            return max(vals), label
    return None


# --------------------------------------------------------------------------- entry

def detect(repo_dir: str | Path, force_tool: str | None = None) -> Detection:
    root = Path(repo_dir)
    if not root.is_dir():
        raise FileNotFoundError(f"not a directory: {root}")
    notes: list[str] = []
    has = lambda *names: any((root / n).is_file() for n in names)
    if force_tool:
        tool = force_tool
    elif has("pom.xml"):
        tool = "maven"
    elif has("build.gradle", "build.gradle.kts", "settings.gradle", "settings.gradle.kts"):
        tool = "gradle"
    else:
        tool = "unknown"
        notes.append("no pom.xml or build.gradle(.kts) at the build root")
    if has("pom.xml") and has("build.gradle", "build.gradle.kts") and not force_tool:
        notes.append("repo has both Maven and Gradle builds; using Maven (set tool=gradle to override)")
    wrapper = has("mvnw") if tool == "maven" else has("gradlew") if tool == "gradle" else False

    hit = from_version_files(root)
    if not hit and tool == "maven":
        hit = from_maven(root, notes)
    if not hit and tool == "gradle":
        hit = from_gradle(root)
    java, source = hit if hit else (DEFAULT_JAVA, "default (nothing declared)")
    return Detection(tool, wrapper, java, source, notes)


def main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    force = argv[argv.index("--tool") + 1] if "--tool" in argv else None
    print(json.dumps(asdict(detect(argv[0], force)), indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
