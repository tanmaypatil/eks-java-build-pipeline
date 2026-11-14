#!/usr/bin/env python3
"""Build a self-contained HTML report from a run directory.

usage: make_report.py <run-dir>
reads  <run-dir>/results/<repo>.json (+ .pod.json, .build.log), phases.env, run.env, cluster.env
writes <run-dir>/report.html and <run-dir>/summary.csv
"""
import csv, datetime as dt, glob, html, json, os, re, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
run_dir = sys.argv[1]
res_dir = os.path.join(run_dir, "results")


def read_env(path):
    out = {}
    if os.path.exists(path):
        for line in open(path):
            m = re.match(r'\s*([A-Za-z_][A-Za-z0-9_]*)=(.*)', line)
            if m:
                out[m.group(1)] = m.group(2).strip().strip('"')
    return out


def defaults_from_run_env():
    """Prices and sizes: the run's effective settings if saved, else config/settings.env; env overrides."""
    eff = os.path.join(run_dir, "settings.effective.env")
    d = read_env(eff if os.path.exists(eff) else os.path.join(ROOT, "config", "settings.env"))
    return {k: os.environ.get(k, v) for k, v in d.items()}


def ts(s):
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp() if s else None


cfg = defaults_from_run_env()
phases = read_env(os.path.join(run_dir, "phases.env"))
runenv = read_env(os.path.join(run_dir, "run.env"))
cluster = read_env(os.path.join(run_dir, "cluster.env"))
P = {k: float(cfg.get(k, 0)) for k in ("PRICE_EKS_HOUR", "PRICE_NODE_HOUR", "PRICE_EBS_GB_MONTH", "PRICE_IPV4_HOUR")}

rows = []
for f in sorted(glob.glob(os.path.join(res_dir, "*.json"))):
    if f.endswith((".build.json", ".pod.json")) or os.path.basename(f).startswith("_"):
        continue
    try:
        r = json.load(open(f))
    except Exception:
        continue
    if not isinstance(r, dict):
        continue
    name = r.get("repo") or os.path.basename(f)[:-5]
    r["repo"] = name
    pod_f = os.path.join(res_dir, name + ".pod.json")
    r["queue_s"] = r["startup_s"] = None
    if os.path.exists(pod_f):
        try:
            pod = json.load(open(pod_f))
            created = ts(pod["metadata"]["creationTimestamp"])
            sched = next((ts(c["lastTransitionTime"]) for c in pod["status"].get("conditions", []) if c["type"] == "PodScheduled"), None)
            term = (pod["status"].get("containerStatuses") or [{}])[0].get("state", {}).get("terminated", {})
            started = ts(term.get("startedAt"))
            if created and sched:
                r["queue_s"] = max(0, sched - created)
            if sched and started:
                r["startup_s"] = max(0, started - sched)
            r["node"] = pod["spec"].get("nodeName")
        except Exception:
            pass
    att = os.path.join(res_dir, name + ".attempts")
    r["attempts"] = int(open(att).read().strip() or 1) if os.path.exists(att) else 1
    r["log"] = "results/%s.build.log" % name if os.path.exists(os.path.join(res_dir, name + ".build.log")) else None
    r["passed"] = bool(r.get("ok")) and r.get("rc", 1) == 0
    rows.append(r)

n = len(rows)
npass = sum(r["passed"] for r in rows)
build_total = sum(r.get("build_s") or 0 for r in rows)
clone_total = sum(r.get("clone_s") or 0 for r in rows)
tests = sum(r.get("tests") or 0 for r in rows)
tfail = sum((r.get("failures") or 0) + (r.get("errors") or 0) for r in rows)

def p(k):
    # phase names written by the jbp driver: up/deploy/orchestrate/collect/report/down _start|_end
    aliases = {"run_start": ("run_start", "deploy_start", "orchestrate_start"),
               "run_end": ("run_end", "collect_end", "orchestrate_end")}
    for name in aliases.get(k, (k,)):
        if name in phases:
            return int(phases[name])
    return None

run_wall = (p("run_end") or 0) - (p("run_start") or 0) if p("run_start") else None
up_s = p("up_end") - p("up_start") if p("up_end") and p("up_start") else None
down_s = p("down_end") - p("down_start") if p("down_end") and p("down_start") else None

# ---------- cost ----------
nodes = int(cluster.get("NODE_COUNT", cfg.get("NODE_COUNT", 1)))
disk = int(cfg.get("NODE_DISK_GB", 40))
pvc = int(re.sub(r"\D", "", cfg.get("PVC_SIZE", "30Gi")) or 30)

