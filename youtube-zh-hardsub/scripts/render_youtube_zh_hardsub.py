#!/usr/bin/env python3
"""
Download a YouTube video, translate English subtitles to Simplified Chinese,
and burn the translated subtitles into a hard-subbed MP4.
"""

from __future__ import annotations

import argparse
import html
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
from functools import lru_cache
from pathlib import Path
from typing import Iterable


VIDEO_EXTENSIONS = {".mp4", ".mkv", ".webm", ".mov", ".m4v", ".avi"}
DEFAULT_MODEL = os.environ.get("OPENAI_TRANSLATION_MODEL", "gpt-4.1-mini")
DEFAULT_API_BASE = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")
DEFAULT_FONT = os.environ.get("YOUTUBE_ZH_HARDSUB_FONT", "Noto Sans CJK SC")
DEFAULT_OUTPUT_DIR = str(Path.home() / "Videos")
DEFAULT_PROXY = "http://127.0.0.1:7890"
DEFAULT_COOKIES_BROWSER = "firefox"
TIMING_PATTERN = re.compile(
    r"(?P<start>\d{2}:\d{2}:\d{2},\d{3})\s*-->\s*(?P<end>\d{2}:\d{2}:\d{2},\d{3})"
)
VTT_TIMING_PATTERN = re.compile(
    r"(?P<start>(?:\d{2}:)?\d{2}:\d{2}\.\d{3})\s*-->\s*(?P<end>(?:\d{2}:)?\d{2}:\d{2}\.\d{3})"
)
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


@dataclass
class SubtitleCandidateResult:
    format_name: str
    path: Path
    issue_count: int
    overlap_count: int


