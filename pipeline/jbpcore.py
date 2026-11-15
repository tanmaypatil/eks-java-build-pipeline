"""Shared helpers: settings, logging, running commands, and stage tracking.

Standard library only, so it runs unchanged on a laptop, in this repo's driver,
and inside the orchestrator pod (alpine/k8s ships python3).
"""
from __future__ import annotations

import json
import logging
import os
import re
import shlex
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Mapping, Sequence

ROOT = Path(__file__).resolve().parent.parent  # repo root when run from a checkout


# --------------------------------------------------------------------------- time

def now_ms() -> int:
    return int(time.time() * 1000)


def utc_iso(ts: float | None = None) -> str:
    return datetime.fromtimestamp(ts if ts is not None else time.time(), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def fmt_duration(seconds: float | None) -> str:
    if seconds is None:
        return "-"
    s = int(round(seconds))
    return f"{s // 60}:{s % 60:02d}" if s >= 60 else f"{s}s"


# --------------------------------------------------------------------------- settings

_LINE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$")


def read_env_file(path: Path | str) -> dict[str, str]:
    """Parse a plain KEY=value file. '#' starts a comment line. Quotes around values are stripped."""
    out: dict[str, str] = {}
    p = Path(path)
    if not p.exists():
        return out
    for raw in p.read_text().splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        m = _LINE.match(raw)
        if m:
            v = m.group(2)
            if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                v = v[1:-1]
            out[m.group(1)] = v
    return out


class Settings(dict):
    """settings.env defaults, overridden by environment variables of the same name."""

    @classmethod
    def load(cls, path: Path | str | None = None, environ: Mapping[str, str] | None = None) -> "Settings":
        env = os.environ if environ is None else environ
        s = cls(read_env_file(path or ROOT / "config" / "settings.env"))
        # Only keys that exist in the file can be overridden from the environment, so an unrelated env var
        # with a matching-looking name can't silently change a run.
        for k in list(s):
            if k in env:
                s[k] = env[k]
        return s

    def int(self, key: str, default: int = 0) -> int:
        try:
            return int(str(self.get(key, default)).strip() or default)
        except ValueError:
            raise ConfigError(f"{key} must be a whole number, got {self.get(key)!r}")

    def float(self, key: str, default: float = 0.0) -> float:
        try:
            return float(str(self.get(key, default)).strip() or default)
        except ValueError:
            raise ConfigError(f"{key} must be a number, got {self.get(key)!r}")


def load_images(path: Path | str, registry: str) -> dict[str, str]:
    imgs = read_env_file(path)
    return {k: v.replace("{registry}", registry) for k, v in imgs.items()}


# --------------------------------------------------------------------------- errors

class JbpError(Exception):
    """Base error: message is meant for a human."""


class ConfigError(JbpError):
    pass


class CommandError(JbpError):
    def __init__(self, cmd: Sequence[str], rc: int, stdout: str, stderr: str):
        self.cmd, self.rc, self.stdout, self.stderr = list(cmd), rc, stdout, stderr
        tail = (stderr or stdout or "").strip().splitlines()[-5:]
        super().__init__(f"`{shlex.join(self.cmd)[:200]}` exited {rc}: " + (" | ".join(tail) or "(no output)"))


# --------------------------------------------------------------------------- logging

class _UTCFormatter(logging.Formatter):
    converter = time.gmtime

    def format(self, record: logging.LogRecord) -> str:
        repo = getattr(record, "repo", None)
        stage = getattr(record, "stage", None)
        ctx = ""
        if repo or stage:
            ctx = "[" + "|".join(x for x in (repo, stage) if x) + "] "
        record.ctx = ctx
        return super().format(record)


def setup_logging(component: str, logfile: Path | str | None = None, verbose: bool = False) -> logging.Logger:
    """Console + optional file. Format:
    2026-11-14T11:30:02Z INFO  orchestrator [perf-p2|DETECT] tool=maven java=21
    """
    fmt = _UTCFormatter("%(asctime)sZ %(levelname)-5s %(name)s %(ctx)s%(message)s", "%Y-%m-%dT%H:%M:%S")
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(logging.DEBUG)
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.DEBUG if verbose else logging.INFO)
    console.setFormatter(fmt)
    root.addHandler(console)
    if logfile:
        Path(logfile).parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(logfile)
        fh.setLevel(logging.DEBUG)  # file always has the detail (commands, retries)
        fh.setFormatter(fmt)
        root.addHandler(fh)
    return logging.getLogger(component)


