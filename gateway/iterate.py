"""`iterate`: ask a local LLM, check the answer against gates, and on failure feed the failures back and retry.

The loop is pure (the chat and entail calls are injected) so it is testable without a gateway. Keeps work on free local models;
when the attempts run out the caller gets the best-effort text plus exactly which gates still fail, to take over itself.
"""
from __future__ import annotations

from typing import Awaitable, Callable

from .gates import EntailFn, Gate, run_gate

MAX_ATTEMPTS_CAP = 6
# (model override or None for the default, messages) -> (text, info)
ChatFn = Callable[[str | None, list[dict]], Awaitable[tuple[str, dict]]]


async def check_all(gates: list[Gate], text: str, entail: EntailFn | None) -> list[dict]:
    """Cheap gates first; the NLI gate (which may start a model) only runs once the cheap ones pass."""
    results = []
    for g in sorted(gates, key=lambda g: g.expensive):
        if g.expensive and any(not r["ok"] for r in results):
            results.append({"gate": g.type, "ok": None, "message": "skipped until the other gates pass"})
            continue
        ok, msg = await run_gate(g, text, entail)
        results.append({"gate": g.type, "ok": ok, "message": msg})
    return results


def feedback(results: list[dict]) -> str:
    problems = "\n".join(f"- {r['message']}" for r in results if r["ok"] is False)
    return ("Your previous answer failed these checks:\n" + problems +
            "\nReply again with only the corrected answer, no apology or commentary.")


async def run_iterate(chat: ChatFn, entail: EntailFn | None, gates: list[Gate], messages: list[dict],
                      max_attempts: int = 3, escalate_to: str | None = None) -> dict:
    """Up to `max_attempts` tries; the last one uses `escalate_to` (a bigger or different model) if given and attempts > 1."""
    attempts: list[dict] = []
    convo = list(messages)
    text, results = "", []
    for n in range(1, max_attempts + 1):
        model = escalate_to if escalate_to and n == max_attempts and max_attempts > 1 else None
        text, info = await chat(model, convo)
        results = await check_all(gates, text, entail)
        passed = all(r["ok"] for r in results)
        attempts.append({"attempt": n, "model": info.get("model"), "secs": info.get("secs"), "passed": passed,
                         "failures": [r["message"] for r in results if r["ok"] is False]})
        if passed:
            break
        convo = [*messages, {"role": "assistant", "content": text}, {"role": "user", "content": feedback(results)}]
    passed = bool(attempts) and attempts[-1]["passed"]
    return {"passed": passed, "text": text, "attempts": attempts,
            "gates": [{"gate": r["gate"], "ok": r["ok"]} for r in results],
            **({} if passed else {"note": "gates still failing after all attempts; the text is the last try, not verified. "
                                          "Fix it yourself or relax the gates."})}
