# Showcase explainer: storyboard (v2)

Status: **proposal, for review.** It replaces v1 (built and rendered at commit `90850c1`; see git history), which opened on the memory incident, gave memory management half the film, and moved from topic to topic without saying where it was going.

## Intent

The argument: *a lot of the work an AI assistant does is small, frequent and private, and a Mac can do it locally: cheaper, faster and without the data leaving. The trick is knowing which jobs to give a small model, and checking its work.*

The film opens with **why local**, states the **three-part plan** out loud before starting it, and keeps the plan on screen, so each part is announced before it arrives. Memory management becomes one beat inside the gateway part.

## The spine: one stack of jobs, followed through the film

The through-line is a **deck of real job cards**: the small requests that come up while working with Claude Code in this repo. The same cards appear in every act, and what happens to them is the story:

| Card | Kind | Where it ends up |
|---|---|---|
| Summarise `Foo.scala` | generate | local LLM, gated on faithfulness |
| Is PR #11285 internal housekeeping? | decide | decision / NLI model |
| Which emoji fits this phrase? | decide | decision model |
| Does this summary match the code? | check | NLI model |
| Extract the method names as JSON | generate | local LLM, gated on JSON |
| Is this review positive, negative or mixed? | decide | decision model |
| Design the eviction policy | judgment | stays with Claude |
| Review this concurrency fix | judgment | stays with Claude |

- **Act 1 (why):** the cards pile up, and each one carries a cost, a wait and a privacy mark. This is the problem.
- **Act 3, part 2:** the cards are sorted by kind, and most of them turn out to be choices, not essays. This is the payoff that v1 lacked: the decision-model idea is **set up in act 1** and pays off here.
- **Act 3, part 3:** the cards are routed. Judgment stays with Claude, and the rest goes to the Mac through gates.
- **Ending:** the deck is empty, and the ledger shows where everything went.

## Other devices

1. **The plan rail.** After the why, three chapter titles appear along the top edge: *One front door · Decide, don't generate · Trust, but check.* They stay there for the rest of the film, with the current chapter lit, the finished ones ticked and the next one dim. Every chapter starts with its title sliding from the rail into the frame and ends by sliding back. The viewer always knows where they are and what's next.
2. **The ledger.** A small, quiet counter in the corner: *jobs done on this Mac* and *jobs done by Claude*. It ticks as each demo runs. At the end it shows the real split for making this film (see "Production split"), read from the gateway's request counters and the build log. The Claude column is not hidden, because it *is* the division of labour.
3. **Bookend with a promise.** The first line says the voice was made on this Mac and that we'll come back to it; the last act keeps that promise.
4. **Show the mechanism.** As in v1: decide is animated as the real computation on a real phrase, and the gates as a real retry.
5. **Honesty beats.** Local models are smaller and are confidently wrong sometimes (the invented IndexError, and the NLI model agreeing with it). Act 1 says it, act 3 deals with it.
6. **No stage-manager metaphor.** It's not needed once memory is a single beat.

Kept from v1: the visual language (dark warm ground, one orange accent for "problem" and "active", family hues, real captures, numbers that count up, morphs through shared objects rather than cuts), and the build pipeline (Remotion, `narrate` with cue markers, real data).

## Structure and timing (≈ 3:30)

| | Act | Time | Job |
|---|---|---|---|
| 0 | Hook | 0:00–0:12 | the promise: everything here was made on this Mac |
| 1 | Why local | 0:12–1:05 | cost, latency, sovereignty, and the honest catch |
| 2 | The plan | 1:05–1:15 | the question becomes three parts; the rail appears |
| 3a | One front door | 1:15–1:45 | the gateway; models on demand; memory in one beat |
| 3b | Decide, don't generate | 1:45–2:35 | sort the cards; the mechanism; Alice and PR triage as evidence |
| 3c | Trust, but check | 2:35–3:05 | wrong in confident ways; gates; routing with Claude |
| 4 | Payoff | 3:05–3:30 | the voice, the ledger, the empty deck |

## Scenes

Narration is a first draft at about 150 words a minute. `[[cue]]` marks a moment the animation must hit.

### 0. Hook (0:00–0:12)

- **Visual:** black. The narrator's waveform draws itself as the first sentence is spoken. The ledger fades in at the bottom right: `on this Mac: 0 · by Claude: 0`. It ticks to 1 as the sentence ends.
- **Narration:** "This voice wasn't made in a data centre. [[ledger]] Neither was the first draft of what it's saying, or the fact-check. Both happened on this Mac. We'll come back to how. First, why you'd want that."

### 1. Why local (0:12–1:05)

**1a. The deck (0:12–0:24).** The job cards deal in one by one, in a loose stack, as the narration names a few. Each card is real text (a file name, a PR number, an Alice phrase).
"Working with an AI assistant creates a stream of small jobs. [[deal]] Summarise this file. Is this pull request just housekeeping? Which emoji fits this phrase? Does this summary match the code?"

