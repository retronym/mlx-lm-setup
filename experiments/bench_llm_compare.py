"""Head-to-head of the catalog's generative LLMs on sub-agent-style tasks with mechanically checkable answers.

One model per process (memory):
  /opt/homebrew/opt/mlx-lm/libexec/bin/python bench_llm_compare.py <hf-model-id> <label>
Writes data/llm_bench_<label>.json and prints a table. Modes: thinking off, and thinking on where the chat template supports it.
Single run per cell at temperature 0.3, so treat pass/fail on one task as a signal, not a statistic.
"""
import ast, json, os, re, subprocess, sys, tempfile, time
import mlx.core as mx
from mlx_lm import load, stream_generate
from mlx_lm.sample_utils import make_sampler

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))     # the repo root: data/ and models/ live there
sys.path[:0] = [os.path.join(HERE, "experiments"), os.path.join(HERE, "pipelines", "emoji_book")]
MODEL, LABEL = sys.argv[1], sys.argv[2]
SCALA = """object Foo {
  def a(x: Int): Int = x + 1
  private def b(s: String) = s.length
  val c = 3
  def d[T](xs: List[T]): Int = xs.size
}"""
PY_REVIEW = """def moving_avg(xs, n):
    out = []
    for i in range(len(xs)):
        window = xs[i:i+n]
        out.append(sum(window) / n)
    return out"""
SUP_SRC = open(os.path.join(HERE, "gateway", "supervisor.py")).read()


def public_methods(src, cls):
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.ClassDef) and node.name == cls:
            return {n.name for n in node.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and not n.name.startswith("_")}
    return set()


def strip_fences(t):
    m = re.search(r"```(?:\w+)?\n(.*?)```", t, re.S)
    return m.group(1) if m else t


# ---- checkers: text (thinking removed) -> (passed, note) -------------------------------------------------------------
def chk_json(t):
    try:
        j = json.loads(strip_fences(t).strip())
        ok = set(j.get("methods", [])) == {"a", "b", "d"} and j.get("count") == 3
        return ok, f"methods={j.get('methods')} count={j.get('count')}"
    except Exception as e:                                     # noqa: BLE001
        return False, f"not valid JSON ({type(e).__name__})"


def chk_code(t):
    code = strip_fences(t)
    if re.search(r"\b(import\s+(os|subprocess|socket|sys)|__import__|eval\(|exec\(|open\()", code):
        return False, "refused to run: suspicious code"
    code += "\nassert dedupe([]) == []\nassert dedupe(list('abca')) == list('abc')\nassert dedupe([3,1,3,2,1]) == [3,1,2]\n"
    with tempfile.TemporaryDirectory() as td:
        try:
            r = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, timeout=10, cwd=td)
        except subprocess.TimeoutExpired:
            return False, "timeout"
    return r.returncode == 0, (r.stderr.strip().splitlines() or ["ok"])[-1][:100]


def chk_review(t):
    kw = re.search(r"(partial|fewer than|shorter than|extends? beyond|instead of (the )?(actual|window)|actual (window|length|size|number)|trailing|last (n|few|\w+ )?(window|element|position|item)|end of the (list|input)|near the end|tail|shorter window|incomplete window)", t, re.I)
    false_claim = re.search(r"(raises?|throws?|causes?|results? in|leads? to)[^.\n]{0,40}IndexError", t, re.I)
    return bool(kw) and not false_claim, f"keyword={'yes' if kw else 'no'} claims-IndexError={'yes' if false_claim else 'no'}"


def chk_fold(t):
    tail = t.strip()[-300:]
    ok = "-6" in tail and re.search(r"(?<![\d.-])2(?![\d.])", tail.replace("-6", " ")) is not None
    return ok, tail[-60:].replace("\n", " ")


TRUTH = public_methods(SUP_SRC, "Supervisor")