@dataclass(frozen=True)
class EncoderPlan:
    family: str
    codec: str
    encoder: str
    label: str


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
        help="Fallback quality target when source bitrate probing is unavailable",
    )
    parser.add_argument(
        "--preset",
        default="medium",
        help="ffmpeg x264 preset for CPU mode",
    )
    parser.add_argument(
        "--video-encoder",
        choices=["auto", "nvidia", "cpu"],
        default="auto",
        help="Burn-step video encoder: auto prefers NVIDIA NVENC when available",
    )
    parser.add_argument(
        "--use-nvidia",
        action="store_true",
        help=argparse.SUPPRESS,
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
    if args.use_nvidia:
        args.video_encoder = "nvidia"
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


def parse_srt_timestamp(value: str) -> int:
    hours, minutes, seconds_and_ms = value.split(":")
    seconds, milliseconds = seconds_and_ms.split(",")
    return (
        int(hours) * 3_600_000
        + int(minutes) * 60_000
        + int(seconds) * 1_000
        + int(milliseconds)
    )


def format_srt_timestamp(value: int) -> str:
    value = max(value, 0)
    hours, remainder = divmod(value, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, milliseconds = divmod(remainder, 1_000)
    return f"{hours:02}:{minutes:02}:{seconds:02},{milliseconds:03}"


def parse_timing_range(timing: str) -> tuple[int, int]:
    match = TIMING_PATTERN.fullmatch(timing.strip())
    if not match:
        raise RuntimeError(f"Invalid SRT timing line: {timing!r}")
    return (
        parse_srt_timestamp(match.group("start")),
        parse_srt_timestamp(match.group("end")),
    )


def format_timing_range(start_ms: int, end_ms: int) -> str:
    return f"{format_srt_timestamp(start_ms)} --> {format_srt_timestamp(end_ms)}"


def parse_vtt_timestamp(value: str) -> int:
    parts = value.split(":")
    if len(parts) == 2:
        hours = 0
        minutes, seconds_and_ms = parts
    elif len(parts) == 3:
        hours, minutes, seconds_and_ms = parts
    else:
        raise RuntimeError(f"Invalid VTT timestamp: {value!r}")
    seconds, milliseconds = seconds_and_ms.split(".")
    return (
        int(hours) * 3_600_000
        + int(minutes) * 60_000
        + int(seconds) * 1_000
        + int(milliseconds)
    )


def backup_file_once(path: Path) -> Path:
    backup_path = path.with_name(f"{path.name}.bak")
    if not backup_path.exists():
        shutil.copy2(path, backup_path)
    return backup_path


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


def download_video(
    url: str,
    work_dir: Path,
    proxy: str | None,
    cookies_from_browser: str | None,
) -> Path:
    cmd = [
        "yt-dlp",
        "--no-playlist",
        "--merge-output-format",
        "mp4",
        "--output",
        "source.%(ext)s",
    ]
    add_yt_dlp_network_args(cmd, proxy, cookies_from_browser)
    cmd.append(url)
    run(cmd, cwd=work_dir)
    return find_video_path(work_dir)


def cleanup_subtitle_candidates(work_dir: Path, prefix: str) -> None:
    for path in work_dir.iterdir():
        if not path.is_file():
            continue
        if path.name == prefix or path.name.startswith(f"{prefix}."):
            path.unlink(missing_ok=True)


def find_candidate_file_path(
    work_dir: Path,
    prefix: str,
    suffixes: set[str],
    preferred_language: str | None = None,
) -> Path:
    candidates = sorted(
        path
        for path in work_dir.iterdir()
        if path.is_file()
        and path.suffix.lower() in suffixes
        and (path.name == prefix or path.name.startswith(f"{prefix}."))
    )
    if not candidates:
        suffix_label = ", ".join(sorted(suffixes))
        raise RuntimeError(f"Candidate file not found for prefix {prefix!r} with suffixes {suffix_label}")

    def score(path: Path) -> tuple[int, str]:
        name = path.name.lower()
        preferred = (preferred_language or "").lower()
        if preferred and name == f"{prefix}.{preferred}.srt":
            return (0, name)
        if preferred and name.startswith(f"{prefix}.{preferred}."):
            return (1, name)
        if name == f"{prefix}.en.srt":
            return (2, name)
        if ".en." in name or name.startswith(f"{prefix}.en"):
            return (3, name)
        return (99, name)

    best = min(candidates, key=score)
    if score(best)[0] >= 99:
        raise RuntimeError(f"Could not determine subtitle candidate for prefix {prefix!r}")
    return best


def inspect_subtitle_candidate(path: Path) -> tuple[int, int]:
    entries = parse_srt(path)
    _, overlap_count, changed_count = normalize_subtitle_timings(entries)
    return overlap_count, changed_count


def download_subtitle_candidate(
    url: str,
    work_dir: Path,
    track: SubtitleTrack,
    proxy: str | None,
    cookies_from_browser: str | None,
    prefix: str,
    sub_format: str,
) -> SubtitleCandidateResult:
    cleanup_subtitle_candidates(work_dir, prefix)

    cmd = [
        "yt-dlp",
        "--skip-download",
        "--no-playlist",
        "--output",
        f"{prefix}.%(ext)s",
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
            sub_format,
            "--convert-subs",
            "srt",
            url,
        ]
    )
    run(cmd, cwd=work_dir)
    subtitle_path = find_candidate_file_path(work_dir, prefix, {".srt"}, track.language)
    overlap_count, issue_count = inspect_subtitle_candidate(subtitle_path)
    print(
        f"[subtitle] {sub_format} produced {subtitle_path.name} "
        f"with {issue_count} timing issues ({overlap_count} overlaps)",
        file=sys.stderr,
    )
    return SubtitleCandidateResult(
        format_name=sub_format,
        path=subtitle_path,
        issue_count=issue_count,
        overlap_count=overlap_count,
    )


def download_raw_subtitle_candidate(
    url: str,
    work_dir: Path,
    track: SubtitleTrack,
    proxy: str | None,
    cookies_from_browser: str | None,
    prefix: str,
    sub_format: str,
    suffix: str,
) -> Path:
    cleanup_subtitle_candidates(work_dir, prefix)

    cmd = [
        "yt-dlp",
        "--skip-download",
        "--no-playlist",
        "--output",
        f"{prefix}.%(ext)s",
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
            sub_format,
            url,
        ]
    )
    run(cmd, cwd=work_dir)
    return find_candidate_file_path(work_dir, prefix, {suffix}, track.language)