def cost(cp_s, node_s):
    h_cp, h_node = cp_s / 3600, node_s / 3600
    ebs = (nodes * disk + pvc) * P["PRICE_EBS_GB_MONTH"] / 730 * h_node
    return {
        "control plane": P["PRICE_EKS_HOUR"] * h_cp,
        "spot nodes": P["PRICE_NODE_HOUR"] * nodes * h_node,
        "EBS (node disks + PVC)": ebs,
        "public IPv4": P["PRICE_IPV4_HOUR"] * nodes * h_node,
    }

actual = None
if up_s and p("down_end"):
    cp = p("down_end") - p("up_start")
    node_s = (p("down_start") - p("up_end")) + 6 * 60 + 3 * 60  # nodes start ~6 min before "up" ends, die ~3 min into teardown
    actual = cost(cp, node_s)
# projection for a typical ephemeral run of this workload: 18 min up, builds, 3 min collect, 12 min down
work = run_wall or (build_total + clone_total)
proj = cost(18 * 60 + work + 180 + 12 * 60, 6 * 60 + work + 180 + 3 * 60)

# ---------- html ----------
e = html.escape

def fmt(v, unit="", nd=0):
    if v is None or v == "":
        return '<span class="mute">–</span>'
    if isinstance(v, (int, float)):
        return ("{:,.%df}" % nd).format(v) + unit
    return e(str(v)) + unit

