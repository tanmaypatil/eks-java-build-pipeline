#!/usr/bin/env python3
"""Compare the variants of an experiment session: one self-contained HTML page.

usage: compare.py <session-dir>
reads  <session-dir>/experiment.json and <session-dir>/<RUN_ID>/results/<repo>.json for every run
writes <session-dir>/comparison.html and <session-dir>/comparison.csv

Per experiment: its objective and what we expected, a repo x variant table of median build time with the
change against the baseline variant, a geometric-mean row across repos, an automatic verdict, and a
detail table (spread, CPU use, throttling, tests, cache hits, downloads).
"""
import csv, html, json, math, os, statistics, sys

session_dir = sys.argv[1]
meta = json.load(open(os.path.join(session_dir, "experiment.json")))
E = html.escape


def load(run_id, repo):
    try:
        r = json.load(open(os.path.join(session_dir, run_id, "results", repo + ".json")))
        return r if isinstance(r, dict) else None
    except (OSError, ValueError):
        return None


def num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def med(xs):
    xs = [x for x in xs if x is not None]
    return statistics.median(xs) if xs else None


def pct(new, base):
    if new is None or not base:
        return None
    return 100.0 * (new - base) / base


def fmt_s(x):
    return "–" if x is None else (f"{x:.1f} s" if x < 100 else f"{x:.0f} s")


def fmt_pct(p):
    return "" if p is None else f"{p:+.0f}%"


def num_td(x, f):
    return '<td class="num">' + ("–" if x is None else f.format(x)) + "</td>"


def tone(p):
    """CSS class for a change in build time: faster is good."""
    if p is None:
        return ""
    if p <= -10:
        return "good2"
    if p <= -3:
        return "good"
    if p >= 10:
        return "bad2"
    if p >= 3:
        return "bad"
    return "flat"


# ---- gather: (experiment, variant, repo) -> list of result dicts (one per rep)
cells = {}
for run in meta.get("runs", []):
    exp = next((e for e in meta["experiments"] if e["id"] == run["experiment"]), None)
    if not exp:
        continue
    var = next(v for v in exp["variants"] if v["name"] == run["variant"])
    for repo in var["repos"]:
        r = load(run["run_id"], repo)
        cells.setdefault((exp["id"], var["name"], repo), []).append(r or {"ok": False, "missing": True})

