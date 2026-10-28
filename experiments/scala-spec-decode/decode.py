"""Phase 2: Qwen3-Coder decoding (mlx-lm, greedy) with the Scala compiler's completions.

At a selection (the model's next token starts with `.` after an expression), the compiler service lists the members of the
qualifier's type. Those names, as `.name` strings, make a trie that is used

- as a mask (lossy): logits are restricted to tokens that keep the text on a member name; a name ends where the next character
  is not an identifier character (`(`, `[`, `.`, space, newline...), so tokens straddling the end of a name are allowed;
- as a draft (lossless under greedy decoding): once one name is left in the trie, its remaining characters are tokenised and
  verified in one forward pass; the KV cache is trimmed back over the rejected tokens.

Nothing is constrained where the compiler is guessing: the service answers "guessing" for an error-typed or Dynamic qualifier.

The loop emits events (prompt, token, trigger, mask, draft, done) to a callback, which the eval and the demo page consume.
"""
import bisect, os, re, sys, time

import numpy as np
import mlx.core as mx
from mlx_lm import load
from mlx_lm.models.cache import make_prompt_cache, trim_prompt_cache

import compiler_client
from phase0b import prefix_for

MODEL = "mlx-community/Qwen3-Coder-30B-A3B-Instruct-4bit"
IDENT = re.compile(r"[A-Za-z0-9_$]")
NAME = re.compile(r"^[A-Za-z_$][A-Za-z0-9_$]*$")
MEMBER_START = re.compile(r"(def|val|var|lazy|override|private|protected|final|inline|given|@|class|object|trait|case class|end\b|/\*\*|//)")


def acquire_memory(need_gb=22.0, wait_s=600):
    """Out-of-gateway model use, as the search refresh does it: stop idle gateway LLMs, then wait for free memory."""
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "pipelines"))
    import gateway_client
    if not os.environ.get("GATEWAY_TOKEN"):
        for p in (os.path.expanduser("~/code/mlx-lm-setup/.gateway/token"),):
            if os.path.exists(p):
                os.environ["GATEWAY_TOKEN"] = open(p).read().strip()
    stopped = gateway_client.release("llm")
    t0 = time.time()
    while (gateway_client.available_gb() or need_gb) < need_gb and time.time() - t0 < wait_s:
        time.sleep(10)
    return stopped, gateway_client.available_gb()


class Vocab:
    """Decoded token strings, indexed for the trie: exact lookup, prefix ranges, and 'starts with a non-identifier char'."""

    def __init__(self, tok, size):
        self.strs = [tok.decode([i]) for i in range(size)]
        self.by_str = {}
        for i, s in enumerate(self.strs):
            if s and i not in tok.all_special_ids:
                self.by_str.setdefault(s, []).append(i)
        self.sorted = sorted((s, i) for s, ids in self.by_str.items() for i in ids)
        self.keys = [s for s, _ in self.sorted]
        self.nonident_start = np.array([bool(s) and i not in tok.all_special_ids and not IDENT.match(s[0]) for i, s in enumerate(self.strs)])

    def starting_with(self, p):
        lo = bisect.bisect_left(self.keys, p)
        hi = bisect.bisect_left(self.keys, p + "\U0010ffff")
        return self.sorted[lo:hi]


