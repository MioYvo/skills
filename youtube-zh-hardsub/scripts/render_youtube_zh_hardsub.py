#!/usr/bin/env python3
"""
Download a YouTube video, translate English subtitles to Simplified Chinese,
and burn the translated subtitles into a hard-subbed MP4.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


VIDEO_EXTENSIONS = {".mp4", ".mkv", ".webm", ".mov", ".m4v", ".avi"}
DEFAULT_MODEL = os.environ.get("OPENAI_TRANSLATION_MODEL", "gpt-4.1-mini")
DEFAULT_API_BASE = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")
DEFAULT_FONT = os.environ.get("YOUTUBE_ZH_HARDSUB_FONT", "Noto Sans CJK SC")
DEFAULT_OUTPUT_DIR = str(Path.home() / "Videos")
DEFAULT_PROXY = "http://127.0.0.1:7890"
DEFAULT_COOKIES_BROWSER = "firefox"
SYSTEM_PROMPT = """You translate English subtitle text into Simplified Chinese.
Return valid JSON only, in the exact shape:
{"items":[{"id":1,"translated":"..."},{"id":2,"translated":"..."}]}

Rules:
- Keep item count identical.
- Keep every id unchanged.
- Translate subtitle text only.
- Do not add explanations, markdown, or code fences.
- Preserve speaker labels, bracketed cues like [Music], and simple inline tags such as <i>...</i>.
- Keep wording concise and natural for on-screen subtitles.
- Do not merge or split subtitle items.
"""
CODEX_TRANSLATION_PROMPT_TEMPLATE = """# Role
You are an expert subtitle translator and SRT formatting specialist.
Your Goal: Translate the SRT subtitle batch in `<INPUT>` into Chinese.
Your Core Principle: **Strict Structural Adherence + Natural Spoken Language.**

# INPUT DATA
[Global Context]: See `<FULL_SOURCE_CONTEXT>` (if provided). Use this ONLY for understanding plot/terms. DO NOT translate it.
[Target Batch]: See `<INPUT>`. This is the ONLY text you must translate and output.

# CRITICAL FORMATTING RULES (Non-Negotiable)
1. **Absolute 1-to-1 Mapping**:
   - Count the input blocks. The output MUST have the EXACT same number of blocks.
   - If input has 32 blocks, output MUST have 32 blocks.
2. **Immutable Metadata**:
   - Copy Index Numbers (1, 2, 3...) exactly.
   - Copy Timestamps (00:00:00,000 --> ...) exactly.
   - Preserve the exact blank line structure between blocks.
3. **Segmentation Logic (Crucial)**:
   - **Do NOT merge lines**: If a sentence spans across Block A and Block B in the source, the translation MUST also span across Block A and Block B.
   - **Do NOT split lines**: Never create new blocks.
   - **Fragment Mapping**: If a source line is a sentence fragment (e.g., "I went to..."), translate it as a corresponding fragment in Chinese that flows logically into the next line. Do not force it into a complete sentence if that destroys the flow with the next block.
4. **Safety & Fallback**:
   - Never output empty text. If a line is untranslatable (sound effects, symbols), keep the original text or a standardized equivalent in Chinese.
   - Do not include `<html>` tags, font codes, or brackets like `(music)` unless they exist in the source.

# TRANSLATION STYLE GUIDELINES
1. **Natural & Conversational**: Use the target language's spoken logic. Avoid "Translationese" or stiff formal grammar.
2. **Concise**: Subtitles must be readable quickly. Choose shorter synonyms if they convey the same meaning/emotion.
3. **Contextual Flow**: Ensure the translation fits the Global Context (gender, tone, plot) but stays strictly within the time-bound limitations of the specific batch.

{context_block}

# OUTPUT FORMAT
Return the result in a plaintext code block.

# Actual Task
Translate the following batch into Chinese:

<INPUT>
{input_block}
</INPUT>
"""


@dataclass(frozen=True)
class SubtitleTrack:
    source: str
    language: str


@dataclass
class SubtitleEntry:
    index: int
    timing: str
    text: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Render a YouTube video with Simplified Chinese hard subtitles."
    )
    parser.add_argument("url", nargs="?", help="YouTube video URL")
    parser.add_argument(
        "--output-dir",
        default=DEFAULT_OUTPUT_DIR,
        help=f"Directory where a new work directory will be created (default: {DEFAULT_OUTPUT_DIR})",
    )
    parser.add_argument(
        "--work-dir",
        help="Existing work directory to resume from, or target directory for a fresh run",
    )
    parser.add_argument(
        "--proxy",
        default=DEFAULT_PROXY,
        help=f"Proxy passed to yt-dlp --proxy (default: {DEFAULT_PROXY})",
    )
    parser.add_argument(
        "--cookies-from-browser",
        default=DEFAULT_COOKIES_BROWSER,
        help=(
            "Browser profile passed to yt-dlp --cookies-from-browser "
            f"(default: {DEFAULT_COOKIES_BROWSER}; use 'none' to disable)"
        ),
    )
    parser.add_argument(
        "--skip-download",
        action="store_true",
        help="Reuse the existing source video and English subtitle in --work-dir",
    )
    parser.add_argument(
        "--skip-translate",
        action="store_true",
        help="Reuse the existing source.zh.srt in --work-dir",
    )
    parser.add_argument(
        "--translation-model",
        default=DEFAULT_MODEL,
        help=f"OpenAI model used for subtitle translation (default: {DEFAULT_MODEL})",
    )
    parser.add_argument(
        "--translation-backend",
        choices=["auto", "codex", "openai"],
        default="auto",
        help="Subtitle translation backend: prefer codex when available, otherwise OpenAI API",
    )
    parser.add_argument(
        "--api-base",
        default=DEFAULT_API_BASE,
        help=f"OpenAI-compatible API base URL (default: {DEFAULT_API_BASE})",
    )
    parser.add_argument(
        "--api-key",
        default=os.environ.get("OPENAI_API_KEY"),
        help="OpenAI API key; defaults to OPENAI_API_KEY",
    )
    parser.add_argument(
        "--chunk-size",
        type=int,
        default=40,
        help="Maximum subtitle items per translation request",
    )
    parser.add_argument(
        "--chunk-chars",
        type=int,
        default=12000,
        help="Maximum source characters per translation request",
    )
    parser.add_argument(
        "--font-name",
        default=DEFAULT_FONT,
        help=f"Subtitle font name passed to ffmpeg/libass (default: {DEFAULT_FONT})",
    )
    parser.add_argument(
        "--font-size",
        type=int,
        default=18,
        help="Subtitle font size for ffmpeg/libass",
    )
    parser.add_argument(
        "--crf",
        type=int,
        default=21,
        help="ffmpeg CRF value for the output video",
    )
    parser.add_argument(
        "--preset",
        default="medium",
        help="ffmpeg x264 preset for the output video",
    )
    args = parser.parse_args()

    if not args.skip_download and not args.url:
        parser.error("url is required unless --skip-download is set")
    if (args.skip_download or args.skip_translate) and not args.work_dir:
        parser.error("--work-dir is required when using --skip-download or --skip-translate")
    if args.chunk_size <= 0:
        parser.error("--chunk-size must be greater than 0")
    if args.chunk_chars <= 0:
        parser.error("--chunk-chars must be greater than 0")
    return args


def ensure_binary(name: str) -> None:
    if shutil.which(name):
        return
    raise RuntimeError(f"Missing required binary: {name}")


def run(cmd: list[str], cwd: Path | None = None, capture_output: bool = False) -> subprocess.CompletedProcess[str]:
    printable = " ".join(cmd)
    print(f"[run] {printable}", file=sys.stderr)
    return subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        text=True,
        capture_output=capture_output,
        check=True,
    )


def sanitize_slug(value: str) -> str:
    value = value.strip()
    value = re.sub(r"[^\w.-]+", "-", value, flags=re.UNICODE)
    value = re.sub(r"-{2,}", "-", value).strip("-._")
    return value or "video"


def resolve_path(value: str) -> Path:
    return Path(value).expanduser().resolve()


def normalize_optional_arg(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    if not stripped or stripped.lower() in {"none", "false", "no"}:
        return None
    return stripped


def prepare_work_dir(output_dir: Path, metadata: dict, explicit_dir: Path | None) -> Path:
    if explicit_dir:
        explicit_dir.mkdir(parents=True, exist_ok=True)
        return explicit_dir

    title = sanitize_slug(metadata.get("title") or "video")
    video_id = sanitize_slug(metadata.get("id") or "unknown")
    base = output_dir / f"{title}-{video_id}"
    candidate = base
    index = 2
    while candidate.exists():
        candidate = output_dir / f"{base.name}-{index}"
        index += 1
    candidate.mkdir(parents=True, exist_ok=False)
    return candidate


def add_yt_dlp_network_args(
    cmd: list[str],
    proxy: str | None,
    cookies_from_browser: str | None,
) -> None:
    if proxy:
        cmd.extend(["--proxy", proxy])
    if cookies_from_browser:
        cmd.extend(["--cookies-from-browser", cookies_from_browser])


def fetch_metadata(url: str, proxy: str | None, cookies_from_browser: str | None) -> dict:
    cmd = ["yt-dlp", "--dump-single-json", "--no-playlist"]
    add_yt_dlp_network_args(cmd, proxy, cookies_from_browser)
    cmd.append(url)
    result = run(cmd, capture_output=True)
    return json.loads(result.stdout)


def choose_english_track(metadata: dict) -> SubtitleTrack:
    def rank_language(language: str) -> tuple[int, str]:
        language = language.lower()
        if language == "en":
            return (0, language)
        if language in {"en-us", "en-gb"}:
            return (1, language)
        if language.startswith("en-"):
            return (2, language)
        if language.startswith("en"):
            return (3, language)
        return (99, language)

    subtitles = metadata.get("subtitles") or {}
    automatic = metadata.get("automatic_captions") or {}

    manual_candidates = sorted(
        (lang for lang in subtitles if lang.lower().startswith("en")),
        key=rank_language,
    )
    if manual_candidates:
        return SubtitleTrack(source="manual", language=manual_candidates[0])

    auto_candidates = sorted(
        (lang for lang in automatic if lang.lower().startswith("en")),
        key=rank_language,
    )
    if auto_candidates:
        return SubtitleTrack(source="auto", language=auto_candidates[0])

    raise RuntimeError("No English subtitles or English auto-captions found for this video")


def save_metadata(work_dir: Path, metadata: dict) -> None:
    metadata_path = work_dir / "video_metadata.json"
    metadata_path.write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def load_saved_metadata(work_dir: Path) -> dict:
    metadata_path = work_dir / "video_metadata.json"
    if not metadata_path.exists():
        return {}
    return json.loads(metadata_path.read_text(encoding="utf-8"))


def download_assets(
    url: str,
    work_dir: Path,
    track: SubtitleTrack,
    proxy: str | None,
    cookies_from_browser: str | None,
) -> tuple[Path, Path]:
    cmd = [
        "yt-dlp",
        "--no-playlist",
        "--merge-output-format",
        "mp4",
        "--output",
        "source.%(ext)s",
    ]
    add_yt_dlp_network_args(cmd, proxy, cookies_from_browser)
    if track.source == "manual":
        cmd.append("--write-sub")
    else:
        cmd.append("--write-auto-sub")
    cmd.extend(
        [
            "--sub-langs",
            track.language,
            "--sub-format",
            "srt/best",
            "--convert-subs",
            "srt",
            url,
        ]
    )
    run(cmd, cwd=work_dir)
    return find_video_path(work_dir), find_english_subtitle_path(work_dir, track.language)


def find_video_path(work_dir: Path) -> Path:
    candidates = [
        path
        for path in work_dir.iterdir()
        if path.is_file()
        and path.suffix.lower() in VIDEO_EXTENSIONS
        and not path.name.endswith(".part")
    ]
    if not candidates:
        raise RuntimeError("Downloaded video file not found in work directory")
    return max(candidates, key=lambda item: item.stat().st_size)


def find_english_subtitle_path(work_dir: Path, preferred_language: str | None = None) -> Path:
    candidates = sorted(path for path in work_dir.iterdir() if path.suffix.lower() == ".srt")
    if not candidates:
        raise RuntimeError("English subtitle file not found in work directory")

    def score(path: Path) -> tuple[int, str]:
        name = path.name.lower()
        preferred = (preferred_language or "").lower()
        if path.name == "source.en.srt":
            return (0, name)
        if preferred and name == f"source.{preferred}.srt":
            return (1, name)
        if preferred and name.startswith(f"source.{preferred}."):
            return (2, name)
        if ".en." in name:
            return (3, name)
        if name.startswith("source.en"):
            return (4, name)
        return (99, name)

    best = min(candidates, key=score)
    if score(best)[0] >= 99:
        raise RuntimeError("Could not determine which subtitle file is the English subtitle track")
    return best


def parse_srt_text(raw: str) -> list[SubtitleEntry]:
    chunks = re.split(r"\r?\n\r?\n+", raw.strip())
    entries: list[SubtitleEntry] = []
    for chunk in chunks:
        lines = chunk.splitlines()
        if len(lines) < 3:
            continue
        if "-->" not in lines[1]:
            continue
        try:
            index = int(lines[0].strip())
        except ValueError:
            index = len(entries) + 1
        timing = lines[1].strip()
        text = "\n".join(line.rstrip() for line in lines[2:]).strip()
        entries.append(SubtitleEntry(index=index, timing=timing, text=text))
    if not entries:
        raise RuntimeError("Failed to parse subtitle entries")
    return entries


def parse_srt(path: Path) -> list[SubtitleEntry]:
    return parse_srt_text(path.read_text(encoding="utf-8-sig"))


def write_srt(path: Path, entries: Iterable[SubtitleEntry]) -> None:
    parts: list[str] = []
    for index, entry in enumerate(entries, start=1):
        parts.append(str(index))
        parts.append(entry.timing)
        parts.append(entry.text.strip())
        parts.append("")
    path.write_text("\n".join(parts).rstrip() + "\n", encoding="utf-8")


def chunk_entries(entries: list[SubtitleEntry], chunk_size: int, chunk_chars: int) -> list[list[SubtitleEntry]]:
    chunks: list[list[SubtitleEntry]] = []
    current: list[SubtitleEntry] = []
    current_chars = 0
    for entry in entries:
        entry_chars = len(entry.text)
        limit_reached = (
            current
            and (len(current) >= chunk_size or current_chars + entry_chars > chunk_chars)
        )
        if limit_reached:
            chunks.append(current)
            current = []
            current_chars = 0
        current.append(entry)
        current_chars += entry_chars
    if current:
        chunks.append(current)
    return chunks


def entries_to_srt_text(entries: Iterable[SubtitleEntry]) -> str:
    parts: list[str] = []
    for entry in entries:
        parts.append(str(entry.index))
        parts.append(entry.timing)
        parts.append(entry.text.strip())
        parts.append("")
    return "\n".join(parts).rstrip() + "\n"


def extract_json_object(text: str) -> dict:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start == -1 or end == -1 or start >= end:
            raise
        return json.loads(cleaned[start : end + 1])


def extract_code_block(text: str) -> str:
    matches = re.findall(r"```(?:text|plaintext)?\n(.*?)```", text, flags=re.S)
    if matches:
        return matches[-1].strip()
    return text.strip()


def request_translation(items: list[dict], api_key: str, model: str, api_base: str) -> list[str]:
    user_prompt = {
        "target_language": "Simplified Chinese",
        "items": items,
    }
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(user_prompt, ensure_ascii=False)},
        ],
    }
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        f"{api_base.rstrip('/')}/chat/completions",
        data=body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        details = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Translation API request failed: {exc.code} {details}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Translation API request failed: {exc}") from exc

    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError(f"Unexpected translation API response: {json.dumps(data)}") from exc

    parsed = extract_json_object(content)
    translated_items = parsed.get("items")
    if not isinstance(translated_items, list):
        raise RuntimeError(f"Translation payload did not contain an items list: {content}")

    output: list[str] = []
    for item in translated_items:
        if not isinstance(item, dict) or "translated" not in item:
            raise RuntimeError(f"Malformed translated item: {item!r}")
        output.append(str(item["translated"]).strip())
    return output


def build_codex_context_block(metadata: dict) -> str:
    lines = ["[Global Context Summary]:"]
    title = metadata.get("title")
    if title:
        lines.append(f"- Video title: {title}")
    channel = metadata.get("channel")
    if channel:
        lines.append(f"- Channel: {channel}")
    if metadata.get("categories"):
        lines.append(f"- Topic: {', '.join(metadata['categories'])}")
    elif title:
        lines.append("- Topic: instructional or spoken YouTube content; keep terms consistent with the title.")
    description = metadata.get("description")
    if description:
        summary = re.sub(r"\s+", " ", description).strip()
        if summary:
            lines.append(f"- Description hint: {summary[:240]}")
    return "\n".join(lines)


def validate_translated_entries(
    source_entries: list[SubtitleEntry],
    translated_entries: list[SubtitleEntry],
) -> None:
    if len(source_entries) != len(translated_entries):
        raise RuntimeError(
            f"Translated item count mismatch: expected {len(source_entries)}, got {len(translated_entries)}"
        )
    for source_entry, translated_entry in zip(source_entries, translated_entries, strict=True):
        if source_entry.index != translated_entry.index:
            raise RuntimeError(
                f"Translated subtitle index mismatch: expected {source_entry.index}, got {translated_entry.index}"
            )
        if source_entry.timing != translated_entry.timing:
            raise RuntimeError(
                f"Translated subtitle timing mismatch: expected {source_entry.timing}, got {translated_entry.timing}"
            )
        if not translated_entry.text.strip():
            raise RuntimeError(f"Translated subtitle text is empty for block {translated_entry.index}")


def request_codex_translation(
    entries: list[SubtitleEntry],
    work_dir: Path,
    metadata: dict,
) -> list[SubtitleEntry]:
    if not shutil.which("codex"):
        raise RuntimeError("codex CLI is required for the codex translation backend")

    prompt = CODEX_TRANSLATION_PROMPT_TEMPLATE.format(
        context_block=build_codex_context_block(metadata),
        input_block=entries_to_srt_text(entries).rstrip(),
    )
    with tempfile.NamedTemporaryFile(
        mode="w+",
        encoding="utf-8",
        suffix=".txt",
        dir=work_dir,
        delete=False,
    ) as output_file:
        output_path = Path(output_file.name)

    cmd = [
        "codex",
        "exec",
        "--skip-git-repo-check",
        "--sandbox",
        "read-only",
        "--color",
        "never",
        "-c",
        'model_reasoning_effort="low"',
        "-o",
        output_path.name,
        "-",
    ]
    printable = " ".join(cmd)
    print(f"[run] {printable}", file=sys.stderr)
    try:
        result = subprocess.run(
            cmd,
            cwd=str(work_dir),
            input=prompt,
            text=True,
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            details = (result.stderr or result.stdout or "").strip()
            raise RuntimeError(f"codex translation backend failed: {details}")
        message = output_path.read_text(encoding="utf-8")
    finally:
        output_path.unlink(missing_ok=True)

    translated_text = extract_code_block(message)
    translated_entries = parse_srt_text(translated_text)
    validate_translated_entries(entries, translated_entries)
    return translated_entries


def choose_translation_backend(requested_backend: str, api_key: str | None) -> str:
    if requested_backend == "codex":
        if not shutil.which("codex"):
            raise RuntimeError("codex CLI is not available")
        return "codex"
    if requested_backend == "openai":
        if not api_key:
            raise RuntimeError("OPENAI_API_KEY is required for the openai translation backend")
        return "openai"
    if shutil.which("codex"):
        return "codex"
    if api_key:
        return "openai"
    raise RuntimeError("No translation backend available: install codex or set OPENAI_API_KEY")


def translate_subtitles(
    entries: list[SubtitleEntry],
    api_key: str,
    model: str,
    api_base: str,
    chunk_size: int,
    chunk_chars: int,
    translation_backend: str,
    work_dir: Path,
    metadata: dict,
) -> list[SubtitleEntry]:
    backend = choose_translation_backend(translation_backend, api_key)
    translated_entries: list[SubtitleEntry] = []
    chunks = chunk_entries(entries, chunk_size, chunk_chars)
    for index, chunk in enumerate(chunks, start=1):
        print(
            f"[translate] chunk {index}/{len(chunks)} with {len(chunk)} subtitle items via {backend}",
            file=sys.stderr,
        )
        if backend == "openai":
            items = [{"id": entry.index, "text": entry.text} for entry in chunk]
            translated_texts = request_translation(items, api_key, model, api_base)
            if len(translated_texts) != len(chunk):
                raise RuntimeError(
                    f"Translated item count mismatch: expected {len(chunk)}, got {len(translated_texts)}"
                )
            for entry, translated_text in zip(chunk, translated_texts, strict=True):
                translated_entries.append(
                    SubtitleEntry(
                        index=entry.index,
                        timing=entry.timing,
                        text=translated_text or entry.text,
                    )
                )
        else:
            translated_entries.extend(request_codex_translation(chunk, work_dir, metadata))
    return translated_entries


def build_force_style(font_name: str, font_size: int) -> str:
    safe_font = font_name.replace("'", "")
    return (
        f"FontName={safe_font},FontSize={font_size},Alignment=2,"
        "PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,"
        "BorderStyle=1,Outline=2,Shadow=0,MarginV=28"
    )


def burn_subtitles(
    work_dir: Path,
    video_path: Path,
    subtitle_path: Path,
    output_path: Path,
    font_name: str,
    font_size: int,
    crf: int,
    preset: str,
) -> None:
    force_style = build_force_style(font_name, font_size)
    run(
        [
            "ffmpeg",
            "-y",
            "-i",
            video_path.name,
            "-vf",
            f"subtitles={subtitle_path.name}:force_style={force_style}",
            "-c:v",
            "libx264",
            "-preset",
            preset,
            "-crf",
            str(crf),
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-movflags",
            "+faststart",
            output_path.name,
        ],
        cwd=work_dir,
    )


def main() -> int:
    args = parse_args()
    ensure_binary("yt-dlp")
    ensure_binary("ffmpeg")

    explicit_work_dir = resolve_path(args.work_dir) if args.work_dir else None
    output_dir = resolve_path(args.output_dir)
    proxy = normalize_optional_arg(args.proxy)
    cookies_from_browser = normalize_optional_arg(args.cookies_from_browser)
    metadata = None

    if args.skip_download:
        work_dir = explicit_work_dir
        assert work_dir is not None
        work_dir.mkdir(parents=True, exist_ok=True)
    else:
        assert args.url is not None
        metadata = fetch_metadata(args.url, proxy, cookies_from_browser)
        output_dir.mkdir(parents=True, exist_ok=True)
        work_dir = prepare_work_dir(output_dir, metadata, explicit_work_dir)
        save_metadata(work_dir, metadata)
        track = choose_english_track(metadata)
        print(
            f"[subtitle] using {track.source} English subtitles: {track.language}",
            file=sys.stderr,
        )
        download_assets(args.url, work_dir, track, proxy, cookies_from_browser)

    metadata = metadata or load_saved_metadata(work_dir)

    video_path = find_video_path(work_dir)
    english_srt = find_english_subtitle_path(work_dir)
    chinese_srt = work_dir / "source.zh.srt"
    output_path = work_dir / "output.zh.hardsub.mp4"

    if args.skip_translate:
        if not chinese_srt.exists():
            raise RuntimeError(f"Expected translated subtitle file not found: {chinese_srt}")
    else:
        english_entries = parse_srt(english_srt)
        chinese_entries = translate_subtitles(
            english_entries,
            api_key=args.api_key,
            model=args.translation_model,
            api_base=args.api_base,
            chunk_size=args.chunk_size,
            chunk_chars=args.chunk_chars,
            translation_backend=args.translation_backend,
            work_dir=work_dir,
            metadata=metadata,
        )
        write_srt(chinese_srt, chinese_entries)

    burn_subtitles(
        work_dir=work_dir,
        video_path=video_path,
        subtitle_path=chinese_srt,
        output_path=output_path,
        font_name=args.font_name,
        font_size=args.font_size,
        crf=args.crf,
        preset=args.preset,
    )

    print(str(output_path.resolve()))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except subprocess.CalledProcessError as exc:
        print(exc.stderr or str(exc), file=sys.stderr)
        raise SystemExit(exc.returncode)
    except Exception as exc:  # pragma: no cover
        print(f"[error] {exc}", file=sys.stderr)
        raise SystemExit(1)
