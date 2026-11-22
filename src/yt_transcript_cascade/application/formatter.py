"""Render TranscriptResult into structured Markdown (.md), JSON, or SRT."""

from __future__ import annotations

import json
import os
from pathlib import Path

from ..domain.models import OutputFormat, TranscriptResult


def _fmt_mmss(seconds: float) -> str:
    total = max(0, int(seconds))
    hh, rem = divmod(total, 3600)
    mm, ss = divmod(rem, 60)
    if hh > 0:
        return f"{hh:02d}:{mm:02d}:{ss:02d}"
    return f"{mm:02d}:{ss:02d}"


def _fmt_srt_time(seconds: float) -> str:
    ms_total = max(0, int(round(seconds * 1000)))
    hh, rem = divmod(ms_total, 3_600_000)
    mm, rem = divmod(rem, 60_000)
    ss, ms = divmod(rem, 1000)
    return f"{hh:02d}:{mm:02d}:{ss:02d},{ms:03d}"


def _yaml_escape(val: str | None) -> str:
    if val is None:
        return "null"
    escaped = val.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")
    return f'"{escaped}"'


def render_transcript(result: TranscriptResult, output_format: OutputFormat) -> str:
    """Render a TranscriptResult into Markdown (with YAML Frontmatter), JSON, or SRT."""
    if output_format == OutputFormat.JSON:
        return result.model_dump_json(indent=2)

    if output_format == OutputFormat.SRT:
        blocks: list[str] = []
        for idx, seg in enumerate(result.segments, 1):
            blocks.append(
                f"{idx}\n{_fmt_srt_time(seg.start)} --> {_fmt_srt_time(seg.end)}\n{seg.text.strip()}"
            )
        return "\n\n".join(blocks) + ("\n" if blocks else "")

    # Default: Structured Markdown (.md) with YAML Frontmatter Provenance for AI Agents
    frontmatter = [
        "---",
        f"video_id: {result.video_id}",
        f"title: {_yaml_escape(result.title)}",
        f"channel: {_yaml_escape(result.channel)}",
        f"duration_sec: {result.duration_sec if result.duration_sec is not None else 'null'}",
        f"source_type: {result.source_type.value}",
        f"tier_used: {result.tier_used.value}",
        f"is_generated: {'true' if result.is_generated else 'false'}",
        f"source_language: {_yaml_escape(result.source_language)}",
        f"quality_status: {result.quality_report.status}",
        f"duration_coverage_ratio: {result.quality_report.duration_coverage_ratio if result.quality_report.duration_coverage_ratio is not None else 'null'}",
        f"cached: {'true' if result.cached else 'false'}",
        "---",
        "",
        f"# {result.title or result.video_id}",
        "",
    ]
    body = [f"- [{_fmt_mmss(seg.start)}] {seg.text.strip()}" for seg in result.segments if seg.text.strip()]
    return "\n".join(frontmatter + body) + "\n"


def atomic_write_text(path: Path, content: str) -> None:
    """Write text atomically via temp file + os.replace to prevent half-written files."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(content, encoding="utf-8")
    os.replace(tmp_path, path)