def repo_logger(log: logging.Logger, repo: str, stage: str | None = None) -> logging.LoggerAdapter:
    return logging.LoggerAdapter(log, {"repo": repo, "stage": stage})


# --------------------------------------------------------------------------- commands

def run(cmd: Sequence[str], *, log: logging.Logger | logging.LoggerAdapter | None = None, check: bool = True,
        timeout: float | None = None, retries: int = 0, backoff: float = 5.0, input: str | None = None,
        env: Mapping[str, str] | None = None, cwd: str | Path | None = None,
        retry_on: Iterable[str] = ()) -> subprocess.CompletedProcess:
    """Run a command, capture output, retry on failure.

    retries: extra attempts after the first (delay grows: backoff, 2x, 4x ...).
    retry_on: if given, only retry when stderr/stdout contains one of these substrings.
    Raises CommandError (check=True) with the tail of stderr so logs show *why*.
    """
    log = log or logging.getLogger("cmd")
    full_env = None if env is None else {**os.environ, **env}
    attempt, delay = 0, backoff
    retry_on = tuple(retry_on)
    while True:
        attempt += 1
        log.debug("$ %s%s", shlex.join(cmd)[:500], f"  (attempt {attempt})" if attempt > 1 else "")
        t0 = time.time()
        try:
            cp = subprocess.run(list(cmd), capture_output=True, text=True, timeout=timeout, input=input,
                                env=full_env, cwd=cwd)
        except subprocess.TimeoutExpired as e:
            cp = subprocess.CompletedProcess(cmd, 124, e.stdout or "", (e.stderr or "") + f"\ntimed out after {timeout}s")
        except FileNotFoundError:
            raise JbpError(f"command not found: {cmd[0]} (is it installed and on PATH?)")
        log.debug("  -> rc=%s in %.1fs", cp.returncode, time.time() - t0)
        if cp.returncode == 0 or not check:
            return cp
        out = (cp.stderr or "") + (cp.stdout or "")
        # DECISION: retry? Only while attempts remain AND (if retry_on was given) the output contains one of
        # those substrings. This lets git clone retry on "Connection reset" but fail at once on "not found".
        retryable = attempt <= retries and (not retry_on or any(s in out for s in retry_on))
        if not retryable:
            raise CommandError(cmd, cp.returncode, cp.stdout, cp.stderr)
        log.warning("command failed (rc=%s), retrying in %.0fs: %s", cp.returncode, delay,
                    (out.strip().splitlines() or ["(no output)"])[-1][:200])
        time.sleep(delay)
        delay *= 2


def require_tools(*tools: str) -> None:
    from shutil import which
    missing = [t for t in tools if not which(t)]
    if missing:
        raise JbpError("missing tools on PATH: " + ", ".join(missing))


# --------------------------------------------------------------------------- files

