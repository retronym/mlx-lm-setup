# Speech: local text-to-speech and speech-to-text

The gateway can speak and listen. The point is video explainers: Claude writes a script, the gateway turns each scene into narration with **known timings**, and a renderer lays the visuals out against them. Everything is local; clips are files under `data/audio`.

## Setup (one venv for all speech models)

```bash
uv venv --python 3.13 .venv-audio
uv pip install --python .venv-audio/bin/python mlx-audio "misaki[en]" num2words spacy phonemizer-fork espeakng-loader \
    fastapi uvicorn python-multipart soundfile pip pillow imageio-ffmpeg
.venv-audio/bin/python -m spacy download en_core_web_sm     # Kokoro's text front end; needs pip inside the venv
```

`pillow` and `imageio-ffmpeg` are only for the explainer example (the latter bundles an ffmpeg binary, so nothing is installed system-wide). Weights download from Hugging Face on first use of each model. Nothing to start by hand: the gateway starts the speech backends on demand like every other model.

## The models

| Backend | Model | Memory (RSS) | Speed (warm, M5 Pro) | For |
|---|---|---|---|---|
| `kokoro` | Kokoro-82M | 0.9 GB | about 30x real time (RTF 0.03) | default narration; 27 English preset voices (`af_*`, `am_*`, `bf_*`, `bm_*`); no cloning |
| `qwen3-tts-design` | Qwen3-TTS 1.7B VoiceDesign, 8-bit | 3.7 GB | about 2x real time (RTF 0.4-0.55) | invent a narrator from a text description |
| `qwen3-tts-clone` (alias `clone`) | Qwen3-TTS 1.7B Base, 8-bit | 3.7 GB | about 2.5x real time (RTF 0.2-0.4) | speak in the voice of a reference clip, consistently across scenes |
| `whisper` (alias `stt`) | Whisper large-v3-turbo, fp16 | 2.0 GB | a 20 s clip in about 3 s | word timestamps for captions; checking that a clip says what it should |

The catalog's memory estimates are higher than the RSS figures (2.5 / 4.5 / 3 GB) because Metal memory is not in RSS. Not added, deliberately: Dia (multi-speaker dialogue tags), Orpheus, Chatterbox, Higgs Audio v3, OmniVoice. They overlap with Qwen3-TTS or serve a need we do not have yet; adding one is a catalog entry on the `mlx_audio_tts` adapter, since `tts_server.py` passes the request through to `model.generate`.

## Using it

**From Claude Code**: the `speak` and `transcribe` MCP tools.

```
speak(text="...", voice="bm_george")                         # Kokoro preset voice
speak(text="...", model="clone", ref_audio="narrator")      # clone a reference clip (also the default for model="clone")
speak(text="...", model="qwen3-tts-design",
      instruct="A calm, warm female narrator in her thirties, clear and measured.",
      save_as_voice="narrator")                              # design once, keep the voice
transcribe(path="<a clip path from speak>")                  # text + word timestamps
```

`speak` returns `path` (absolute), `duration_s`, `sample_rate` and `segments` (`[{text, start_s, end_s}]` per sentence group). Text of any length works: it is split at sentence boundaries into groups of at most 300 characters (220 for Qwen3-TTS), synthesised group by group, and joined with a 0.15 s pause (0.45 s at a blank line). The same request returns the cached clip (`cached: true`); `fresh: true` forces a new take, which matters for the sampling models.

**Over HTTP**: `POST /v1/audio/speech` (OpenAI-compatible; returns `audio/wav`), `POST /api/speak` (JSON as above), `POST /api/transcribe`, `GET /api/voices`.

**In the browser**: the chat site has a voice picker and a "▶ speak" button on every finished reply (reasoning and code blocks are skipped).

## A designed voice is a recipe, then a clip

Voice design draws a **new** voice on every call, so scenes narrated with it would not match. The workflow is: design a narrator once with `save_as_voice="narrator"` (this copies the clip to `data/voices/narrator.wav` with its transcript in `narrator.txt`), then narrate every scene with the clone model, whose default reference is `narrator`. To clone a real person's voice put a 5-15 s clip and its transcript in `data/voices/`; **get their consent first**. Reference clips are named, never passed as paths, and `transcribe` can only read under `data/audio` and `data/voices`.

## Example: a narrated explainer

[`examples/explainer/`](../examples/explainer/) turns a `script.json` of scenes (heading, bullets or a box-and-arrow diagram, narration) into an MP4 with burned-in captions and an `.srt`:

```bash
.venv-audio/bin/python examples/explainer/build.py                  # clone voice; --model kokoro --voice bm_george for a preset
open data/explainer/explainer.mp4
```

Per scene it calls `/api/speak` (duration), then `/api/transcribe` (word timestamps), draws slides with Pillow, and assembles them with ffmpeg. A frame changes exactly when a caption line or a bullet is due, so timing follows the audio. The five-scene demo (82 s of video) takes about 35 s end to end, most of it synthesis (3-5 s per scene) and model loading. It is the workflow in miniature: Claude writes `script.json`, runs the script, and looks at frames; swapping the slide renderer for Remotion or HTML screenshots changes `draw_slide` and nothing else.

## What we measured and what we could not

- Intelligibility is checked mechanically: every clip was transcribed back with Whisper and matched its script word for word apart from punctuation and number formatting ("Phase nine" came back as "Phase 9"). **Naturalness and voice similarity were not judged by ear here**; listen to the voices (the chat site's picker is the quickest way) before committing to one.
- The first Kokoro call in a new language pipeline pays about 10 s for the text front end (British voices: `lang_code` is derived from the voice name); the server warms up the default one before reporting ready. Qwen3-TTS backends take about a minute on a cold download, a few seconds from the cache.
- Speech runs on the same GPU as the LLMs. A narration batch while a 30B model generates slows both; the memory budget keeps them from evicting each other only because the speech models are small.