**1b. Three costs (0:24–0:56).** Three columns form beside the deck, one per reason. Each card in the stack gets a badge in each column as that reason is named, so the viewer sees the costs pile up on the same cards.

- **Cost.** A token counter runs over the deck at real volume: the Alice book is 748 phrases × 4 questions, the PR triage 300 PRs × 6 questions, and the narration had dozens of re-takes. Counts are computed from the real pipelines, shown as tokens, with a marginal local cost of "electricity". (Dollar figures only if we source a current price at build time; see open questions.)
  "Sent to a hosted model, every one is billed. That's fine for one. [[volume]] It isn't fine for a book's worth of phrases, three hundred pull requests, or fifty re-takes of a narration. Locally, a re-take is free, so you iterate more."
- **Latency.** A round trip animates out to a cloud and back, against a short local hop. The local numbers are measured: first token 0.27 s from the chat capture, about 1 s per decision, a cold start of about 2 s. The cloud side gets no number, just the shape of a network round trip and a queue.
  "Small jobs are also latency bound. [[latency]] A local decision takes about a second, with no network and no rate limit."
- **Sovereignty.** The cards whose content is private (unreleased code, a PR description, a voice clip of a real person) get a lock. Their path to the cloud is drawn and then cut at `127.0.0.1`.
  "And some of this text shouldn't leave the machine at all: [[private]] unreleased code, private pull requests, a voice."

**1c. The catch (0:56–1:05).** One card flips over to show a confidently wrong answer from a local model, in orange. It's a small preview of act 3c.
"The catch: [[catch]] local models are smaller, and they're sometimes confidently wrong."

### 2. The plan (1:05–1:15)

- **Visual:** the three columns fold away. A question types out in the centre and then splits into three titles, which rise into the **plan rail** at the top of the frame and stay there for the rest of the film.
- **Narration:** "So the question isn't local or cloud. It's which jobs to hand a small model, and how to trust the result. [[plan]] Three parts: [[p1]] one front door, [[p2]] the right kind of model for each job, [[p3]] and checking its work."

### 3a. One front door (1:15–1:45)

- **Visual:**
  - *One front door* lights on the rail and slides down into the frame.
  - A gateway box `127.0.0.1:8090` appears. Claude Code (MCP), the browser (chat, admin) and any OpenAI client connect to it from the left; the model families sit behind it on the right.
  - The chat and admin captures slide past briefly.
  - Then the one memory beat: a compact memory bar under the gateway. A model is called on in about 2 s, serves, idles and leaves, and its memory flows back. When a newcomer arrives, the least recently used model makes room. The budget line is visible. All of this takes about 8 seconds, with no incident and no swap.
  - The ledger ticks with each request.
- **Narration:** "[[door]] One gateway on this Mac is the front door for everything. [[clients]] Claude Code talks to it over MCP, the browser gets a chat and an admin page, and anything that speaks the OpenAI API works too. [[memory]] Models load on demand in a couple of seconds and leave when they go idle, within a memory budget, so the Mac stays usable for everything else."

### 3b. Decide, don't generate (1:45–2:35)

- **Bridge (1:45–1:58):**
  - *Decide, don't generate* slides down from the rail. The deck from act 1 returns.
  - The cards sort themselves into three trays labelled **generate**, **decide** and **check**. Most land in decide and check; two land in generate; two are set aside with a "judgment" tag (they matter in 3c).
  - "Look at those jobs again. [[sort]] Most of them aren't essays. They're choices: which label, which emoji, yes or no."
- **Mechanism (1:58–2:20):**
  - One decide card opens: the sentiment review. Left, a model types its answer token by token; right, the decision model scores all four options in one pass.
  - Then the real computation on the Alice phrase, as in v1: the context is read once, the four questions add only their own options, each score is `logit(yes) − logit(no)`, and the top emoji rise above the phrase.
  - "A decision model scores every option in a single forward pass, with probabilities. [[once]] The context is read once; each extra question costs only its options. Fast, deterministic, and nothing to parse."
- **Evidence (2:20–2:35):**
  - Two quick vignettes, each the decide card at volume. The Alice book capture, with emoji above every phrase (a count of the scored phrases, read at build time). Then PR titles streaming past six questions, the ROC curves drawing, and the "well ranked, badly calibrated" note.
  - The ledger runs up with each.
  - "That's how this repo annotates Alice in Wonderland, [[book]] phrase by phrase, and triages three hundred Scala pull requests. [[prs]] Good at ranking; less good at knowing where to draw the line."

### 3c. Trust, but check (2:35–3:05)

- **Wrong (2:35–2:45):** *Trust, but check* slides down from the rail. The flipped card from act 1 returns: the `moving_avg` code, the invented IndexError, and OpenJev agreeing at 0.72.
  "Remember the catch. [[wrong]] Asked to review this code, a local model invented a bug, and the fact checker agreed with it."
