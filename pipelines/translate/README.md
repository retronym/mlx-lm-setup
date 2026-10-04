# Translate shortcut

Press a key with text selected and get its English translation in a Quick Look panel, with a one-to-three sentence summary for longer texts. Press it with nothing selected and you pick a screen region instead. Everything runs on this Mac through the gateway's `/api/translate`.

Screenshots are read by macOS's own OCR (Vision, the Live Text engine) in a fraction of a second, so no vision model has to load. The text then goes to whichever Gemma is already loaded. A warm translation takes 1–3 s; the first one after the models were unloaded adds a ~7 s cold start. Images with little machine-readable text (stylised or vertical lettering, handwriting) fall back to the vision model. Design and measurements: [PLAN.md, Translate](../../PLAN.md#translate-phase-11).

## From the command line

```bash
pipelines/translate/translate.py "Der Zug fällt heute aus."
```

```bash
pipelines/translate/translate.py --image ~/Desktop/shot.png
```

```bash
pipelines/translate/translate.py
```

The last form, with nothing on stdin, lets you drag out a screen region. Text or image bytes can also come in on stdin. `--mode vision` skips OCR, and `--json` prints the full response, which includes the route taken, the model used, OCR confidence and timings.

## Building the shortcut

The shortcut file cannot be shipped (signing it needs an iCloud sign-in), but it is three actions:

1. In Shortcuts, create a new shortcut called **Translate**. In its details (ⓘ), turn on **Use as Quick Action** (Services Menu) and **Show in Share Sheet**, and add a keyboard shortcut, e.g. ⌃⌥T.
2. Set **Receive** to *Text*, *Rich Text*, *Images* and *Files* from *Quick Actions and Share Sheet*, and **If there's no input** to *Continue*.
3. **Run Shell Script**: shell `zsh`, input *Shortcut Input*, pass input *to stdin*, script:
   ```
   /usr/bin/python3 /Users/jzaugg-virtus/code/mlx-lm-setup/pipelines/translate/translate.py --notify
   ```
4. **Make Rich Text from Markdown** (input: *Shell Script Result*).
5. **Quick Look** (input: *Rich Text from Markdown*).

One-time permissions: Shortcuts → Settings → Advanced → **Allow Running Scripts**, and Screen Recording for Shortcuts the first time it captures a region.
