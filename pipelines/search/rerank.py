"""Cross-encoder reranking with Qwen3-Reranker-0.6B: a causal LM asked "does this document answer the query? yes/no"; the score
is P(yes) from the two logits. Applied to the top ~30 fused candidates only, so nothing about it needs a lifecycle."""
import os
import torch
from transformers import AutoModelForCausalLM, AutoTokenizer

MODEL = os.environ.get("RERANK_MODEL", "Qwen/Qwen3-Reranker-0.6B")
TASK = "Given a question or code snippet about the Scala compiler, standard library or build tools, judge whether the document is relevant source code, documentation or issue text that helps answer it"
PREFIX = ('<|im_start|>system\nJudge whether the Document meets the requirements based on the Query and the Instruct provided. '
          'Note that the answer can only be "yes" or "no".<|im_end|>\n<|im_start|>user\n')
SUFFIX = "<|im_end|>\n<|im_start|>assistant\n<think>\n\n</think>\n\n"
DOC_CHARS = 1800


class Reranker:
    def __init__(self, model=MODEL):
        self.name = model
        self.tok = AutoTokenizer.from_pretrained(model, padding_side="left")
        self.dev = "mps" if torch.backends.mps.is_available() else "cpu"
        self.m = AutoModelForCausalLM.from_pretrained(model, dtype=torch.float16 if self.dev == "mps" else torch.float32).to(self.dev).eval()
        self.yes, self.no = self.tok.convert_tokens_to_ids("yes"), self.tok.convert_tokens_to_ids("no")

    @torch.no_grad()
    def scores(self, query, docs, batch=8):
        texts = [f"{PREFIX}<Instruct>: {TASK}\n<Query>: {query}\n<Document>: {d[:DOC_CHARS]}{SUFFIX}" for d in docs]
        out = []
        for s in range(0, len(texts), batch):
            b = self.tok(texts[s:s + batch], padding=True, truncation=True, max_length=1024, return_tensors="pt").to(self.dev)
            logits = self.m(**b).logits[:, -1, [self.no, self.yes]].float()
            out += torch.softmax(logits, dim=-1)[:, 1].cpu().tolist()
        return out