def chk_methods(t):
    got = {w for w in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", t) if not w.startswith("_")}
    got = {w for w in got if w in TRUTH or w in ("ensure_running", "lease", "start", "stop", "set_policy", "snapshot", "log_tail")}
    # precision: names listed that are not real public methods
    listed = {w.strip(" `*.-") for w in re.split(r"[,\n]", t)}
    listed = {w for w in listed if re.fullmatch(r"[a-z_][a-z0-9_]*", w)}
    rec = len(TRUTH & listed) / max(1, len(TRUTH))
    prec = len(TRUTH & listed) / max(1, len(listed))
    return rec >= 0.8 and prec >= 0.8, f"recall={rec:.2f} precision={prec:.2f} ({len(TRUTH)} real methods)"


def chk_format(t):
    lines = [l for l in t.strip().splitlines() if l.strip()]
    ok = len(lines) == 3 and all(re.match(r"^\s*([-*•]|\d+[.)])\s+\S", l) and len(re.sub(r"^\s*([-*•]|\d+[.)])\s+", "", l).split()) <= 8 for l in lines)
    return ok, f"{len(lines)} lines, words={[len(re.sub(r'^\s*([-*•]|\d+[.)])\s+', '', l).split()) for l in lines]}"


TASKS = [
    ("extract_json", f"Extract the names of all methods (def) defined in this Scala object. Reply with ONLY JSON of the form {{\"methods\": [...], \"count\": N}}.\n\n{SCALA}", 500, chk_json),
    ("codegen", "Write a Python function `dedupe(xs)` that removes duplicates from a list while preserving first-seen order. Output only the code.", 600, chk_code),
    ("review_bug", f"Find the bug in this Python function, tersely (2-3 sentences):\n\n{PY_REVIEW}", 500, chk_review),
    ("scala_fold", "In Scala, what do `List(1,2,3).foldLeft(0)(_ - _)` and `List(1,2,3).foldRight(0)(_ - _)` evaluate to? End your answer with the two numbers separated by a comma, like: X, Y", 600, chk_fold),
    ("long_context", f"Here is a Python module:\n\n{SUP_SRC}\n\nList the names of all public methods (those not starting with an underscore) of the class Supervisor, as a comma-separated list and nothing else.", 400, chk_methods),
    ("format", "Give exactly 3 bullet points about why mixture-of-experts models are fast at inference. Each bullet must be at most 8 words. No preamble, no other text.", 300, chk_format),
]

t0 = time.time()
model, tok = load(MODEL)
load_s = time.time() - t0
mx.reset_peak_memory()
tmpl = tok.chat_template if isinstance(tok.chat_template, str) else str(tok.chat_template)
supports_thinking = "enable_thinking" in tmpl
modes = [("think_off", {"enable_thinking": False}), ("think_on", {"enable_thinking": True})] if supports_thinking else [("default", {})]
sampler = make_sampler(temp=0.3, top_p=0.95)
mx.random.seed(0)


def split_thinking(text):
    """Return (visible_text, thinking_text) for Qwen (<think>..</think>) and Gemma (<|channel>thought ... <channel|>) formats."""
    if "</think>" in text:
        th, _, vis = text.partition("</think>")
        return vis.strip(), th
    if "<channel|>" in text:
        th, _, vis = text.partition("<channel|>")
        return vis.strip(), th
    return text.strip(), ""


rows = []
print(f"== {LABEL}: {MODEL} | load {load_s:.1f}s | thinking template support: {supports_thinking}", flush=True)
for mode, kw in modes:
    for name, prompt, max_tokens, chk in TASKS:
        cap = max_tokens + (3500 if mode == "think_on" else 0)
        text = tok.apply_chat_template([{"role": "user", "content": prompt}], tokenize=False, add_generation_prompt=True, **kw)
        out, last = "", None
        t = time.time()
        for r in stream_generate(model, tok, text, max_tokens=cap, sampler=sampler):
            out += r.text
            last = r
        dt = time.time() - t
        visible, thought = split_thinking(out)
        ok, note = chk(visible)
        capped = last.generation_tokens >= cap
        think_tok = len(tok.encode(thought)) if thought else 0
        row = dict(model=LABEL, mode=mode, task=name, ok=bool(ok), note=note, gen_tokens=last.generation_tokens, think_tokens=think_tok,
                   capped=capped, secs=round(dt, 1), gen_tps=round(last.generation_tps, 1), prompt_tokens=last.prompt_tokens,
                   prompt_tps=round(last.prompt_tps, 1), answer=visible[:300])
        rows.append(row)
        print(f"  {mode:9s} {name:13s} {'PASS' if ok else 'fail'}  gen={last.generation_tokens:5d} (think {think_tok:5d}){' CAPPED' if capped else ''}  "
              f"{last.generation_tps:5.1f} tok/s  prompt {last.prompt_tokens}@{last.prompt_tps:.0f} tok/s  {dt:5.1f}s | {note}", flush=True)

peak = mx.get_peak_memory() / 1e9
os.makedirs(os.path.join(HERE, "data"), exist_ok=True)
json.dump(dict(label=LABEL, model=MODEL, load_s=round(load_s, 1), peak_gb=round(peak, 1), supports_thinking=supports_thinking, rows=rows),
          open(os.path.join(HERE, "data", f"llm_bench_{LABEL}.json"), "w"), ensure_ascii=False, indent=1)
print(f"== {LABEL}: peak memory {peak:.1f} GB, load {load_s:.1f}s", flush=True)
