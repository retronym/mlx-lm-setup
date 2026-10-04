"""Text handling for the speech backend (stdlib only, so it is unit-testable without a model)."""
import re

SENTENCE = re.compile(r"(?<=[.!?…])[\"')\]]*\s+|\n+")
NAME_OK = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,80}$")


def groups(text: str, max_chars: int) -> list[tuple[str, bool]]:
    """Split into (group text, starts_new_paragraph). Sentences are packed up to max_chars; a long sentence stands alone."""
    out: list[tuple[str, bool]] = []
    for pi, para in enumerate(re.split(r"\n\s*\n", text.strip())):
        cur = ""
        first = True
        for sent in (s.strip() for s in SENTENCE.split(para)):
            if not sent:
                continue
            if cur and len(cur) + 1 + len(sent) > max_chars:
                out.append((cur, pi > 0 and first)); first = False; cur = sent
            else:
                cur = f"{cur} {sent}".strip()
        if cur:
            out.append((cur, pi > 0 and first))
    return out
