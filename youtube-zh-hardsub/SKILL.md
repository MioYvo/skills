---
name: youtube-zh-hardsub
description: Download a YouTube video, fetch its English subtitles or auto-captions with yt-dlp, translate the subtitle text into Simplified Chinese, and burn the Chinese subtitles into a final hard-subtitled video with ffmpeg. Use when the user provides a YouTube URL and wants a finished video file with 中文字幕, 硬字幕, 中文字幕烧录, or asks to download YouTube video subtitles and render a Chinese-subtitled export.
---

# YouTube Zh Hardsub

Convert a YouTube URL into a finished video file with Simplified Chinese hard subtitles.

Use the bundled script for the full pipeline instead of redoing each step manually.

## Ask First

Before downloading anything, ask the user where they want the downloaded assets and final hard-sub video saved.

- If the user gives a directory, use it.
- If the user does not care, default to `~/Videos/`.
- Unless the user explicitly asks to separate them, use one root directory for both downloaded source assets and the final rendered video.

## Prerequisites

Ensure these tools or environment variables are available before running:

- `python3`
- `yt-dlp`
- `ffmpeg`
- `codex` or `OPENAI_API_KEY`
- `firefox` with a signed-in profile when using the default cookie extraction flow

Optional configuration:

- `--cookies-from-browser` defaults to `firefox` because YouTube may reject anonymous requests with bot checks
- `--proxy` controls the proxy used by `yt-dlp`, defaulting to `http://127.0.0.1:7890`
- `--translation-backend` chooses between `codex`, `openai`, or `auto`; `auto` prefers `codex` when available
- `OPENAI_BASE_URL` when the API endpoint is not `https://api.openai.com/v1`
- `OPENAI_TRANSLATION_MODEL` when the default translation model should be overridden

## Validated Workflow

This is the production path that worked in a real run:

1. Ask the user where to save the work directory and final video. Default to `~/Videos/`.
2. Use `yt-dlp --cookies-from-browser firefox` together with the proxy because YouTube may block anonymous requests.
3. Download the source video as `source.mp4` and the English subtitle track as `source.en.srt`.
4. Translate the English SRT into Simplified Chinese in batches, preserving block count, sequence numbers, timestamps, and fragment boundaries.
5. Burn `source.zh.srt` into the video with `ffmpeg` using a CJK-capable font such as `Noto Sans CJK SC`.

## Default workflow

1. Run the bundled script with the YouTube URL.
2. Let the script query video metadata and prefer manual English subtitles.
3. Fall back to English auto-captions when manual subtitles do not exist.
4. Use `codex` batch translation by default when available, or the OpenAI API when `OPENAI_API_KEY` is configured.
5. Keep the original subtitle timing and only translate subtitle text.
6. Burn the translated Chinese subtitles into a new MP4 file with `ffmpeg`.

## Command

```bash
python3 .claude/skills/youtube-zh-hardsub/scripts/render_youtube_zh_hardsub.py \
  "https://www.youtube.com/watch?v=VIDEO_ID" \
  --cookies-from-browser firefox \
  --proxy "http://127.0.0.1:7890" \
  --output-dir ~/Videos
```

The script creates a dedicated work directory under `--output-dir` and writes:

- `source.mp4` or the merged source video file
- `source.en.srt`
- `source.zh.srt`
- `output.zh.hardsub.mp4`
- `video_metadata.json`

## Resume behavior

Avoid re-downloading when only part of the pipeline failed.

If translation failed after download, rerun with:

```bash
python3 .claude/skills/youtube-zh-hardsub/scripts/render_youtube_zh_hardsub.py \
  --work-dir /path/to/existing-work-dir \
  --cookies-from-browser firefox \
  --proxy "http://127.0.0.1:7890" \
  --skip-download
```

If burning failed but `source.zh.srt` already exists, rerun with:

```bash
python3 .claude/skills/youtube-zh-hardsub/scripts/render_youtube_zh_hardsub.py \
  --work-dir /path/to/existing-work-dir \
  --cookies-from-browser firefox \
  --proxy "http://127.0.0.1:7890" \
  --skip-download \
  --skip-translate
```

## Translation rules

- Translate into Simplified Chinese.
- Preserve subtitle order and timing.
- Keep subtitle content concise enough for on-screen reading.
- Preserve speaker tags, music cues, and simple inline tags such as `<i>...</i>`.
- Do not summarize or merge subtitle blocks.

Load [subtitle-translation.md](/home/mio/PycharmProjects/jura-backend/.claude/skills/youtube-zh-hardsub/references/subtitle-translation.md) only when you need the fallback manual rules or need to inspect the translation constraints separately from the script.

## Failure handling

- If `yt-dlp` reports `Sign in to confirm you’re not a bot`, keep `--cookies-from-browser firefox` enabled or switch it to the browser profile the user actually uses.
- If no English subtitles or auto-captions are available, stop and report that the video lacks an English subtitle source for this workflow.
- If `ffmpeg` renders missing-glyph boxes, rerun with `--font-name` pointing to an installed CJK font.
- If `codex` translation fails temporarily, rerun from `--skip-download`; the downloaded assets remain reusable.
- If the OpenAI API call fails, keep the downloaded assets in the work directory and retry from `--skip-download`.
