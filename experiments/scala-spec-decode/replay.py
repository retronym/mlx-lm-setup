"""Replay logged compiler requests (COMPILER_REQUEST_LOG) against a service, in order, timing each one; for reproducing a
back end's failure or measuring it on the decoder's real request stream.

Usage: python replay.py <requests.jsonl>... [--base URL] [--uri X.scala] [--limit N]
"""
import argparse, json, time, urllib.error

import compiler_client as cc


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("logs", nargs="+")
    ap.add_argument("--base", default="http://127.0.0.1:8774")
    ap.add_argument("--uri", help="only this file (the basename of the uri)")
    ap.add_argument("--limit", type=int, default=0)
    a = ap.parse_args()
    reqs = [r for log in a.logs for r in map(json.loads, open(log))]
    if a.uri:
        reqs = [r for r in reqs if r["uri"].endswith("/" + a.uri)]
    if a.limit:
        reqs = reqs[:a.limit]
    print(f"{len(reqs)} requests", flush=True)
    for k, r in enumerate(reqs):
        t0 = time.time()
        try:
            c = cc._post(r["endpoint"], r["uri"], r["text"], r["offset"], base=a.base)
            status = c.get("status", f"{len(c.get('errors', []))} errors")
        except urllib.error.HTTPError as e:
            status = f"HTTP {e.code}: {e.read()[:200]!r}"
        except (urllib.error.URLError, TimeoutError) as e:
            status = f"failed: {e}"
        print(f"{k:5} {r['endpoint']:8} {r['uri'].rsplit('/', 1)[-1]:28} @{r['offset']:6} {status:14} {(time.time() - t0) * 1000:7.0f} ms", flush=True)
        if status.startswith(("HTTP 500", "failed")):
            break


if __name__ == "__main__":
    main()
