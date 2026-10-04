"""Restricted output gates for `iterate`: JSON (optionally schema-checked), regex, contains, length, and an NLI faithfulness gate.

Deliberately no shell or code-execution gate: a gate can only inspect text. Specs are plain dicts from the caller and are validated
up front (`parse_gates`) so a typo fails before any model runs. Gates return (ok, message); the message is what the model is told
on retry, so it names what to fix.
"""
from __future__ import annotations

import asyncio
import json
import re
import sys
from dataclasses import dataclass
from typing import Awaitable, Callable

import jsonschema

MAX_PATTERN = 500
MAX_TEXT = 200_000
REGEX_TIMEOUT_S = 2.0
MAX_CLAIMS = 20
MIN_CLAIM_WORDS = 3


class GateSpecError(ValueError):
    pass


@dataclass
class Gate:
    type: str
    spec: dict

    @property
    def expensive(self) -> bool:
        return self.type == "nli"


_KEYS = {
    "json": {"type", "schema"},
    "regex": {"type", "pattern", "mode", "ignore_case", "multiline"},
    "contains": {"type", "all", "any", "none", "ignore_case"},
    "length": {"type", "min_chars", "max_chars"},
    "nli": {"type", "source", "min_entailment", "max_contradiction"},
}


def _strs(spec: dict, key: str) -> list[str]:
    v = spec.get(key, [])
    if not isinstance(v, list) or not all(isinstance(x, str) and x for x in v):
        raise GateSpecError(f"{spec['type']} gate: `{key}` must be a list of non-empty strings")
    return v


def parse_gates(raw: list[dict]) -> list[Gate]:
    if not isinstance(raw, list) or not raw:
        raise GateSpecError("`gates` must be a non-empty list")
    gates = []
    for i, spec in enumerate(raw):
        if not isinstance(spec, dict) or spec.get("type") not in _KEYS:
            raise GateSpecError(f"gate {i}: `type` must be one of {sorted(_KEYS)}")
        t = spec["type"]
        extra = set(spec) - _KEYS[t]
        if extra:
            raise GateSpecError(f"gate {i} ({t}): unknown keys {sorted(extra)}; allowed: {sorted(_KEYS[t] - {'type'})}")
        if t == "json" and "schema" in spec:
            if not isinstance(spec["schema"], dict):
                raise GateSpecError("json gate: `schema` must be a JSON Schema object")
            try:
                jsonschema.validators.validator_for(spec["schema"]).check_schema(spec["schema"])
            except jsonschema.SchemaError as e:
                raise GateSpecError(f"json gate: invalid schema: {e.message}") from None
        elif t == "regex":
            p = spec.get("pattern")
            if not isinstance(p, str) or not p or len(p) > MAX_PATTERN:
                raise GateSpecError(f"regex gate: `pattern` must be a string of 1..{MAX_PATTERN} chars")
            if spec.get("mode", "match") not in ("match", "absent"):
                raise GateSpecError("regex gate: `mode` must be 'match' (default) or 'absent'")
            try:
                re.compile(p)
            except re.error as e:
                raise GateSpecError(f"regex gate: invalid pattern: {e}") from None
        elif t == "contains":
            if not any(k in spec for k in ("all", "any", "none")):
                raise GateSpecError("contains gate: give at least one of `all`, `any`, `none`")
            for k in ("all", "any", "none"):
                _strs(spec, k)
        elif t == "length":
            for k in ("min_chars", "max_chars"):
                if k in spec and (isinstance(spec[k], bool) or not isinstance(spec[k], int) or spec[k] < 0):
                    raise GateSpecError(f"length gate: `{k}` must be an integer >= 0")
            if "min_chars" not in spec and "max_chars" not in spec:
                raise GateSpecError("length gate: give `min_chars` and/or `max_chars`")
        elif t == "nli":
            if not isinstance(spec.get("source"), str) or not spec["source"].strip():
                raise GateSpecError("nli gate: `source` (the text the answer must be faithful to) is required")
            for k in ("min_entailment", "max_contradiction"):
                if k in spec and (isinstance(spec[k], bool) or not isinstance(spec[k], (int, float)) or not 0 <= spec[k] <= 1):
                    raise GateSpecError(f"nli gate: `{k}` must be a number in [0, 1]")
        gates.append(Gate(t, spec))
    return gates


# ---- helpers ---------------------------------------------------------------------------------------------------------------
_FENCE = re.compile(r"```[A-Za-z0-9_+-]*\n(.*?)```", re.S)


def extract_json(text: str):
    """Parse `text` as JSON, else the first fenced block that parses, else the first JSON object/array embedded in prose."""
    candidates = [text.strip(), *(m.group(1).strip() for m in _FENCE.finditer(text))]
    for c in candidates:
        try:
            return json.loads(c)
        except json.JSONDecodeError:
            pass
    dec = json.JSONDecoder()
    for i, ch in enumerate(text):
        if ch in "{[":
            try:
                return dec.raw_decode(text, i)[0]
            except json.JSONDecodeError:
                continue
    raise ValueError("no valid JSON found")