def download_structured_subtitle_candidate(
    url: str,
    work_dir: Path,
    track: SubtitleTrack,
    proxy: str | None,
    cookies_from_browser: str | None,
    prefix: str,
    sub_format: str,
) -> SubtitleCandidateResult:
    parser_map = {
        "json3": parse_json3_text,
        "vtt": parse_vtt_text,
    }
    suffix_map = {
        "json3": ".json3",
        "vtt": ".vtt",
    }
    if sub_format not in parser_map:
        raise RuntimeError(f"Unsupported structured subtitle format: {sub_format}")

    raw_path = download_raw_subtitle_candidate(
        url=url,
        work_dir=work_dir,
        track=track,
        proxy=proxy,
        cookies_from_browser=cookies_from_browser,
        prefix=prefix,
        sub_format=sub_format,
        suffix=suffix_map[sub_format],
    )
    raw_text = raw_path.read_text(encoding="utf-8-sig")
    entries = parser_map[sub_format](raw_text)
    _, overlap_count, issue_count = normalize_subtitle_timings(entries)
    subtitle_path = work_dir / f"{prefix}.en.srt"
    write_srt(subtitle_path, entries)
    print(
        f"[subtitle] {sub_format} produced {subtitle_path.name} "
        f"with {issue_count} timing issues ({overlap_count} overlaps)",
        file=sys.stderr,
    )
    return SubtitleCandidateResult(
        format_name=sub_format,
        path=subtitle_path,
        issue_count=issue_count,
        overlap_count=overlap_count,
    )


def download_english_subtitle(
    url: str,
    work_dir: Path,
    track: SubtitleTrack,
    proxy: str | None,
    cookies_from_browser: str | None,
) -> Path:
    successful_results: list[SubtitleCandidateResult] = []
    try:
        srt_result = download_subtitle_candidate(
            url=url,
            work_dir=work_dir,
            track=track,
            proxy=proxy,
            cookies_from_browser=cookies_from_browser,
            prefix="subtitle_srtbest",
            sub_format="srt/best",
        )
        successful_results.append(srt_result)
    except Exception as exc:
        print(f"[subtitle] srt/best download failed: {exc}", file=sys.stderr)
        srt_result = None

    if srt_result and srt_result.issue_count == 0:
        print("[subtitle] selected clean subtitle candidate from srt/best", file=sys.stderr)
    else:
        for prefix, sub_format in [("subtitle_json3", "json3"), ("subtitle_vtt", "vtt")]:
            try:
                result = download_structured_subtitle_candidate(
                    url=url,
                    work_dir=work_dir,
                    track=track,
                    proxy=proxy,
                    cookies_from_browser=cookies_from_browser,
                    prefix=prefix,
                    sub_format=sub_format,
                )
            except Exception as exc:
                print(f"[subtitle] {sub_format} download failed: {exc}", file=sys.stderr)
                continue
            successful_results.append(result)
            if result.issue_count == 0:
                print(
                    f"[subtitle] selected clean subtitle candidate from {sub_format}",
                    file=sys.stderr,
                )
                break

    if not successful_results:
        raise RuntimeError("Failed to download any usable English subtitle candidate")

    selected_result = min(
        successful_results,
        key=lambda item: (item.issue_count, item.overlap_count, item.format_name != "srt/best"),
    )
    final_subtitle_path = work_dir / "source.en.srt"
    if selected_result.path.resolve() != final_subtitle_path.resolve():
        shutil.copy2(selected_result.path, final_subtitle_path)
    if selected_result.issue_count > 0:
        print(
            f"[subtitle] no clean candidate found; using {selected_result.format_name} "
            f"and repairing {selected_result.issue_count} timing issues locally",
            file=sys.stderr,
        )
    return final_subtitle_path


def download_assets(
    url: str,
    work_dir: Path,
    track: SubtitleTrack,
    proxy: str | None,
    cookies_from_browser: str | None,
) -> tuple[Path, Path]:
    video_path = download_video(url, work_dir, proxy, cookies_from_browser)
    subtitle_path = download_english_subtitle(url, work_dir, track, proxy, cookies_from_browser)
    return video_path, subtitle_path


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

    def score(path: Path) -> tuple[int, int]:
        name = path.name.lower()
        if name.startswith("source."):
            return (0, -path.stat().st_size)
        if "hardsub" in name:
            return (2, -path.stat().st_size)
        return (1, -path.stat().st_size)

    return min(candidates, key=score)


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


