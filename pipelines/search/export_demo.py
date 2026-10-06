#!/usr/bin/env python3
"""Export the search dashboard as one self-contained HTML file: a snapshot to share while the real thing needs a gateway and local models.

It asks a running gateway for the dashboard facts over each range preset and for the model labels (kind, risk) of every open PR and new issue,
then writes gateway/web/search.html with its two scripts inlined and a small shim in front of the page's own script: fetch() answers the dashboard
endpoints from the embedded snapshot (everything else says it is not in the demo), and Date.now() is frozen at the snapshot time so "updated 3 d
ago" stays true. Only the Dashboard tab is shown. The page code is the live page's, unchanged, so the demo cannot drift from it.

    export_demo.py [--gateway http://127.0.0.1:8090] [--out FILE] [--no-judge]
"""
import argparse, json, re, sys, time, urllib.error, urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WEB = ROOT / "gateway" / "web"
DAYS = [7, 30, 90, 365]                                                   # the page's range presets
BATCH = 10                                                                # what the page asks per judge request


def call(base, path, body=None, timeout=600):
    req = urllib.request.Request(base + path, data=None if body is None else json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        raise SystemExit(f"{path}: HTTP {e.code} {e.read().decode()[:300]}")
    except urllib.error.URLError as e:
        raise SystemExit(f"{path}: {e.reason} (is the gateway running? mise run service-status)")


def day(ts, back):
    return time.strftime("%Y-%m-%d", time.gmtime(ts - back * 86400))


def snapshot(base, judge):
    now = time.time()
    snap = {"generated": int(now * 1000), "status": call(base, "/api/search/status"), "dashboards": {}, "judge": {}}
    for n in DAYS:
        t = time.time()
        snap["dashboards"][n] = call(base, f"/api/search/dashboard?since={day(now, n - 1)}")
        d = snap["dashboards"][n]
        print(f"  {n:>3} days: {len(d['open_prs'])} open PRs, {len(d['new_issues'])} new issues, {d['totals'].get('commits', 0)} commits ({time.time() - t:.1f} s)", flush=True)
    if judge:
        want = {}
        for d in snap["dashboards"].values():                              # the same set the page labels: every open PR, the first 15 new issues
            for x in d["open_prs"] + d["new_issues"][:15]:
                want[f"{x['repo']}#{x['number']}"] = {"project": x["project"], "repo": x["repo"], "number": int(x["number"])}
        refs, t0 = list(want.values()), time.time()
        for i in range(0, len(refs), BATCH):
            r = call(base, "/api/search/dashboard/judge", {"refs": refs[i:i + BATCH]})
            snap["judge"].update(r["results"])
            done = min(i + BATCH, len(refs)); el = time.time() - t0
            print(f"  labelled {done} of {len(refs)} ({el:.0f} s, about {el / done * (len(refs) - done):.0f} s left)", flush=True)
    return snap


SHIM = r"""<script>
// ---- demo shim: the dashboard endpoints answered from an embedded snapshot (pipelines/search/export_demo.py) ----
const DEMO = __SNAPSHOT__;
{ const realNow = Date.now; Date.now = () => DEMO.generated; Date.realNow = realNow; }
const json = (o, status = 200) => new Response(JSON.stringify(o), { status, headers: { "Content-Type": "application/json" } });
window.fetch = async (url, opts = {}) => {
  const u = new URL(url, location.href), path = u.pathname;
  if (path === "/api/search/status") return json(DEMO.status);
  if (path === "/api/search/dashboard") {                                          // the snapshot of the smallest preset that covers the asked range
    const days = Math.round((DEMO.generated - Date.parse(u.searchParams.get("since") || "0")) / 864e5) + 1;
    const keys = Object.keys(DEMO.dashboards).map(Number).sort((a, b) => a - b);
    const d = DEMO.dashboards[keys.find(k => k >= days - 1) || keys[keys.length - 1]];
    const only = u.searchParams.getAll("projects");
    if (!only.length) return json(d);
    const mine = x => only.includes(x.project);                                    // project picks filter the lists; the activity series and totals stay as exported
    return json({ ...d, open_prs: d.open_prs.filter(mine), new_issues: d.new_issues.filter(mine), projects: d.projects.filter(p => only.includes(p.id ?? p)) });
  }
  if (path === "/api/search/dashboard/judge") {
    const refs = JSON.parse(opts.body || "{}").refs || [], results = {};
    for (const r of refs) { const v = DEMO.judge[`${r.repo}#${r.number}`]; if (v) results[`${r.repo}#${r.number}`] = v; }
    return json({ results, seconds: 0 });
  }
  return json({ error: { message: "Not available in the static demo" } }, 404);
};
location.hash = "#dashboard";
</script>
"""

CSS = """<style>
/* demo: only the dashboard works without a gateway */
#t-search, #t-duplicates, #t-clusters, #t-outliers, #t-status, header > a, header .logo, label:has(#dash-since), label:has(#dash-until) { display: none !important; }
.demo-banner { font-size: 13px; color: var(--muted); border: 1px solid var(--line); border-radius: 8px; padding: 6px 10px; }
</style>
"""


def build(snap, out):
    html = (WEB / "search.html").read_text()
    for name in re.findall(r'<script src="/vendor/([\w.-]+)"></script>', html):        # inline the vendored libraries
        html = html.replace(f'<script src="/vendor/{name}"></script>', f"<script>{(WEB / 'vendor' / name).read_text()}</script>")
    data = json.dumps(snap, separators=(",", ":")).replace("</", "<\\/").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    first = html.index("<script>")                                                      # the page's own script: the shim goes in front of it
    when = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(snap["generated"] / 1000))
    html = html[:first] + SHIM.replace("__SNAPSHOT__", data) + html[first:]
    html = html.replace("</head>", CSS + "</head>", 1)
    banner = f'<div class="demo-banner">A static snapshot of the Dashboard tab, taken {when}. The live page also searches, asks, and shows duplicates and clusters, over local models.</div>'
    html = html.replace("<main>", "<main>" + banner, 1)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html)
    return len(html)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--gateway", default="http://127.0.0.1:8090")
    ap.add_argument("--out", type=Path, default=Path(__file__).parent / "data" / "demo" / "search-dashboard.html")
    ap.add_argument("--no-judge", action="store_true", help="skip the model labels (the decision model need not be running)")
    a = ap.parse_args()
    print(f"snapshot from {a.gateway}", flush=True)
    snap = snapshot(a.gateway.rstrip("/"), not a.no_judge)
    n = build(snap, a.out)
    print(f"wrote {a.out} ({n / 1024:.0f} KiB, {len(snap['judge'])} labelled items)")


if __name__ == "__main__":
    sys.exit(main())
