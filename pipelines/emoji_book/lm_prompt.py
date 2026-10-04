"""The few-shot prompt under which an LLM scores emoji for a phrase: log P(emoji | prompt) (score_worker.py lm, lm_emoji.py)."""

FEWSHOT = """Phrase: The cat knocked the glass off the table and stared at me.
Emoji: 😼

Phrase: We finally got the keys to our first house!
Emoji: 🏠

Phrase: I am literally dying, that was so funny.
Emoji: 💀

Phrase: Her heart sank when she read the letter.
Emoji: 💔

Phrase: """


def prompt(phrase, fewshot=FEWSHOT):
    return fewshot + phrase + "\nEmoji:"
