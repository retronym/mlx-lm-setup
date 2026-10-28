"""Client for the compiler service (jvm/Service.scala): completions after a `.` in an edited buffer, scope at a bare identifier,
and errors. A failing back end never stops the caller: completions answer status "error" (no intervention), and a check falls
back to the stock service. With COMPILER_REQUEST_LOG set, every request is appended there (JSON lines), for replaying."""
import json, os, time, urllib.error, urllib.request

BASE = os.environ.get("COMPILER_SERVICE", "http://127.0.0.1:8773")      # 8773: the #84 back end; 8771: stock
STOCK = os.environ.get("COMPILER_SERVICE_STOCK", "http://127.0.0.1:8771")
LOG = os.environ.get("COMPILER_REQUEST_LOG")
failures = []


def _post(endpoint: str, uri: str, text: str, offset: int = 0, base=None) -> dict:
    body = {"uri": uri, "text": text, "offset": offset}
    if LOG:
        with open(LOG, "a") as fh:
            fh.write(json.dumps({"endpoint": endpoint, "t": time.time(), **body}) + "\n")
    req = urllib.request.Request(f"{base or BASE}/{endpoint}", json.dumps(body).encode(), {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def _guarded(endpoint, uri, text, offset):
    try:
        return _post(endpoint, uri, text, offset)
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        failures.append({"endpoint": endpoint, "uri": uri, "offset": offset, "error": str(e)})
        return {"status": "error", "qualifier": str(e), "members": [], "typeMs": 0.0, "totalMs": 0.0}


def complete(uri: str, text: str, offset: int) -> dict:
    """{status: ok | guessing | none | error, qualifier, members: [{name, kind, paramless}], typeMs, totalMs}; offset is right after the `.`."""
    return _guarded("complete", uri, text, offset)


def members(uri: str, text: str, offset: int) -> dict:
    """Members of the type of the expression that ends at offset."""
    return _guarded("members", uri, text, offset)


def scope(uri: str, text: str, offset: int) -> dict:
    """At the end of a bare identifier: status ok (resolves) | unresolved (members: every name in scope) | none | error."""
    return _guarded("scope", uri, text, offset)


def check(uri: str, text: str) -> list:
    """Errors after typer: [{id, offset, line, msg}]."""
    try:
        return _post("check", uri, text)["errors"]
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        failures.append({"endpoint": "check", "uri": uri, "error": str(e)})
        return _post("check", uri, text, base=STOCK)["errors"]