def parse_vtt_text(raw: str) -> list[SubtitleEntry]:
    chunks = re.split(r"\r?\n\r?\n+", raw.strip())
    entries: list[SubtitleEntry] = []
    for chunk in chunks:
        lines = [line.rstrip("\r") for line in chunk.splitlines()]
        if not lines:
            continue
        if lines[0].strip().startswith("WEBVTT"):
            continue
        if lines[0].strip().startswith(("NOTE", "STYLE", "REGION")):
            continue

        timing_index = None
        for index, line in enumerate(lines[:2]):
            if "-->" in line:
                timing_index = index
                break
        if timing_index is None:
            continue

        timing_line = lines[timing_index].strip()
        match = VTT_TIMING_PATTERN.search(timing_line)
        if not match:
            continue

        start_ms = parse_vtt_timestamp(match.group("start"))
        end_ms = parse_vtt_timestamp(match.group("end"))
        text = "\n".join(line.rstrip() for line in lines[timing_index + 1 :] if line.strip()).strip()
        if not text:
            continue
        entries.append(
            SubtitleEntry(
                index=len(entries) + 1,
                timing=format_timing_range(start_ms, end_ms),
                text=text,
            )
        )

    if not entries:
        raise RuntimeError("Failed to parse WebVTT subtitle entries")
    return entries


def parse_json3_text(raw: str) -> list[SubtitleEntry]:
    data = json.loads(raw)
    events = data.get("events")
    if not isinstance(events, list):
        raise RuntimeError("json3 subtitle payload did not contain an events list")

    entries: list[SubtitleEntry] = []
    for event in events:
        if not isinstance(event, dict):
            continue
        segs = event.get("segs")
        if not isinstance(segs, list):
            continue
        text = "".join(str(seg.get("utf8", "")) for seg in segs if isinstance(seg, dict))
        text = html.unescape(text).replace("\xa0", " ").strip()
        if not text:
            continue

        start_ms = int(event.get("tStartMs", 0))
        duration_ms = int(event.get("dDurationMs", 0))
        end_ms = start_ms + max(duration_ms, 0)
        entries.append(
            SubtitleEntry(
                index=len(entries) + 1,
                timing=format_timing_range(start_ms, end_ms),
                text=text,
            )
        )

    if not entries:
        raise RuntimeError("Failed to parse json3 subtitle entries")
    return entries


def write_srt(path: Path, entries: Iterable[SubtitleEntry]) -> None:
    parts: list[str] = []
    for index, entry in enumerate(entries, start=1):
        parts.append(str(index))
        parts.append(entry.timing)
        parts.append(entry.text.strip())
        parts.append("")
    path.write_text("\n".join(parts).rstrip() + "\n", encoding="utf-8")


def normalize_subtitle_timings(
    entries: list[SubtitleEntry],
) -> tuple[list[SubtitleEntry], int, int]:
    normalized: list[SubtitleEntry] = []
    overlap_count = 0
    changed_count = 0

    parsed_ranges: list[tuple[int, int]] = []
    for entry in entries:
        start_ms, end_ms = parse_timing_range(entry.timing)
        if end_ms < start_ms:
            end_ms = start_ms
        parsed_ranges.append((start_ms, end_ms))

    adjusted_ranges = list(parsed_ranges)
    for index in range(len(adjusted_ranges) - 1):
        current_start, current_end = adjusted_ranges[index]
        next_start, next_end = adjusted_ranges[index + 1]
        if next_start < current_start:
            next_start = current_start
            if next_end < next_start:
                next_end = next_start
            adjusted_ranges[index + 1] = (next_start, next_end)
        if current_end > next_start:
            overlap_count += 1
            current_end = next_start
            if current_end < current_start:
                current_end = current_start
            adjusted_ranges[index] = (current_start, current_end)

    for entry, (start_ms, end_ms), original_range in zip(
        entries,
        adjusted_ranges,
        parsed_ranges,
        strict=True,
    ):
        timing = format_timing_range(start_ms, end_ms)
        if (start_ms, end_ms) != original_range:
            changed_count += 1
        normalized.append(
            SubtitleEntry(
                index=entry.index,
                timing=timing,
                text=entry.text,
            )
        )

    return normalized, overlap_count, changed_count


def maybe_rewrite_subtitle_file(
    path: Path,
    original_entries: list[SubtitleEntry],
    updated_entries: list[SubtitleEntry],
    reason: str,
) -> None:
    if len(original_entries) != len(updated_entries):
        raise RuntimeError(
            f"Subtitle rewrite mismatch for {path.name}: "
            f"expected {len(original_entries)} entries, got {len(updated_entries)}"
        )
    changed_count = sum(
        1
        for original, updated in zip(original_entries, updated_entries, strict=True)
        if original.index != updated.index
        or original.timing != updated.timing
        or original.text != updated.text
    )
    if not changed_count:
        return
    backup_path = backup_file_once(path)
    write_srt(path, updated_entries)
    print(
        f"[subtitle] {reason}: updated {changed_count} blocks in {path.name} "
        f"(backup: {backup_path.name})",
        file=sys.stderr,
    )


