# Subtitle Translation Rules

Use these rules when the bundled script cannot complete the translation stage and the subtitle file must be handled manually.

## Required constraints

- Keep subtitle timing unchanged.
- Keep subtitle order unchanged.
- Translate into Simplified Chinese.
- Do not merge subtitle blocks.
- Do not drop speaker labels, bracketed cues, or simple inline tags.
- Prefer concise subtitle phrasing over literal but verbose translation.

## Validated batch workflow

The stable manual fallback is:

1. Keep the original English SRT as `source.en.srt`.
2. Split it into small batches, typically 40 subtitle blocks per request.
3. Translate each batch with strict 1:1 block preservation.
4. Validate that the translated batch keeps the same index numbers and timestamps.
5. Concatenate all translated batches into `source.zh.srt`.

## Codex prompt

When translating manually with `codex exec`, use this prompt template:

```text
# Role
You are an expert subtitle translator and SRT formatting specialist.
Your Goal: Translate the SRT subtitle batch in <INPUT> into Chinese.
Your Core Principle: Strict Structural Adherence + Natural Spoken Language.

# INPUT DATA
[Global Context]: See <FULL_SOURCE_CONTEXT> (if provided). Use this ONLY for understanding plot/terms. DO NOT translate it.
[Target Batch]: See <INPUT>. This is the ONLY text you must translate and output.

# CRITICAL FORMATTING RULES (Non-Negotiable)
1. Absolute 1-to-1 Mapping:
   - Count the input blocks. The output MUST have the EXACT same number of blocks.
2. Immutable Metadata:
   - Copy Index Numbers exactly.
   - Copy Timestamps exactly.
   - Preserve the exact blank line structure between blocks.
3. Segmentation Logic:
   - Do NOT merge lines.
   - Do NOT split lines.
   - Preserve sentence fragments across neighboring blocks.
4. Safety & Fallback:
   - Never output empty text.
   - Preserve literal symbols or cues when necessary.

# TRANSLATION STYLE GUIDELINES
1. Natural and conversational.
2. Concise and subtitle-readable.
3. Consistent with the global context.

{context_block}

# OUTPUT FORMAT
Return the result in a plaintext code block.

# Actual Task
Translate the following batch into Chinese:

<INPUT>
...
</INPUT>
```

## Recommended output files

- English source subtitle: `source.en.srt`
- Chinese translated subtitle: `source.zh.srt`
- Burned output video: `output.zh.hardsub.mp4`

## Manual recovery flow

1. Open `source.en.srt`.
2. Translate only the subtitle text, not the sequence number or timing line.
3. Save the translated file as `source.zh.srt` in the same work directory.
4. Re-run the main script with `--work-dir ... --skip-download --skip-translate` only if `source.zh.srt` is already complete and only the burn step needs to be retried.
