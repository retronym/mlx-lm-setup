"""Client for the compiler service (jvm/Service.scala): completions after a `.` in an edited buffer."""
import json, os, urllib.request

BASE = os.environ.get("COMPILER_SERVICE", "http://127.0.0.1:8773")      # 8773: the #84 back end; 8771: stock


def _post(endpoint: str, uri: str, text: str, offset: int = 0) -> dict:
    req = urllib.request.Request(f"{BASE}/{endpoint}", json.dumps({"uri": uri, "text": text, "offset": offset}).encode(),
                                 {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.load(r)


def complete(uri: str, text: str, offset: int) -> dict:
    """{status: ok | guessing | none, qualifier, members: [{name, kind, paramless}], typeMs, totalMs}; offset is right after the `.`."""
    return _post("complete", uri, text, offset)


def members(uri: str, text: str, offset: int) -> dict:
    """Members of the type of the expression that ends at offset."""
    return _post("members", uri, text, offset)


def check(uri: str, text: str) -> list:
    """Errors after typer: [{id, offset, line, msg}]."""
    return _post("check", uri, text)["errors"]