def normalize_subtitle_file(path: Path) -> list[SubtitleEntry]:
    entries = parse_srt(path)
    normalized_entries, overlap_count, changed_count = normalize_subtitle_timings(entries)
    if changed_count:
        maybe_rewrite_subtitle_file(
            path,
            entries,
            normalized_entries,
            reason=f"repaired {overlap_count} overlapping timing ranges",
        )
    return normalized_entries


def synchronize_subtitle_timings(
    entries: list[SubtitleEntry],
    reference_entries: list[SubtitleEntry],
) -> list[SubtitleEntry]:
    if len(entries) != len(reference_entries):
        raise RuntimeError(
            f"Subtitle timing sync failed: expected {len(reference_entries)} entries, got {len(entries)}"
        )
    return [
        SubtitleEntry(
            index=reference_entry.index,
            timing=reference_entry.timing,
            text=entry.text,
        )
        for entry, reference_entry in zip(entries, reference_entries, strict=True)
    ]


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


def quote_ffmpeg_filter_value(value: str) -> str:
    escaped = value.replace("\\", r"\\").replace("'", r"\'")
    return f"'{escaped}'"


def probe_bitrate(path: Path, select_streams: str | None, entry: str) -> int | None:
    cmd = ["ffprobe", "-v", "error"]
    if select_streams:
        cmd.extend(["-select_streams", select_streams])
    cmd.extend(
        [
            "-show_entries",
            entry,
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            path.name,
        ]
    )
    result = run(cmd, cwd=path.parent, capture_output=True)
    value = result.stdout.strip()
    if not value or value == "N/A":
        return None
    try:
        bitrate = int(value)
    except ValueError:
        return None
    return bitrate if bitrate > 0 else None


def probe_source_bitrates(video_path: Path) -> tuple[int | None, int | None]:
    video_bitrate = probe_bitrate(video_path, "v:0", "stream=bit_rate")
    audio_bitrate = probe_bitrate(video_path, "a:0", "stream=bit_rate")
    format_bitrate = probe_bitrate(video_path, None, "format=bit_rate")

    if video_bitrate is None and format_bitrate and audio_bitrate:
        video_bitrate = max(format_bitrate - audio_bitrate, 1)
    if audio_bitrate is None and format_bitrate and video_bitrate and format_bitrate > video_bitrate:
        audio_bitrate = max(format_bitrate - video_bitrate, 1)
    return video_bitrate, audio_bitrate


def build_vbr_rate_args(target_bitrate: int) -> list[str]:
    maxrate = max(int(target_bitrate * 1.10), target_bitrate + 64_000)
    bufsize = max(target_bitrate * 2, 512_000)
    return [
        "-b:v",
        str(target_bitrate),
        "-maxrate",
        str(maxrate),
        "-bufsize",
        str(bufsize),
    ]


@lru_cache(maxsize=None)
def encoder_available(encoder_name: str) -> tuple[bool, str]:
    test_cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        "testsrc2=d=1:s=1280x720:r=30",
        "-an",
        "-c:v",
        encoder_name,
        "-f",
        "null",
        "-",
    ]
    result = subprocess.run(
        test_cmd,
        text=True,
        capture_output=True,
        check=False,
    )
    details = (result.stderr or result.stdout or "").strip()
    return result.returncode == 0, details