csv_rows = []
sections = []
for exp in meta["experiments"]:
    vnames = [v["name"] for v in exp["variants"]]
    repos = []
    for v in exp["variants"]:
        repos += [r for r in v["repos"] if r not in repos]
    base = exp["baseline"]
    stats = {}
    for vn in vnames:
        for repo in repos:
            rs = cells.get((exp["id"], vn, repo), [])
            okr = [r for r in rs if r.get("ok")]
            bs = [num(r.get("build_s")) for r in okr]
            stats[(vn, repo)] = {
                "n": len(rs), "ok": len(okr), "build": med(bs),
                "lo": min([b for b in bs if b is not None], default=None),
                "hi": max([b for b in bs if b is not None], default=None),
                "cores": med([num(r.get("avg_cores")) for r in okr]),
                "thr": med([num(r.get("throttled_pct")) for r in okr]),
                "tests": med([num(r.get("tests")) for r in okr]),
                "hits": med([num(r.get("cache_hits")) for r in okr]),
                "dl": med([num(r.get("downloaded_kb")) / 1024 if num(r.get("downloaded_kb")) is not None else None for r in okr]),
                "fails": sorted({r.get("failure_kind") or ("no result" if r.get("missing") else "failed")
                                 for r in rs if not r.get("ok")}),
            }
    if not any(s["n"] for s in stats.values()):
        sections.append(f'<section><h2>{E(exp["id"])}</h2><p class="muted">No runs recorded for this experiment.</p></section>')
        continue

    # geometric mean of time ratios vs baseline, across repos measured in both
    geo = {}
    for vn in vnames:
        ratios = [stats[(vn, r)]["build"] / stats[(base, r)]["build"] for r in repos
                  if stats[(vn, r)]["build"] and stats[(base, r)]["build"]]
        geo[vn] = (math.exp(sum(map(math.log, ratios)) / len(ratios)) - 1) * 100 if ratios else None

    # pivot table
    head = "".join(f'<th class="num">{E(vn)}{" <span class=tag>baseline</span>" if vn == base else ""}</th>' for vn in vnames)
    body = []
    for repo in repos:
        tds = []
        b = stats[(base, repo)]["build"]
        for vn in vnames:
            s = stats[(vn, repo)]
            p = None if vn == base else pct(s["build"], b)
            warn = ""
            bt = stats[(base, repo)]["tests"]
            if s["tests"] is not None and bt is not None and s["tests"] != bt:
                t_new, t_base = int(s["tests"]), int(bt)
                warn = f'<span class="warn" title="tests run vs {t_base} in the baseline">tests {t_new}</span>'
            if s["fails"]:
                why = E(", ".join(s["fails"]))
                warn += f'<span class="warn" title="{why}">{s["n"] - s["ok"]} failed</span>'
            tds.append(f'<td class="num {tone(p)}">{fmt_s(s["build"])}<br><small>{fmt_pct(p)}</small>{warn}</td>')
        body.append(f"<tr><th>{E(repo)}</th>{''.join(tds)}</tr>")
    geo_tds = "".join(f'<td class="num {tone(None if vn == base else geo[vn])}"><b>{"–" if vn == base else fmt_pct(geo[vn])}</b></td>'
                      for vn in vnames)
    body.append(f'<tr class="geo"><th>All repos <small>(geometric mean vs baseline)</small></th>{geo_tds}</tr>')
    vsettings = {v["name"]: " ".join(f"{k}={val}" for k, val in v["settings"].items()) or "defaults" for v in exp["variants"]}
    settings_row = "".join(f'<td class="num settings">{E(vsettings[vn])}</td>' for vn in vnames)
    body.append(f'<tr class="settings"><th>Settings</th>{settings_row}</tr>')
    pivot = f'<div class="scroll"><table class="pivot"><thead><tr><th>Repo</th>{head}</tr></thead><tbody>{"".join(body)}</tbody></table></div>'

    # verdict
    ranked = sorted((g, vn) for vn, g in geo.items() if g is not None and vn != base)
    lines = []
    if ranked:
        g, vn = ranked[0]
        over = f"geometric mean over {len(repos)} repo{'s' if len(repos) != 1 else ''}"
        if g < 0:
            lines.append(f"Fastest overall: <b>{E(vn)}</b>, {fmt_pct(g)} vs {E(base)} ({over}).")
        else:
            lines.append(f"The baseline <b>{E(base)}</b> is fastest; the closest variant is <b>{E(vn)}</b> at {fmt_pct(g)} ({over}).")
        g2, vn2 = ranked[-1]
        if vn2 != vn:
            lines.append(f"Slowest: <b>{E(vn2)}</b>, {fmt_pct(g2)}.")
    fewer = sorted({vn for (vn, r), s in stats.items() if s["tests"] is not None and stats[(base, r)]["tests"] is not None
                    and s["tests"] < stats[(base, r)]["tests"]})
    if fewer:
        lines.append(f"Ran fewer tests than the baseline: {E(', '.join(fewer))}. Expected for a build-cache hit "
                     f"(skipped modules don't re-run tests); anywhere else it means the flag broke something.")
    nfail = sum(s["n"] - s["ok"] for s in stats.values())
    if nfail:
        lines.append(f'<span class="badtext">{nfail} build(s) failed or have no result; their cells exclude them.</span>')
    reps = max((s["n"] for s in stats.values()), default=0)
    spread = [100 * (s["hi"] - s["lo"]) / s["build"] for s in stats.values() if s["build"] and s["hi"] is not None and s["ok"] > 1]
    if spread:
        lines.append(f"Run-to-run spread: median {statistics.median(spread):.0f}% of the build time (max {max(spread):.0f}%) "
                     f"over {reps} reps. Differences smaller than that are noise.")

    # detail table
    det = []
    for vn in vnames:
        for repo in repos:
            s = stats[(vn, repo)]
            if not s["n"]:
                continue
            rng = "" if s["lo"] is None or s["ok"] < 2 else f'{s["lo"]:.1f}–{s["hi"]:.1f}'
            det.append("<tr>" + "".join([
                f"<td>{E(vn)}</td>", f"<td>{E(repo)}</td>", f'<td class="num">{s["ok"]}/{s["n"]}</td>',
                f'<td class="num">{fmt_s(s["build"])}</td>', f'<td class="num">{rng}</td>',
                num_td(s["cores"], "{:.2f}"),
                num_td(s["thr"], "{:.0f}%"),
                num_td(s["tests"], "{:.0f}"),
                num_td(s["hits"], "{:.0f}"),
                num_td(s["dl"], "{:.1f}"),
            ]) + "</tr>")
            csv_rows.append({"experiment": exp["id"], "variant": vn, "repo": repo, "runs_ok": s["ok"], "runs": s["n"],
                             "median_build_s": s["build"], "min_build_s": s["lo"], "max_build_s": s["hi"],
                             "vs_baseline_pct": None if vn == base else pct(s["build"], stats[(base, repo)]["build"]),
                             "avg_cores": s["cores"], "throttled_pct": s["thr"], "tests": s["tests"],
                             "cache_hits": s["hits"], "downloaded_mb": s["dl"]})
    detail = ('<details><summary>Detail per variant and repo</summary><div class="scroll"><table class="detail"><thead><tr>'
              '<th>Variant</th><th>Repo</th><th class="num">OK runs</th><th class="num">Median</th><th class="num">Range (s)</th>'
              '<th class="num">Avg cores</th><th class="num">Throttled</th><th class="num">Tests</th>'
              '<th class="num">Cache hits</th><th class="num">Downloaded (MB)</th></tr></thead><tbody>'
              + "".join(det) + "</tbody></table></div></details>")

    sections.append(f"""<section id="{E(exp['id'])}">
<h2>{E(exp['id'])} <span class="tag">{E(exp['order'])} order</span></h2>
<table class="obj"><tr><th>Objective</th><td>{E(exp['objective'])}</td></tr>
<tr><th>Expected</th><td>{E(exp['expect'])}</td></tr>
<tr><th>Measured</th><td>{'<br>'.join(lines) or 'No comparable results.'}</td></tr></table>
{pivot}
{detail}
</section>""")