class NameTrie:
    """The strings a name may be written as (`.name` for a member, `<lead>name` for a scope name after a backtrack), and how far
    the text has got into them (`e`)."""

    def __init__(self, strings):
        self.F = sorted(set(strings))
        self.e = ""

    def live(self):
        lo = bisect.bisect_left(self.F, self.e)
        hi = bisect.bisect_left(self.F, self.e + "\U0010ffff")
        return self.F[lo:hi]

    def allowed(self, vocab: Vocab) -> np.ndarray:
        m = np.zeros(len(vocab.strs), dtype=bool)
        for f in self.live():
            r = f[len(self.e):]
            if not r:                                          # the name is complete: anything that ends it
                m |= vocab.nonident_start
                continue
            for k in range(1, len(r) + 1):
                for i in vocab.by_str.get(r[:k], ()):
                    m[i] = True
            for s, i in vocab.starting_with(r):                 # straddles the end of the name: `name(`, `name.`, `name `
                if len(s) > len(r) and not IDENT.match(s[len(r)]):
                    m[i] = True
        return m

    def advance(self, t: str) -> bool:
        """Consume a token; False once the text has left every name (past its end, or off the list)."""
        self.e += t
        return bool(self.live())

    def unique_rest(self):
        live = self.live()
        return live[0][len(self.e):] if len(live) == 1 else None


def closers(text: str) -> str:
    """Closing brackets for what the half-written body leaves open. Without them the rest of the file mis-parses and typing the
    recovered trees costs ~3 s instead of ~0.4 s on a 1.2k-line scala3 file."""
    stack, i, n = [], 0, len(text)
    while i < n:
        c = text[i]
        if text.startswith("//", i):
            i = text.find("\n", i)
            i = n if i < 0 else i
            continue
        if text.startswith("/*", i):
            j = text.find("*/", i + 2)
            i = n if j < 0 else j + 2
            continue
        if c == '"':
            if text.startswith('"""', i):
                j = text.find('"""', i + 3)
                i = n if j < 0 else j + 3
                continue
            j = i + 1
            while j < n and text[j] not in '"\n':
                j += 2 if text[j] == "\\" else 1
            i = j + 1
            continue
        if c in "([{":
            stack.append({"(": ")", "[": "]", "{": "}"}[c])
        elif c in ")]}" and stack and stack[-1] == c:
            stack.pop()
        i += 1
    return "".join(reversed(stack))


KEYWORDS = set("""abstract case catch class def do else enum export extends false final finally for forSome given if implicit import
lazy match new null object override package private protected return sealed super then this throw trait true try type val var while
with yield end inline opaque open transparent using derives infix as _""".split())
BINDERS = {"val", "var", "def", "given", "type", "class", "object", "trait", "enum", "case", "import", "export", "package", "for", "end"}
IDENT_RUN = re.compile(r"(?<![A-Za-z0-9_$.])[A-Za-z_][A-Za-z0-9_$]*")


def pending_idents(text: str, body_from: int, checked: set):
    """Bare identifiers that have ended and whose role is now decidable: yields (start, end, verdict), verdict "check" for a
    reference the compiler should resolve, "skip" for a binder or a non-reference, and stops at the first one that needs more
    text. A lambda parameter (`x =>`, `(a, b) =>`), a pattern binder, a definition's name, a named argument and a `for` binder
    are binders; forward references to local defs written later cannot be told apart and are checked (and counted)."""
    for m in IDENT_RUN.finditer(text, max(body_from, len(text) - 200)):
        s, e = m.span()
        if s in checked or e >= len(text):
            continue
        name = m.group(0)
        line_start = text.rfind("\n", 0, s) + 1
        line = text[line_start:s]
        rest = text[e:]
        stripped = rest.lstrip(" \t")
        if "\n" in rest[:len(rest) - len(stripped)] or len(stripped) >= 2:
            pass
        else:
            return                                              # need to see what follows
        if name in KEYWORDS or "//" in line or line.count('"') % 2 == 1:
            yield s, e, "skip"
            continue
        before = text[line_start:s].rstrip()
        prev_word = re.search(r"([A-Za-z_][A-Za-z0-9_$]*)\s*$", before)
        if prev_word and prev_word.group(1) in BINDERS:
            yield s, e, "skip"
            continue
        last_case = max((m2.end() for m2 in re.finditer(r"\bcase\b", text[line_start:s])), default=-1)
        if last_case >= 0 and "=>" not in text[line_start + last_case:s]:
            yield s, e, "skip"                              # between `case` and its `=>`: a pattern
            continue
        if stripped.startswith(("=>", "<-", "@")) or (stripped.startswith(":") and not stripped.startswith("::")) \
                or (stripped.startswith("=") and not stripped.startswith(("==", "=>"))):
            yield s, e, "skip"
            continue
        if before.endswith(("(", ",")) and stripped[:1] in (",", ")"):
            close = matching_close(text, s)                  # `(a, b) =>` or arguments? decided after the `)`
            if close is None:
                return
            after = text[close + 1:].lstrip(" \t")
            if len(after) < 2:
                return
            if after.startswith("=>"):
                yield s, e, "skip"
                continue
        yield s, e, "check"


