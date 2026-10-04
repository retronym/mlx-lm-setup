"""Deterministic phrase chunker (no OpenJev): split a 32-word window at syntactic boundaries using spaCy.

next_len(window_words) -> number of words in the next phrase.

Boundaries (a phrase may break BEFORE the token / AFTER the token):
  before: coordinating/subordinating conjunctions, relative pronouns/wh-words, prepositions that head a
          prepositional phrase (except "of"), an opening bracket/quote
  after : , ; : . ? ! and closing brackets/quotes
The phrase ends at the first boundary at/after MIN_LEN words (never past MAX_LEN or a sentence end).
"""
import re

MIN_LEN, MAX_LEN = 4, 12
_nlp = None


def nlp():
    global _nlp
    if _nlp is None:
        import spacy
        _nlp = spacy.load("en_core_web_sm", disable=["ner", "lemmatizer"])
    return _nlp


def _word_index(window):
    """char offset of each word start in ' '.join(window)."""
    offs, pos = [], 0
    for w in window:
        offs.append(pos)
        pos += len(w) + 1
    return offs


def boundaries(window):
    """Set of n such that a phrase of n words (window[:n]) ends at a syntactic boundary."""
    text = " ".join(window)
    offs = _word_index(window)
    start_to_word = {o: i for i, o in enumerate(offs)}
    doc = nlp()(text)
    ok = set()

    def word_of(tok):
        # index of the whitespace word containing this token
        j = 0
        for i, o in enumerate(offs):
            if o <= tok.idx:
                j = i
            else:
                break
        return j

    for tok in doc:
        wi = word_of(tok)
        first_tok_of_word = tok.idx in start_to_word
        # break BEFORE this token (only when it begins a word, and not at word 0)
        if first_tok_of_word and wi > 0:
            before = (tok.pos_ in ("CCONJ", "SCONJ")
                      or tok.tag_ in ("WDT", "WP", "WP$", "WRB")
                      or (tok.dep_ == "prep" and tok.lower_ != "of")
                      or tok.text in ("(", "“", "‘", "\""))
            if before:
                ok.add(wi)              # phrase of wi words ends just before this word
        # break AFTER a punctuation token
        if tok.is_punct and tok.text in (",", ";", ":", ".", "?", "!", ")", "”", "’", "\""):
            ok.add(word_of(tok) + 1)
    # word-attached trailing punctuation (e.g. "bank,") is a token boundary too, handled above
    return ok


def next_len(window):
    hi = min(MAX_LEN, len(window))
    for i, w in enumerate(window[:hi], start=1):          # never cross a sentence end
        if w.rstrip("”’\"')]").endswith((".", "?", "!")):
            hi = i
            break
    if hi <= MIN_LEN:
        return hi
    b = boundaries(window)
    for n in range(MIN_LEN, hi + 1):
        if n in b:
            return n
    return hi


if __name__ == "__main__":
    import json, sys, time
    words = [x["w"] for x in json.load(open("data/book_words.json"))]
    cur, t0, out = 0, time.time(), []
    while cur < 140:
        n = next_len(words[cur:cur + 32])
        out.append(" ".join(words[cur:cur + n])); cur += n
    print(f"{time.time()-t0:.2f}s for {len(out)} phrases")
    for p in out:
        print(" |", p)