# ---- session facts and cost
st = meta.get("settings", {})
# phases.env is appended to by every chunk of a session (same --run-id), so take the first start and the
# last end rather than one value per key.
starts, ends = [], []
pf = os.path.join(session_dir, "phases.env")
if os.path.exists(pf):
    for line in open(pf):
        k, _, v = line.strip().partition("=")
        if v.isdigit():
            (starts if k.endswith("_start") else ends if k.endswith("_end") else []).append(int(v))
hours = (max(ends) - min(starts)) / 3600 if starts and ends and max(ends) > min(starts) else None
cost = ""
if hours is not None and not meta.get("local"):
    p_eks, p_node = float(st.get("PRICE_EKS_HOUR", 0.10)), float(st.get("PRICE_NODE_HOUR", 0.07))
    n = int(st.get("NODE_COUNT", 1) or 1)
    usd = hours * (p_eks + n * (p_node + float(st.get("PRICE_IPV4_HOUR", 0.005))))
    cost = f"<li>Cluster up to down: {hours:.2f} h, about <b>${usd:.2f}</b> (control plane ${p_eks}/h + {n} node at ${p_node}/h; EBS extra, cents)</li>"
where = "local Docker" if meta.get("local") else f"EKS {E(st.get('AWS_REGION', ''))}, {E(st.get('INSTANCE_TYPES', ''))} spot"
ok_runs = sum(1 for r in meta.get("runs", []) if r.get("ok"))
toc = " · ".join(f'<a href="#{E(e["id"])}">{E(e["id"])}</a>' for e in meta["experiments"])