def matching_close(text: str, s: int):
    depth = 0
    for i in range(s, len(text)):
        if text[i] in "([{":
            depth += 1
        elif text[i] in ")]}":
            if depth == 0:
                return i
            depth -= 1
    return None


def selection_context(text: str) -> bool:
    """Is a `.` here a selection on an expression (not a comment, string, number or `..`)?"""
    line = text[text.rfind("\n") + 1:]
    if "//" in line or line.count('"') % 2 == 1:
        return False
    t = text.rstrip(" \t\n")
    if not t:
        return False
    c = t[-1]
    if c in ")]}`\"":
        return True
    if not IDENT.match(c):
        return False
    word = re.search(r"[A-Za-z0-9_$]+$", t).group(0)
    return not word.isdigit()


class Decoder:
    def __init__(self, model_name=MODEL):
        self.model, self.tok = load(model_name)
        size = self.model.args.vocab_size if hasattr(self.model, "args") else len(self.tok.get_vocab())
        self.vocab = Vocab(self.tok, size)
        self.eos = set(self.tok.eos_token_ids) | {self.tok.convert_tokens_to_ids("<|im_end|>")}

    # ---- prompt -------------------------------------------------------------------------------------------------------
    def prompt(self, task, feedback=None):
        """Chat prompt as in phase 0b, with the assistant turn pre-filled up to the `=` of the def, so the model writes the body."""
        src = task["src"]
        msg = (f"```scala\n{prefix_for(src, task)}\n```\n\nThis Scala 3 file from the Scala 3 compiler (dotty) stops right after the `=` of method `{task['name']}`. "
               "Write only the body of that method (the expression after `=`), indented as it would be in the file. "
               "Reply with a single scala code block and nothing else.")
        messages = [{"role": "user", "content": msg}]
        if feedback:
            body, errors = feedback
            messages += [{"role": "assistant", "content": f"```scala\n{task['header']}{body}\n```"},
                         {"role": "user", "content": "That body does not compile:\n" + "\n".join(f"- {e}" for e in errors) +
                          "\nWrite the corrected body. Reply with a single scala code block and nothing else."}]
        p = self.tok.apply_chat_template(messages, add_generation_prompt=True, tokenize=False)
        return p + "```scala\n" + task["header"]

    # ---- generation ---------------------------------------------------------------------------------------------------
    def generate(self, task, mask=False, draft=False, idents=False, max_tokens=600, on_event=lambda e: None, feedback=None):
        """Greedy decode of one body. Returns (body, stats). With `idents`, each bare identifier that ends in a reference
        position is resolved by the compiler; an unresolved one is taken back (KV cache trimmed to the token where it starts)
        and written again under a mask of the names in scope."""
        src, uri = task["src"], task["uri"]
        before, after = src[:task["eq"] + 1], src[task["rhsEnd"]:]
        use_trie = mask or draft
        st = dict(tokens=0, steps=0, triggers=0, guessing=0, none=0, compiler_ms=0.0, interventions=0, drafted=0, accepted=0,
                  hit_max=False, decode_s=0.0, ident_checks=0, backtracks=0, scope_ms=0.0)
        text, trie, forced = "", None, False                  # forced: the trie came from a backtrack, so it masks
        ids, starts, checked, rejected = [], [], set(), []
        prompt_ids = self.tok.encode(self.prompt(task, feedback), add_special_tokens=False)
        on_event({"type": "prompt", "tokens": len(prompt_ids), "header": task["header"]})
        cache = make_prompt_cache(self.model)
        t0 = time.time()
        for i in range(0, len(prompt_ids) - 1, 2048):           # prefill in chunks
            self.model(mx.array([prompt_ids[i:min(i + 2048, len(prompt_ids) - 1)]]), cache=cache)
            mx.eval([c.state for c in cache])
        st["prefill_s"] = time.time() - t0
        t0 = time.time()

        def choose(logits):
            nonlocal trie
            raw = int(mx.argmax(logits).item())
            if use_trie and trie is None:
                s = self.vocab.strs[raw]
                if s.startswith(".") and not s.startswith("..") and (len(s) == 1 or IDENT.match(s[1])) and selection_context(text):
                    buf = before + text + "."
                    c = compiler_client.complete(uri, buf + "\n" + "\n".join(closers(text)) + "\n" + after, len(buf))
                    st["triggers"] += 1
                    st["compiler_ms"] += c["totalMs"]
                    names = [m["name"] for m in c["members"] if NAME.match(m["name"])]
                    on_event({"type": "trigger", "at": len(text), "status": c["status"], "qualifier": c["qualifier"],
                              "members": len(names), "ms": c["totalMs"], "sample": names[:40]})
                    if c["status"] == "ok" and names:
                        trie = NameTrie("." + n for n in names)
                    else:
                        st[c["status"]] = st.get(c["status"], 0) + 1
            if (mask or forced) and trie is not None:
                m = trie.allowed(self.vocab)
                if m.any():
                    choice = int(mx.argmax(mx.where(mx.array(m), logits, -mx.inf)).item())
                    if choice != raw:
                        st["interventions"] += 1
                        on_event({"type": "mask", "at": len(text), "raw": self.vocab.strs[raw], "chosen": self.vocab.strs[choice],
                                  "live": len(trie.live())})
                    return choice
            return raw

        def accept(y, drafted=False):
            nonlocal text, trie, forced
            s = self.vocab.strs[y]
            ids.append(y)
            starts.append(len(text))
            text += s
            st["tokens"] += 1
            if trie is not None and not trie.advance(s):
                trie, forced = None, False
            on_event({"type": "token", "text": s, "drafted": drafted, "constrained": trie is not None})

        def check_idents():
            """Resolve the bare identifiers that have ended; on the first unresolved one, rewind and return the token to
            accept in its place (chosen under the scope mask), else None. The cache holds every accepted token but the last."""
            nonlocal text, trie, forced
            for s0, e0, verdict in list(pending_idents(text, 0, checked)):
                checked.add(s0)
                if verdict != "check":
                    continue
                buf = before + text[:e0]
                c = compiler_client.scope(uri, buf + "\n" + "\n".join(closers(text[:e0])) + "\n" + after, len(buf))
                st["ident_checks"] += 1
                st["scope_ms"] += c["totalMs"]
                st["compiler_ms"] += c["totalMs"]
                if c["status"] != "unresolved":
                    continue
                names = [m["name"] for m in c["members"] if NAME.match(m["name"]) and "$" not in m["name"]]
                if not names:
                    continue
                k = bisect.bisect_right(starts, s0) - 1          # the token the identifier starts in
                lead = text[starts[k]:s0]
                n = len(ids)
                trim_prompt_cache(cache, n - k)
                seq_k = ids[k - 1] if k > 0 else prompt_ids[-1]
                cut = starts[k]
                dropped = text[cut:]
                rejected.append({"name": c["qualifier"], "at": s0})
                del ids[k:], starts[k:]
                text = text[:cut]
                st["backtracks"] += 1
                st["tokens"] -= n - k
                on_event({"type": "backtrack", "name": c["qualifier"], "drop_tokens": n - k, "dropped": dropped, "scope": len(names),
                          "ms": c["totalMs"]})
                trie, forced = NameTrie(lead + nm for nm in names), True
                logits = self.model(mx.array([[seq_k]]), cache=cache)[0, -1]
                return choose_masked(logits)
            return None

        def choose_masked(logits):
            m = trie.allowed(self.vocab)
            return int(mx.argmax(mx.where(mx.array(m), logits, -mx.inf)).item()) if m.any() else int(mx.argmax(logits).item())

        y = prompt_ids[-1]
        logits = self.model(mx.array([[y]]), cache=cache)[0, -1]
        y = choose(logits)
        while True:
            if y in self.eos:
                break
            accept(y)
            body, stop = cut_body(text, task["indent"])
            if stop or st["tokens"] >= max_tokens or looping(text):
                st["hit_max"] = not stop
                break
            if idents and trie is None:
                back = check_idents()
                if back is not None:
                    y = back
                    continue
            d = []
            if draft and trie is not None:
                rest = trie.unique_rest()
                if rest:
                    d = self.tok.encode(rest, add_special_tokens=False)
            L = self.model(mx.array([[y] + d]), cache=cache)[0]
            st["steps"] += 1
            if d:
                st["drafted"] += len(d)
                on_event({"type": "draft", "at": len(text), "tokens": [self.vocab.strs[t] for t in d]})
            j = 0
            for j in range(len(d) + 1):
                nxt = choose(L[j])
                if j < len(d) and nxt == d[j] and nxt not in self.eos:
                    st["accepted"] += 1
                    accept(nxt, drafted=True)
                    continue
                y = nxt
                break
            if d and j < len(d):
                trim_prompt_cache(cache, len(d) - j)
            if d:
                on_event({"type": "verify", "accepted": j, "of": len(d)})
            if d and cut_body(text, task["indent"])[1]:       # an accepted draft can end the body
                break
        st["decode_s"] = time.time() - t0
        body, _ = cut_body(text, task["indent"])
        # a rejected name the body defines later was a forward reference: the check was wrong there
        st["forward_refs"] = sum(1 for r in rejected if re.search(r"\b(def|val|var|lazy val)\s+" + re.escape(r["name"]) + r"\b", body))
        st["rejected"] = [r["name"] for r in rejected]
        on_event({"type": "done", "stats": st, "body": body})
        return body, st