- **Gates (2:45–2:55):** a generate card goes through `iterate`: it fails the faithfulness gate, the failure is fed back, and the second try passes. The check cards from the sorting step become the gate posts. That's the visual rhyme: *check* is itself a decision.
  "So generated work passes through checks a machine can verify, and gets retried until it does."
- **Routing (2:55–3:05):** the whole deck routes at once. The two judgment cards go up to Claude; everything else goes down to the Mac. The MCP tool names (`chat`, `decide`, `entail`, `iterate`) are the paths.
  "[[route]] Claude keeps design and judgment. The cheap, checkable work stays on the Mac."

### 4. Payoff (3:05–3:30)

- **The voice (3:05–3:18):**
  - The rail ticks its last chapter and fades. The hook's waveform returns, now with Whisper's word timestamps beneath it.
  - A one-line voice description becomes `narrator.wav`, which fans out to every scene's clip.
  - "As promised: [[voice]] this voice was designed from one sentence of description, then cloned so every scene sounds the same, and Whisper timed every word you've seen."
- **The ledger (3:18–3:30):**
  - The film's own production jobs deal out as a second deck: draft each scene, fact-check each sentence, voice each scene, pick the best take, time the words, sort the cards, design the storyboard, write the renderer, edit the script.
  - They route exactly as the deck in 3c did. Most go down to the Mac; design, code and the final edit go up to Claude.
  - The ledger grows to full frame with the real counts: calls per local model from the gateway's counters (reset before the build), and how many drafted lines Claude kept as written and how many it edited.
  - Fade to the repo name.
  - "Making this film was a stack of jobs too. [[total]] The drafts, the fact-checks, the voice and the timing ran on this Mac. The storyboard, the code and the final edit were Claude's. Same split, same reasons."

## Production split: what the local models do in making the film

The hook and the payoff claim local work, so the build has to earn it. These jobs move to the local models. Each was tried on 2026-10-04 before writing this:

| Production job | Local model | Tried | Result |
|---|---|---|---|
| Voice each scene, consistently | Qwen3-TTS clone (`narrate`) | built (v1) | works; the same narrator in every scene |
| Time every word; cue times; captions | Whisper (`narrate`) | built (v1) | works |
| Pronunciation check | Whisper (`transcript_differs`) | built | flags real differences; spacing and number formatting are filtered or expected |
| **First draft of each scene's narration**, from the storyboard's fact list for that scene | Gemma 4 via `iterate`: gates on length, no digits, no list markup, NLI faithfulness to the fact list | 1 scene | passed on the first attempt in 6 s; usable but plain, e.g. "port eight zero nine zero". Claude edits; the ledger counts lines kept vs edited |
| **Fact-check every narration sentence** against README, FINDINGS and PLAN | OpenJev 4B (`entail`) | 6 claims | 4 true claims entailed (≥ 0.99); an invented telemetry claim came back neutral (flagged); **an inflated number ("three thousand PRs") was wrongly entailed at 0.975**. So the NLI model checks the wording, and **numbers are checked by code** against the data files, never by the model |
| **Sort the job cards on screen** (generate / decide / check / judgment) | Jev-Style 2B (`decide`) | 6 cards | 5 of 6 right. It filed "does this summary match the code?" as generate (check came second at 0.19). The film shows the live result, miss included, and says so: a decision model is good, not perfect |
| **Pick the best take** per scene: 3 fresh takes, keep the one Whisper transcribes most faithfully and that fits the scene's target length | Qwen3-TTS + Whisper | not yet | the cost argument made literal: re-takes are free. Test in the build, and keep it only if the takes really differ |

Stays with Claude: the storyboard and its structure, the Remotion code, the final edit of the narration, and reviewing rendered frames. These are the judgment cards from act 3c.

## How it changes the build

- **Reused:** the Remotion project, the `narrate` tool and cues, the captures, the decide mechanism and pipeline vignettes, the gates loop, and the voice reveal.
- **New:**
  - The job cards: a component with real content and badges.
  - The cost, latency and sovereignty columns.
  - The plan rail, as a persistent layer above the scenes.
  - The ledger.
  - The sorting and routing animations.
- **Cut:** the cold open incident, the memory scene, the stage manager, and the old door scene (folded into 3a).
- **Ledger data:**
  - Restart the gateway before narrating, and read `backends_status` request counts after the build.
  - Token volumes come from the real inputs (chunk counts × prompt sizes, measured with the model's tokenizer).
  - If a dollar figure appears, its price and date are cited on screen.

## Decisions

1. Cost is shown in tokens, plus "marginal cost: electricity". No dollar figures.
2. The hook claims only what the local models really did: voice, timing, first drafts, fact-checks and card sorting. The ledger shows Claude's share openly.
3. No short cut.
4. The ledger lists Claude's jobs by name (see above).

## Open questions

1. **Draft quality.** If the local drafts need heavy editing, the "first draft" claim is weak. Proposal: draft all scenes before committing to the claim, and keep it only if Claude keeps most lines with light edits. The ledger reports the real ratio either way.