def mmss(s):
    if s is None:
        return "–"
    s = int(round(s))
    return "%d:%02d" % (s // 60, s % 60) if s >= 60 else "%ds" % s

# stacked bars: clone | wait+pull | build
maxt = max([(r.get("clone_s") or 0) + (r.get("queue_s") or 0) + (r.get("startup_s") or 0) + (r.get("build_s") or 0) for r in rows] or [1]) or 1
bar_h, gap, label_w, W = 22, 10, 170, 760
svg = ['<svg viewBox="0 0 %d %d" role="img" aria-label="Time per repo" class="chart">' % (W, len(rows) * (bar_h + gap) + 30)]
for i, r in enumerate(rows):
    y = i * (bar_h + gap)
    x = label_w
    svg.append('<text x="%d" y="%d" class="lbl" text-anchor="end">%s</text>' % (label_w - 8, y + 15, e(r["repo"])))
    for key, cls in (("clone_s", "s1"), ("queue_s", "s2"), ("startup_s", "s2"), ("build_s", "s3" if r["passed"] else "s4")):
        v = r.get(key) or 0
        w = (W - label_w - 60) * v / maxt
        if w > 0.5:
            svg.append('<rect x="%.1f" y="%d" width="%.1f" height="%d" class="%s"><title>%s: %.1fs</title></rect>' % (x, y, w, bar_h, cls, key[:-2], v))
        x += w
    tot = sum(r.get(k) or 0 for k in ("clone_s", "queue_s", "startup_s", "build_s"))
    svg.append('<text x="%.1f" y="%d" class="val">%s</text>' % (x + 6, y + 15, mmss(tot)))
svg.append("</svg>")

def cost_table(c, title):
    tot = sum(c.values())
    rows_ = "".join("<tr><td>%s</td><td class=num>$%.3f</td></tr>" % (e(k), v) for k, v in c.items())
    return '<div class="card"><h3>%s</h3><table class="mini">%s<tr class="tot"><td>Total</td><td class=num>$%.2f</td></tr></table></div>' % (title, rows_, tot)

trs = []
for r in rows:
    status = '<span class="pill ok">PASS</span>' if r["passed"] else '<span class="pill bad">FAIL%s</span>' % (" · " + e(r.get("stage")) if r.get("stage") else "")
    tests_cell = fmt(r.get("tests")) + ("" if not ((r.get("failures") or 0) + (r.get("errors") or 0)) else ' <span class="bad-t">(%d failed)</span>' % ((r.get("failures") or 0) + (r.get("errors") or 0)))
    img = (r.get("image") or "").split("/")[-1]
    trs.append("<tr>" + "".join([
        "<td><b>%s</b><div class=sub>%s%s</div></td>" % (e(r["repo"]), e(r.get("commit") or ""), (" · " + e(r["subdir"])) if r.get("subdir") else ""),
        "<td>%s</td>" % status,
        "<td>%s%s</td>" % (e(r.get("tool") or "–"), " <span class=sub>(wrapper)</span>" if r.get("wrapper") == "yes" else ""),
        "<td>%s<div class=sub>%s</div></td>" % (e(str(r.get("java_required") or "–")), e(r.get("java_source") or "")),
        "<td><code>%s</code></td>" % e(img),
        "<td class=num>%s</td>" % mmss(r.get("clone_s")),
        "<td class=num>%s</td>" % mmss((r.get("queue_s") or 0) + (r.get("startup_s") or 0) if r.get("startup_s") is not None else None),
        "<td class=num><b>%s</b></td>" % mmss(r.get("build_s")),
        "<td class=num>%s</td>" % tests_cell,
        "<td class=num>%s<div class=sub>of %s</div></td>" % (fmt(r.get("avg_cores"), "", 2), fmt(r.get("cpu_limit") or r.get("pod_cpu"))),
        "<td class=num>%s</td>" % fmt(r.get("throttled_pct"), "%", 0),
        "<td class=num>%s<div class=sub>of %s</div></td>" % (fmt(r.get("peak_mem_mib"), " MiB"), e(str(r.get("pod_mem") or ""))),
        "<td>%s%s</td>" % ('<a href="%s">log</a>' % e(r["log"]) if r.get("log") else "", " · %d tries" % r["attempts"] if r["attempts"] > 1 else ""),
    ]) + "</tr>")
    if not r["passed"]:
        msg = "\n".join(x for x in [
            ("[%s] %s" % (r.get("failure_kind"), r.get("failure_message", ""))) if r.get("failure_kind") else "",
            (r.get("error_excerpt") or "").replace("|", "\n")] if x)
        if msg:
            trs.append('<tr class="err"><td colspan=13><code>%s</code></td></tr>' % e(msg))

settings = " · ".join("%s=%s" % (k, e(v)) for k, v in runenv.items() if v and k != "RUN_ID")
phase_line = " · ".join(x for x in [
    "cluster up %s" % mmss(up_s) if up_s else "",
    "builds + collect %s" % mmss(run_wall) if run_wall else "",
    "teardown %s" % mmss(down_s) if down_s else "",
] if x)

page = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Java build run</title>
<style>
:root{--bg:#fbfaf8;--fg:#1d1d1b;--mute:#6b6a66;--line:#e4e2dc;--card:#fff;--s1:#8fa7c6;--s2:#c9c3b6;--s3:#2f7d5b;--s4:#c2473b;--ok-bg:#e3f2ea;--bad-bg:#f8e3e0}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--bg:#161615;--fg:#ecebe7;--mute:#9a9893;--line:#2e2d2a;--card:#1f1f1d;--s1:#6f8bb0;--s2:#56524a;--s3:#4fae84;--s4:#e06a5d;--ok-bg:#1f3a2d;--bad-bg:#40221f}}
:root[data-theme="dark"]{--bg:#161615;--fg:#ecebe7;--mute:#9a9893;--line:#2e2d2a;--card:#1f1f1d;--s1:#6f8bb0;--s2:#56524a;--s3:#4fae84;--s4:#e06a5d;--ok-bg:#1f3a2d;--bad-bg:#40221f}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.45 system-ui,-apple-system,Segoe UI,sans-serif}
main{max-width:1200px;margin:0 auto;padding:28px 16px 60px}
h1{font-size:22px;margin:0 0 4px}h2{font-size:15px;margin:32px 0 10px}h3{font-size:13px;margin:0 0 8px;color:var(--mute);font-weight:600}
.mute,.sub{color:var(--mute)}.sub{font-size:12px}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin-top:18px}
.tile,.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 14px}
.tile .v{font-size:24px;font-weight:650}.tile .k{color:var(--mute);font-size:12px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:10px}
.wrap{overflow-x:auto;border:1px solid var(--line);border-radius:10px;background:var(--card)}
table{border-collapse:collapse;width:100%%}th,td{padding:8px 10px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}
th{font-size:12px;color:var(--mute);font-weight:600;white-space:nowrap}.num{text-align:right;white-space:nowrap}
table.mini td{border:0;padding:3px 0}table.mini .tot td{border-top:1px solid var(--line);font-weight:650;padding-top:6px}
.pill{font-size:11px;font-weight:700;padding:2px 7px;border-radius:99px}.ok{background:var(--ok-bg);color:var(--s3)}.bad{background:var(--bad-bg);color:var(--s4)}
.bad-t{color:var(--s4)}tr.err td{background:var(--bad-bg);font-size:12px;white-space:pre-wrap}
code{font:12px ui-monospace,Menlo,monospace}a{color:var(--s1)}
.chart{width:100%%;max-width:900px}.chart .lbl,.chart .val{font-size:12px;fill:var(--fg)}.chart .val{fill:var(--mute)}
.s1{fill:var(--s1)}.s2{fill:var(--s2)}.s3{fill:var(--s3)}.s4{fill:var(--s4)}
.legend{display:flex;gap:14px;font-size:12px;color:var(--mute);margin:4px 0 8px}.legend i{display:inline-block;background:var(--c);width:10px;height:10px;border-radius:2px;margin-right:5px;vertical-align:-1px}
</style></head><body><main>
<h1>Java build run %(run)s</h1>
<div class="mute">%(ctx)s · %(when)s%(phase)s</div>
<div class="tiles">
 <div class="tile"><div class="v">%(npass)d/%(n)d</div><div class="k">repos passed</div></div>
 <div class="tile"><div class="v">%(build)s</div><div class="k">total build time</div></div>
 <div class="tile"><div class="v">%(wall)s</div><div class="k">pipeline wall time</div></div>
 <div class="tile"><div class="v">%(tests)s</div><div class="k">tests run%(tfail)s</div></div>
 <div class="tile"><div class="v">%(cost)s</div><div class="k">%(costk)s</div></div>
</div>
<h2>Time per repo</h2>
<div class="legend"><span><i style="--c:var(--s1)"></i>clone</span><span><i style="--c:var(--s2)"></i>wait + image pull</span><span><i style="--c:var(--s3)"></i>build (pass)</span><span><i style="--c:var(--s4)"></i>build (fail)</span></div>
%(svg)s
<h2>Repos</h2>
<div class="wrap"><table>
<tr><th>Repo</th><th>Result</th><th>Tool</th><th>Java needed</th><th>Image</th><th class=num>Clone</th><th class=num>Wait+pull</th><th class=num>Build</th><th class=num>Tests</th><th class=num>Avg cores</th><th class=num>CPU throttled</th><th class=num>Peak mem</th><th></th></tr>
%(trs)s
</table></div>
<h2>Cost</h2>
<div class="cards">%(costs)s</div>
<p class="sub">Prices from config/run.env (EKS $%(peks).2f/h, node $%(pnode).3f/h spot, EBS $%(pebs).2f/GB-month, IPv4 $%(pip).3f/h). Spot prices move; check the EC2 spot pricing page for your region.</p>
<h2>Settings</h2><p class="sub">%(settings)s</p>
</main></body></html>"""

when = dt.datetime.fromtimestamp(p("run_start")).strftime("%Y-%m-%d %H:%M") if p("run_start") else ""
costs = (cost_table(actual, "This run (measured phases)") if actual else "") + cost_table(proj, "Projected ephemeral EKS run for this workload")
open(os.path.join(run_dir, "report.html"), "w").write(page % dict(
    run=e(runenv.get("RUN_ID", os.path.basename(run_dir.rstrip("/")))), ctx=e(runenv.get("context", "")), when=when,
    phase=(" · " + phase_line) if phase_line else "", npass=npass, n=n, build=mmss(build_total), wall=mmss(run_wall),
    tests="{:,}".format(tests), tfail=(", %d failed" % tfail) if tfail else "",
    cost="$%.2f" % sum((actual or proj).values()), costk="cost of this run" if actual else "projected EKS cost / run",
    svg="".join(svg), trs="\n".join(trs), costs=costs, settings=settings,
    peks=P["PRICE_EKS_HOUR"], pnode=P["PRICE_NODE_HOUR"], pebs=P["PRICE_EBS_GB_MONTH"], pip=P["PRICE_IPV4_HOUR"]))

cols = ["repo", "passed", "tool", "wrapper", "java_required", "java_source", "jdk", "image", "commit", "clone_s", "queue_s", "startup_s",
        "build_s", "tests", "failures", "errors", "skipped", "cpu_s", "avg_cores", "cpu_limit", "throttled_pct", "peak_mem_mib", "oom_kills", "attempts"]
with open(os.path.join(run_dir, "summary.csv"), "w", newline="") as fh:
    w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
    w.writeheader()
    for r in rows:
        w.writerow(r)
print(os.path.join(run_dir, "report.html"))
