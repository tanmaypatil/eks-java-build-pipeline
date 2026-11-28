"""Experiment plans: parse config/experiments.txt and lay out the runs of an experiment session.

The driver (cli.py cmd_experiment) brings the cluster up once, runs one unmeasured warm-up pass, then
REPS rounds of every variant, each variant being an ordinary pipeline run with its own settings.
"""
from __future__ import annotations

import random
import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path

from jbpcore import ConfigError

# Settings a variant may change. Anything else (cluster shape, namespace...) must stay fixed for the whole
# session, or the variants wouldn't be comparable.
VARIANT_KEYS = {"BUILD_CPU", "BUILD_MEM", "DEP_CACHE", "MAVEN_ARGS_EXTRA", "GRADLE_ARGS_EXTRA", "MAVEN_OPTS_EXTRA",
                "BUILD_CACHE", "BUILD_CACHE_EDIT", "CLEAR_CACHE", "BUILD_TIMEOUT_S", "DEP_BUMP", "MAX_PARALLEL"}


@dataclass
class Variant:
    experiment: str
    name: str
    settings: dict[str, str]
    repos: list[str]


@dataclass
class Experiment:
    id: str
    order: str                     # shuffle | fixed
    objective: str = ""
    expect: str = ""
    baseline: str = ""
    variants: list[Variant] = field(default_factory=list)


@dataclass
class Plan:
    settings: dict[str, str]
    experiments: list[Experiment]


def resolve_repos(spec: str, known: list[str]) -> list[str]:
    """REPOS=p1,p3,petclinic -> repo names from repos.txt. pN means the perf-pN-* repo; "all" means every one."""
    if spec.strip() == "all":
        return list(known)
    out = []
    for item in (s.strip() for s in spec.split(",") if s.strip()):
        if re.fullmatch(r"p\d+", item):
            hits = [n for n in known if n.startswith(f"perf-{item}-")]
        else:
            hits = [n for n in known if n == item]
        if len(hits) != 1:
            raise ConfigError(f"REPOS: {item!r} matches {len(hits)} repos in the repo list")
        out.append(hits[0])
    return out


def parse_plan(text: str, known_repos: list[str]) -> Plan:
    plan = Plan({}, [])
    by_id: dict[str, Experiment] = {}
    for no, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        try:
            words = shlex.split(line)
        except ValueError as e:
            raise ConfigError(f"experiments line {no}: {e}")
        kind, rest = words[0], words[1:]

        def kv(items):
            out = {}
            for w in items:
                if "=" not in w:
                    raise ConfigError(f"experiments line {no}: expected KEY=VALUE, got {w!r}")
                k, v = w.split("=", 1)
                out[k] = v
            return out

        if kind == "set":
            plan.settings.update(kv(rest))
        elif kind == "experiment":
            if len(rest) < 2 or rest[1] not in ("shuffle", "fixed"):
                raise ConfigError(f"experiments line {no}: use `experiment ID shuffle|fixed key=value...`")
            opts = kv(rest[2:])
            exp = Experiment(rest[0], rest[1], opts.get("objective", ""), opts.get("expect", ""), opts.get("baseline", ""))
            by_id[exp.id] = exp
            plan.experiments.append(exp)
        elif kind == "variant":
            if len(rest) < 2 or rest[0] not in by_id:
                raise ConfigError(f"experiments line {no}: `variant ID NAME ...` needs an experiment line for ID first")
            opts = kv(rest[2:])
            repos = resolve_repos(opts.pop("REPOS", "all"), known_repos)
            bad = set(opts) - VARIANT_KEYS
            if bad:
                raise ConfigError(f"experiments line {no}: a variant can't change {', '.join(sorted(bad))}")
            if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", rest[1]):
                raise ConfigError(f"experiments line {no}: variant name {rest[1]!r}: use a-z, 0-9 and -")
            by_id[rest[0]].variants.append(Variant(rest[0], rest[1], opts, repos))
        else:
            raise ConfigError(f"experiments line {no}: unknown keyword {kind!r}")
    for exp in plan.experiments:
        names = [v.name for v in exp.variants]
        if not names:
            raise ConfigError(f"experiment {exp.id} has no variants")
        exp.baseline = exp.baseline or names[0]
        if exp.baseline not in names:
            raise ConfigError(f"experiment {exp.id}: baseline {exp.baseline!r} is not one of its variants")
    return plan


def select(plan: Plan, only: str) -> Plan:
    """Keep only the experiments named in a comma list (empty = all)."""
    if not only:
        return plan
    want = [s.strip() for s in only.split(",") if s.strip()]
    missing = set(want) - {e.id for e in plan.experiments}
    if missing:
        raise ConfigError(f"no such experiment: {', '.join(sorted(missing))}")
    return Plan(plan.settings, [e for e in plan.experiments if e.id in want])


def schedule(plan: Plan, reps: int, seed: int | None = None) -> list[tuple[int, Variant]]:
    """Run order: for each rep, experiment by experiment; variants shuffled unless the experiment is fixed."""
    rnd = random.Random(seed)
    out = []
    for rep in range(1, reps + 1):
        for exp in plan.experiments:
            vs = list(exp.variants)
            if exp.order == "shuffle":
                rnd.shuffle(vs)
            out += [(rep, v) for v in vs]
    return out


def run_id(session: str, rep: int, v: Variant) -> str:
    """A RUN_ID that is also a valid Kubernetes label value (<= 63 chars, a-z0-9 and -)."""
    rid = f"{session}-r{rep}-{v.experiment}-{v.name}".lower()
    rid = re.sub(r"[^a-z0-9-]", "-", rid)
    return rid[:63].strip("-")


def filtered_repo_file(repos_txt: Path, names: list[str], out: Path) -> Path:
    """Write a repos.txt holding only `names`, in the original file's order."""
    keep = []
    for line in repos_txt.read_text().splitlines():
        parts = line.split()
        if parts and not parts[0].startswith("#") and parts[0] in names:
            keep.append(line)
    out.write_text("\n".join(keep) + "\n")
    return out