async def _regex_search(pattern: str, text: str, ignore_case: bool, multiline: bool) -> bool | None:
    """`re.search` over the whole answer (`^`/`$` anchor the answer, not each line, unless `multiline`). Run in a child process so a catastrophic pattern is killed after REGEX_TIMEOUT_S instead of freezing the gateway (`re`
    holds the GIL, so a thread would not help). Returns None on timeout."""
    code = ("import re,sys,json;a=json.load(sys.stdin);"
            "print(1 if re.search(a['p'],a['t'],(re.I if a['i'] else 0)|(re.M if a['m'] else 0)) else 0)")
    proc = await asyncio.create_subprocess_exec(sys.executable, "-c", code, stdin=asyncio.subprocess.PIPE,
                                                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(json.dumps({"p": pattern, "t": text, "i": ignore_case, "m": multiline}).encode()),
                                        REGEX_TIMEOUT_S)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return None
    return out.strip() == b"1"


def split_claims(text: str) -> list[str]:
    """Sentences of the answer, minus fenced code, for the faithfulness gate."""
    text = _FENCE.sub(" ", text)
    out = []
    for line in re.split(r"\n+", text):
        line = re.sub(r"^\s*(?:[-*+]|\d+[.)])\s+", "", line).strip()
        for s in re.split(r"(?<=[.!?])\s+", line):
            s = s.strip()
            if len(s.split()) >= MIN_CLAIM_WORDS:
                out.append(s)
    return out


EntailFn = Callable[[str, list[str]], Awaitable[list[dict]]]   # (premise, hypotheses) -> [{"entailment": p, "contradiction": p, ...}]


async def run_gate(gate: Gate, text: str, entail: EntailFn | None = None) -> tuple[bool, str]:
    s, t = gate.spec, gate.type
    if len(text) > MAX_TEXT:
        return False, f"output is too long to check ({len(text)} chars)"
    if t == "json":
        try:
            value = extract_json(text)
        except ValueError:
            return False, "the answer is not valid JSON. Reply with only a JSON value."
        if "schema" in s:
            errs = sorted(jsonschema.Draft202012Validator(s["schema"]).iter_errors(value), key=lambda e: list(e.path))
            if errs:
                shown = "; ".join(f"{'/'.join(map(str, e.path)) or '(root)'}: {e.message[:120]}" for e in errs[:5])
                return False, f"the JSON does not match the schema: {shown}"
        return True, "ok"
    if t == "regex":
        hit = await _regex_search(s["pattern"], text, bool(s.get("ignore_case")), bool(s.get("multiline")))
        if hit is None:
            return False, "regex gate timed out (pattern too expensive)"
        want = s.get("mode", "match") == "match"
        if hit == want:
            return True, "ok"
        return False, (f"the answer must match /{s['pattern']}/" if want else f"the answer must not match /{s['pattern']}/")
    if t == "contains":
        hay = text.lower() if s.get("ignore_case") else text
        norm = (lambda x: x.lower()) if s.get("ignore_case") else (lambda x: x)
        problems = []
        if missing := [x for x in s.get("all", []) if norm(x) not in hay]:
            problems.append(f"missing required text: {missing}")
        if s.get("any") and not any(norm(x) in hay for x in s["any"]):
            problems.append(f"must contain at least one of: {s['any']}")
        if present := [x for x in s.get("none", []) if norm(x) in hay]:
            problems.append(f"must not contain: {present}")
        return (False, "; ".join(problems)) if problems else (True, "ok")
    if t == "length":
        n = len(text.strip())
        if n < s.get("min_chars", 0):
            return False, f"the answer is too short ({n} chars, need at least {s['min_chars']})"
        if n > s.get("max_chars", float("inf")):
            return False, f"the answer is too long ({n} chars, at most {s['max_chars']})"
        return True, "ok"
    if t == "nli":
        if entail is None:
            return False, "nli gate unavailable"
        claims = split_claims(text)[:MAX_CLAIMS]
        if not claims:
            return True, "ok (no checkable sentences)"
        probs = await entail(s["source"], claims)
        min_e, max_c = s.get("min_entailment", 0.5), s.get("max_contradiction", 0.5)
        bad = []
        for claim, p in zip(claims, probs):
            if p["contradiction"] >= max_c:
                bad.append(f"contradicted by the source ({p['contradiction']:.2f}): \"{claim[:160]}\"")
            elif p["entailment"] < min_e:
                bad.append(f"not supported by the source ({p['entailment']:.2f}): \"{claim[:160]}\"")
        if bad:
            return False, "these statements are not faithful to the source text. Remove or fix them: " + " | ".join(bad[:5])
        return True, "ok"
    raise AssertionError(t)