def write_json_atomic(path: Path | str, data) -> None:
    """Write JSON so a reader never sees half a file (tmp file + rename)."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=p.name + ".", suffix=".tmp")
    with os.fdopen(fd, "w") as fh:
        json.dump(data, fh, indent=1, sort_keys=False, default=str)
    os.replace(tmp, p)


def read_json(path: Path | str, default=None):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return default


# --------------------------------------------------------------------------- stage tracking

@dataclass
class StageEvent:
    ts: str
    scope: str      # "run" or a repo name
    stage: str
    status: str     # started | ok | failed | skipped | info
    detail: dict


class StageTracker:
    """Records where the program is, for humans and for `jbp status`.

    status_file: current state (overwritten atomically on every change)
    events_file: append-only history, one JSON object per line
    """

    def __init__(self, status_file: Path | str, events_file: Path | str, run_id: str, component: str):
        self.status_file, self.events_file = Path(status_file), Path(events_file)
        self._lock = threading.Lock()
        # Same run id as the existing status file = this is a restart (e.g. after a spot reclaim):
        # keep the earlier start time and per-repo state instead of starting from a blank picture.
        prev = read_json(self.status_file, {}) or {}
        self.state = {
            "run_id": run_id,
            "component": component,
            "started": prev.get("started") if prev.get("run_id") == run_id else utc_iso(),
            "updated": utc_iso(),
            "phase": None,
            "repos": prev.get("repos", {}) if prev.get("run_id") == run_id else {},
        }
        self._flush()

    def phase(self, name: str, status: str = "started", **detail) -> None:
        with self._lock:
            self.state["phase"] = {"name": name, "status": status, "since": utc_iso(), **detail}
            self._event("run", name, status, detail)
            self._flush()

    def repo(self, repo: str, stage: str, status: str = "started", **detail) -> None:
        with self._lock:
            r = self.state["repos"].setdefault(repo, {"history": []})
            r.update({"stage": stage, "status": status, "since": utc_iso()})
            r.update({k: v for k, v in detail.items() if k in ("error", "result", "image", "java", "tool", "attempt")})
            r["history"].append({"stage": stage, "status": status, "at": utc_iso()})
            self._event(repo, stage, status, detail)
            self._flush()

    def summary(self) -> dict:
        repos = self.state["repos"]
        done = [r for r in repos.values() if r.get("stage") in ("DONE", "FAILED", "SKIPPED")]
        return {
            "total_seen": len(repos),
            "finished": len(done),
            "passed": sum(1 for r in repos.values() if r.get("result") == "PASS"),
            "failed": sum(1 for r in repos.values() if r.get("result") == "FAIL"),
            "active": {k: f'{v.get("stage")} since {v.get("since")}' for k, v in repos.items()
                       if v.get("stage") not in ("DONE", "FAILED", "SKIPPED")},
        }

    def _event(self, scope, stage, status, detail) -> None:
        ev = StageEvent(utc_iso(), scope, stage, status, detail)
        self.events_file.parent.mkdir(parents=True, exist_ok=True)
        with open(self.events_file, "a") as fh:
            fh.write(json.dumps(ev.__dict__, default=str) + "\n")

    def _flush(self) -> None:
        self.state["updated"] = utc_iso()
        self.state["summary"] = self.summary() if "repos" in self.state else {}
        write_json_atomic(self.status_file, self.state)


def run_streaming(cmd: Sequence[str], *, log: logging.Logger | logging.LoggerAdapter, prefix: str = "",
                  timeout: float | None = None, env: Mapping[str, str] | None = None, raw: bool = False) -> int:
    """Run a long command (eksctl, kubectl logs -f) and log each output line as it arrives."""
    log.debug("$ %s", shlex.join(cmd)[:500])
    full_env = None if env is None else {**os.environ, **env}
    try:
        proc = subprocess.Popen(list(cmd), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=full_env)
    except FileNotFoundError:
        raise JbpError(f"command not found: {cmd[0]} (is it installed and on PATH?)")
    deadline = time.time() + timeout if timeout else None
    tail: list[str] = []
    assert proc.stdout is not None
    for line in proc.stdout:
        line = line.rstrip()
        if line and raw:   # already-formatted log lines from a child: print as-is, keep a copy in the log file
            print(prefix + line, flush=True)
            log.debug("%s%s", prefix, line)
        elif line:
            log.info("%s%s", prefix, line)
            tail = (tail + [line])[-20:]
        if deadline and time.time() > deadline:
            proc.kill()
            raise JbpError(f"`{cmd[0]}` timed out after {timeout}s")
    rc = proc.wait()
    if rc != 0:
        raise CommandError(cmd, rc, "\n".join(tail), "")
    return rc