page = f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Experiment results</title>
<style>
:root{{--bg:#fbfbfa;--fg:#1d1d1b;--muted:#6b6b66;--line:#e3e2de;--card:#fff;--head:#f3f2ef;
--good:#e3f2e6;--good2:#c5e6cc;--bad:#fbe9e3;--bad2:#f5cfc2;--flat:transparent;--warn:#a15c00;--accent:#2f5d8a}}
@media (prefers-color-scheme:dark){{:root:not([data-theme="light"]){{--bg:#171716;--fg:#e9e8e4;--muted:#a3a29c;--line:#34332f;
--card:#1f1f1d;--head:#262624;--good:#1f3a26;--good2:#25502f;--bad:#4a2620;--bad2:#663026;--warn:#e7a54a;--accent:#8db6e0}}}}
:root[data-theme="dark"]{{--bg:#171716;--fg:#e9e8e4;--muted:#a3a29c;--line:#34332f;--card:#1f1f1d;--head:#262624;
--good:#1f3a26;--good2:#25502f;--bad:#4a2620;--bad2:#663026;--warn:#e7a54a;--accent:#8db6e0}}
body{{background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif;margin:0;padding:24px 16px 64px}}
main{{max-width:1100px;margin:0 auto}} h1{{font-size:26px;margin:0 0 4px}} h2{{font-size:20px;margin:40px 0 12px}}
a{{color:var(--accent)}} .muted,small{{color:var(--muted)}} small{{font-size:12px}}
.tag{{font-size:11px;font-weight:500;color:var(--muted);border:1px solid var(--line);border-radius:10px;padding:1px 7px;vertical-align:middle}}
ul.facts{{padding-left:18px;color:var(--muted)}}
table{{border-collapse:collapse;background:var(--card);width:100%}} th,td{{border:1px solid var(--line);padding:7px 10px;text-align:left;vertical-align:top}}
thead th,.obj th{{background:var(--head);font-weight:600}} .obj th{{width:110px}} .obj{{margin-bottom:14px}}
.num{{text-align:right;font-variant-numeric:tabular-nums}} .scroll{{overflow-x:auto}}
.good{{background:var(--good)}} .good2{{background:var(--good2)}} .bad{{background:var(--bad)}} .bad2{{background:var(--bad2)}}
.warn{{display:block;font-size:11px;color:var(--warn)}} .badtext{{color:var(--warn)}}
tr.geo th,tr.geo td{{border-top:2px solid var(--fg)}} tr.settings td{{font:11px ui-monospace,monospace;color:var(--muted);text-align:left}}
details{{margin-top:10px}} summary{{cursor:pointer;color:var(--accent)}} .detail td{{font-size:13px}}
.legend span{{display:inline-block;padding:1px 8px;margin-right:6px;border-radius:3px}}
</style></head><body><main>
<h1>Experiment results</h1>
<p class="muted">Session {E(meta['session'])} · {where} · {meta.get('reps')} reps · {ok_runs}/{len(meta.get('runs', []))} runs completed</p>
<ul class="facts"><li>Started {E(meta.get('started', ''))}, finished {E(meta.get('finished', 'not finished'))}</li>{cost}
<li>Every cell is the <b>median build time</b> over the reps (Maven/Gradle command only: no clone, no pod start), with the change against the experiment's baseline variant.</li>
<li>Baseline spec unless a variant changes it: {E(st.get('BUILD_CPU', ''))} CPU / {E(st.get('BUILD_MEM', ''))} pod (request = limit), DEP_CACHE={E(st.get('DEP_CACHE', ''))}, one build at a time.</li></ul>
<p class="legend"><span class="good2">≥10% faster</span><span class="good">3–10% faster</span><span class="bad">3–10% slower</span><span class="bad2">≥10% slower</span></p>
<p>{toc}</p>
{''.join(sections)}
</main></body></html>"""

open(os.path.join(session_dir, "comparison.html"), "w").write(page)
with open(os.path.join(session_dir, "comparison.csv"), "w", newline="") as fh:
    w = csv.DictWriter(fh, fieldnames=list(csv_rows[0]) if csv_rows else ["experiment"])
    w.writeheader()
    w.writerows(csv_rows)
print(os.path.join(session_dir, "comparison.html"))
