"""Replay logged /scope requests (COMPILER_REQUEST_LOG) against the service and list the identifiers it would take back, with
the line they end, to eyeball false positives without running the model.

Usage: python replay_scope.py <requests.jsonl> [--base http://127.0.0.1:8773]
"""
import argparse, collections, json

import compiler_client as cc


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("log")
    ap.add_argument("--base", default=cc.BASE)
    a = ap.parse_args()
    reqs = [r for r in map(json.loads, open(a.log)) if r["endpoint"] == "scope"]
    seen, verdicts = set(), collections.Counter()
    for r in reqs:
        key = (r["uri"], r["offset"], r["text"][r["offset"] - 200:r["offset"]])
        if key in seen:
            continue
        seen.add(key)
        c = cc._post("scope", r["uri"], r["text"], r["offset"], base=a.base)
        verdicts[c["status"]] += 1
        if c["status"] == "unresolved":
            line = r["text"][:r["offset"]].rsplit("\n", 1)[-1]
            print(f"{r['uri'].rsplit('/', 1)[-1]:28} {c['qualifier']:24} | {line.strip()[-90:]}")
    print(dict(verdicts))


if __name__ == "__main__":
    main()
