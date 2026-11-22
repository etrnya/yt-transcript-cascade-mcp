"""YouTube URL whitelist parser and safe output path validator."""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .models import CascadeError, ErrorCode

VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
ALLOWED_HOSTS = frozenset(
    {
        "youtube.com",
        "www.youtube.com",
        "m.youtube.com",
        "music.youtube.com",
        "youtu.be",
        "www.youtu.be",
    }
)


def parse_youtube_video_id(raw_url: str) -> str:
    """Extract and strictly validate an 11-char YouTube video_id from an official URL.

    Rejects non-YouTube domains, shell metacharacters, and malformed IDs with INVALID_INPUT.
    """
    candidate = (raw_url or "").strip()
    if not candidate:
        raise CascadeError(ErrorCode.INVALID_INPUT, "YouTube URL cannot be empty.")

    # Direct 11-char video_id shortcut is allowed only if strictly alphanumeric/-/_
    if VIDEO_ID_RE.fullmatch(candidate):
        return candidate

    parsed = urlparse(candidate)
    if parsed.scheme not in ("http", "https"):
        raise CascadeError(
            ErrorCode.INVALID_INPUT,
            f"Unsupported URL scheme: '{parsed.scheme}'. Only http/https YouTube URLs are allowed.",
        )

    host = (parsed.hostname or "").lower()
    if host not in ALLOWED_HOSTS:
        raise CascadeError(
            ErrorCode.INVALID_INPUT,
            f"Disallowed host '{host}'. Only official YouTube domains are permitted.",
        )

    video_id: str | None = None
    path_parts = [p for p in parsed.path.split("/") if p]

    if host in ("youtu.be", "www.youtu.be"):
        if path_parts:
            video_id = path_parts[0]
    else:
        if parsed.path == "/watch":
            qs = parse_qs(parsed.query)
            vals = qs.get("v")
            if vals:
                video_id = vals[0]
        elif len(path_parts) >= 2 and path_parts[0] in ("shorts", "embed", "live", "v"):
            video_id = path_parts[1]

    if not video_id or not VIDEO_ID_RE.fullmatch(video_id):
        raise CascadeError(
            ErrorCode.INVALID_INPUT,
            "Could not extract a valid 11-character YouTube video_id from URL.",
        )
    return video_id


def validate_save_path(save_to_path: str, allowed_root: Path) -> Path:
    """Ensure save_to_path resolves strictly inside allowed_root (anti path traversal)."""
    if not save_to_path or not save_to_path.strip():
        raise CascadeError(ErrorCode.PATH_VIOLATION, "save_to_path cannot be empty.")

    root_resolved = allowed_root.resolve()
    raw = Path(save_to_path)
    target = raw.resolve() if raw.is_absolute() else (root_resolved / raw).resolve()

    try:
        target.relative_to(root_resolved)
    except ValueError as exc:
        raise CascadeError(
            ErrorCode.PATH_VIOLATION,
            f"save_to_path '{save_to_path}' escapes allowed root directory '{root_resolved}'.",
        ) from exc

    if target == root_resolved:
        raise CascadeError(
            ErrorCode.PATH_VIOLATION,
            "save_to_path must point to a file path inside the allowed directory, not the root itself.",
        )
    return target
