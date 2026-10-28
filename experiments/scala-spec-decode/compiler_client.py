"""Client for the compiler service (jvm/Service.scala): completions after a `.` in an edited buffer."""
import json, os, urllib.request

BASE = os.environ.get("COMPILER_SERVICE", "http://127.0.0.1:8771")


def _post(endpoint: str, uri: str, text: str, offset: int) -> dict:
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
