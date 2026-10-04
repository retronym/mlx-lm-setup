"""Precompute deterministic phrase boundaries for the whole book with the spaCy chunker (no model, ~0.1 s/phrase).

Run in .venv-jev (spaCy lives there): .venv-jev/bin/python chunk_book.py   ->  data/book_chunks.json  [[w0, w1], ...]
"""
import json, os, time
import chunker_spacy

HERE = os.path.dirname(os.path.abspath(__file__))
words = [w["w"] for w in json.load(open(os.path.join(HERE, "data", "book_words.json")))]
cur, chunks, t0 = 0, [], time.time()
while cur < len(words):
    n = chunker_spacy.next_len(words[cur:cur + 32])
    chunks.append([cur, cur + n]); cur += n
json.dump(chunks, open(os.path.join(HERE, "data", "book_chunks.json"), "w"))
print(f"{len(chunks)} phrases over {len(words)} words in {time.time()-t0:.0f}s "
      f"(mean {len(words)/len(chunks):.1f} words/phrase)")