def resolve_video_encoder(requested: str) -> EncoderPlan:
    nvidia_candidates = [
        EncoderPlan("nvidia", "hevc", "hevc_nvenc", "NVIDIA NVENC H.265/HEVC"),
        EncoderPlan("nvidia", "av1", "av1_nvenc", "NVIDIA NVENC AV1"),
        EncoderPlan("nvidia", "h264", "h264_nvenc", "NVIDIA NVENC H.264"),
    ]
    cpu_candidates = [
        EncoderPlan("cpu", "hevc", "libx265", "CPU/libx265 H.265/HEVC"),
        EncoderPlan("cpu", "av1", "libsvtav1", "CPU/libsvtav1 AV1"),
        EncoderPlan("cpu", "h264", "libx264", "CPU/libx264 H.264"),
    ]

    if requested == "nvidia":
        candidates = nvidia_candidates
    elif requested == "cpu":
        candidates = cpu_candidates
    else:
        candidates = nvidia_candidates + cpu_candidates

    failures: list[str] = []
    for plan in candidates:
        available, details = encoder_available(plan.encoder)
        if available:
            print(
                f"[encode] using {plan.label} (codec priority: H.265 -> AV1 -> H.264)",
                file=sys.stderr,
            )
            return plan
        if details:
            failures.append(f"{plan.encoder}: {details}")

    if requested == "nvidia":
        raise RuntimeError(
            "Requested NVIDIA encoders, but none of hevc_nvenc, av1_nvenc, or h264_nvenc "
            f"could initialize: {' | '.join(failures) or 'unknown error'}"
        )
    if requested == "cpu":
        raise RuntimeError(
            "Requested CPU encoders, but none of libx265, libsvtav1, or libx264 "
            f"could initialize: {' | '.join(failures) or 'unknown error'}"
        )
    raise RuntimeError(
        "No usable video encoder found in priority order "
        "H.265 -> AV1 -> H.264: "
        f"{' | '.join(failures) or 'unknown error'}"
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
    video_encoder: str,
) -> None:
    force_style = build_force_style(font_name, font_size)
    subtitle_filter = (
        f"subtitles={quote_ffmpeg_filter_value(subtitle_path.name)}:"
        f"force_style={quote_ffmpeg_filter_value(force_style)}"
    )
    selected_encoder = resolve_video_encoder(video_encoder)
    source_video_bitrate, source_audio_bitrate = probe_source_bitrates(video_path)

    if source_video_bitrate:
        print(
            f"[encode] target video bitrate {source_video_bitrate} bps (matched from source)",
            file=sys.stderr,
        )
    else:
        print(
            f"[encode] source video bitrate unavailable; falling back to quality target {crf}",
            file=sys.stderr,
        )
    if source_audio_bitrate:
        print(
            f"[encode] target audio bitrate {source_audio_bitrate} bps (matched from source)",
            file=sys.stderr,
        )

    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        video_path.name,
        "-vf",
        subtitle_filter,
    ]
    cmd.extend(
        [
            "-c:v",
            selected_encoder.encoder,
        ]
    )
    if selected_encoder.family == "nvidia":
        cmd.extend(["-preset", "p5"])
        if source_video_bitrate:
            cmd.extend(["-rc", "vbr"])
            cmd.extend(build_vbr_rate_args(source_video_bitrate))
        else:
            cmd.extend(
                [
                    "-cq",
                    str(crf),
                    "-b:v",
                    "0",
                ]
            )
    else:
        if selected_encoder.encoder == "libsvtav1":
            cmd.extend(["-preset", "6"])
        else:
            cmd.extend(["-preset", preset])
        if source_video_bitrate:
            cmd.extend(build_vbr_rate_args(source_video_bitrate))
        else:
            cmd.extend(
                [
                    "-crf",
                    str(crf),
                ]
            )
    if selected_encoder.codec == "hevc":
        cmd.extend(["-tag:v", "hvc1"])
    cmd.extend(
        [
            "-c:a",
            "aac",
            "-b:a",
            str(source_audio_bitrate or 128_000),
            "-movflags",
            "+faststart",
            output_path.name,
        ]
    )
    run(
        cmd,
        cwd=work_dir,
    )


def main() -> int:
    args = parse_args()
    ensure_binary("yt-dlp")
    ensure_binary("ffmpeg")
    ensure_binary("ffprobe")

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
    english_entries = normalize_subtitle_file(english_srt)

    if args.skip_translate:
        if not chinese_srt.exists():
            raise RuntimeError(f"Expected translated subtitle file not found: {chinese_srt}")
        chinese_entries = parse_srt(chinese_srt)
        synced_chinese_entries = synchronize_subtitle_timings(chinese_entries, english_entries)
        maybe_rewrite_subtitle_file(
            chinese_srt,
            chinese_entries,
            synced_chinese_entries,
            reason="synced translated subtitle timings to repaired English timings",
        )
    else:
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
        video_encoder=args.video_encoder,
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
