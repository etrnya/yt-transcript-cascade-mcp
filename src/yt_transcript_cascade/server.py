"""FastMCP Stdio server exposing 4 single-responsibility tools (all logs -> stderr)."""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Any, Literal

from mcp.server.fastmcp import FastMCP
from pydantic import ValidationError

from .application.orchestrator import CascadeOrchestrator
from .domain.models import (
    ClearCacheInput,
    ErrorCode,
    ExecutionMode,
    GetTranscriptInput,
    OutputFormat,
)

# Enforce Stdio JSON-RPC purity: route all logs exclusively to stderr
logging.basicConfig(
    stream=sys.stderr,
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("yt-transcript-cascade-mcp")


def _default_save_root() -> Path:
    env_root = os.environ.get("YT_CASCADE_SAVE_ROOT")
    return Path(env_root).resolve() if env_root else Path.cwd().resolve()


mcp = FastMCP("yt-transcript-cascade-mcp")
_orchestrator = CascadeOrchestrator(allowed_save_root=_default_save_root())


@mcp.tool()
def get_youtube_transcript(
    url: str,
    languages: list[str] | None = None,
    execution_mode: Literal["AUTO", "L1_ONLY", "LOCAL_ASR_ONLY", "COLAB_ONLY"] = "AUTO",
    enable_colab_fallback: bool = False,
    allow_cloud_processing: bool = False,
    engine: Literal["openai-whisper", "faster-whisper", "sensevoice"] | None = None,
    model: str | None = None,
    output_format: Literal["markdown", "json", "srt"] = "markdown",
    save_to_path: str | None = None,
    max_output_chars: int = 12000,
    timeout_sec: int = 300,
    force_refresh: bool = False,
    keep_audio: bool = False,
) -> dict[str, Any]:
    """Fetch a YouTube transcript with provenance, quality report, and structured Markdown (.md) output."""
    try:
        req = GetTranscriptInput(
            url=url,
            languages=languages if languages is not None else ["zh-Hant", "zh-Hans", "zh", "en"],
            execution_mode=ExecutionMode(execution_mode),
            enable_colab_fallback=enable_colab_fallback,
            allow_cloud_processing=allow_cloud_processing,
            engine=engine,
            model=model,
            output_format=OutputFormat(output_format),
            save_to_path=save_to_path,
            max_output_chars=max_output_chars,
            timeout_sec=timeout_sec,
            force_refresh=force_refresh,
            keep_audio=keep_audio,
        )
    except ValidationError as exc:
        return {
            "ok": False,
            "error": {
                "code": ErrorCode.INVALID_INPUT.value,
                "message": str(exc.errors()[0]["msg"]) if exc.errors() else "Invalid parameters.",
                "user_action_required": True,
                "warnings": [],
            },
        }
    return _orchestrator.get_transcript(req)


@mcp.tool()
def check_cascade_environment() -> dict[str, Any]:
    """Inspect local yt-dlp, ffmpeg, hardware profile, and cascade readiness."""
    return _orchestrator.check_environment()


@mcp.tool()
def get_transcript_cache_stats() -> dict[str, Any]:
    """Read-only summary of Source Cache and ASR Cache entry counts and byte sizes."""
    return _orchestrator.get_cache_stats()


@mcp.tool()
def clear_transcript_cache(
    video_id: str | None = None,
    expired_only: bool = False,
    clear_all: bool = False,
) -> dict[str, Any]:
    """Clear cache using strict 3-way mutual exclusion (exactly one of video_id, expired_only, or clear_all)."""
    try:
        req = ClearCacheInput(
            video_id=video_id,
            expired_only=expired_only,
            clear_all=clear_all,
        )
    except ValidationError as exc:
        return {
            "ok": False,
            "error": {
                "code": ErrorCode.INVALID_INPUT.value,
                "message": str(exc.errors()[0]["msg"]) if exc.errors() else "Invalid cache clear parameters.",
                "user_action_required": True,
                "warnings": [],
            },
        }
    return _orchestrator.clear_cache(req)


def main() -> None:
    logger.info("Starting yt-transcript-cascade-mcp (Stdio transport)")
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