def looping(text: str, n=6) -> bool:
    """Greedy decoding falls into cycles (the same `case` arm again and again): a block of 2..n complete lines, three times."""
    lines = [l for l in text.split("\n")[:-1] if l.strip()]
    return any(len(lines) >= 3 * k and lines[-k:] == lines[-2 * k:-k] == lines[-3 * k:-2 * k] for k in range(2, n + 1))


def cut_body(text: str, indent: int):
    """The body ends at the closing fence, or at the first complete line at or left of the def's indentation (a `}` or `)` there
    closes the body and is kept). Returns (body, ended)."""
    fence = "```" in text
    if fence:
        text = text[:text.index("```")].rstrip() + "\n\n"     # all lines before the fence are complete
    lines = text.split("\n")
    for k in range(1, len(lines) - 1):                          # complete lines only
        l = lines[k]
        if l.strip() and len(l) - len(l.lstrip()) <= indent:
            if l.strip()[0] in "})":
                return "\n".join(lines[:k + 1]), True
            return "\n".join(lines[:k]).rstrip(), True
    return (text.rstrip(), True) if fence else (text, False)


def make_task(meta: dict) -> dict:
    """A held-out body from phase 0b's sample, with what the decoder needs."""
    src = open(meta["file"], encoding="utf-8").read()
    ls = src.rfind("\n", 0, meta["defStart"]) + 1
    return {**meta, "src": src, "uri": "file:///" + os.path.basename(meta["file"]), "header": src[ls:meta["eq"] + 1]}
