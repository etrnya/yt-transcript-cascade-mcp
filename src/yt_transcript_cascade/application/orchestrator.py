"""M1 CascadeOrchestrator: synchronous execution, shared deadline, in-process single-flight."""

from __future__ import annotations

import platform
import shutil
import threading
import time
from pathlib import Path
from typing import Any

from ..domain.models import (
    CascadeAction,
    CascadeError,
    CascadeTier,
    ClearCacheInput,
    ErrorCode,
    ExecutionMode,
    GetTranscriptInput,
    HardwareProfile,
    SourceType,
    TranscriptResult,
)
from ..domain.url_parser import parse_youtube_video_id, validate_save_path
from ..infrastructure.source_cache import SourceCache
from ..infrastructure.yt_dlp_adapter import YtDlpAdapter
from .cascade_policy import CascadePolicy
from .formatter import atomic_write_text, render_transcript
from .quality_analyzer import analyze_transcript_quality


class CascadeOrchestrator:
    """Coordinates URL validation, single-flight dedup, Source Cache, L1 retrieval, and QA."""

    def __init__(
        self,
        *,
        yt_dlp_adapter: YtDlpAdapter | None = None,
        source_cache: SourceCache | None = None,
        allowed_save_root: Path | None = None,
    ) -> None:
        self.yt_dlp = yt_dlp_adapter or YtDlpAdapter()
        self.cache = source_cache or SourceCache()
        self.allowed_save_root = (allowed_save_root or Path.cwd()).resolve()
        self._inflight_guard = threading.Lock()
        self._inflight_locks: dict[str, threading.Lock] = {}

    def _get_flight_lock(self, key: str) -> threading.Lock:
        with self._inflight_guard:
            lock = self._inflight_locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._inflight_locks[key] = lock
            return lock

    def get_transcript(self, req: GetTranscriptInput) -> dict[str, Any]:
        """Execute M1 transcript retrieval synchronously under a shared request deadline."""
        t_start = time.monotonic()
        deadline = t_start + req.timeout_sec

        try:
            CascadePolicy.validate_request(req)
            video_id = parse_youtube_video_id(req.url)

            save_target: Path | None = None
            if req.save_to_path is not None:
                save_target = validate_save_path(req.save_to_path, self.allowed_save_root)

            # Enforce M1 boundaries honestly for L2/L3-only modes
            if req.execution_mode == ExecutionMode.LOCAL_ASR_ONLY:
                raise CascadeError(
                    ErrorCode.ASR_BACKEND_UNAVAILABLE,
                    "L2 local ASR engine is scheduled for Milestone M2 and is not enabled in M1 MVP.",
                )
            if req.execution_mode == ExecutionMode.COLAB_ONLY:
                raise CascadeError(
                    ErrorCode.COLAB_FAILED,
                    "L3 Colab GPU runtime is scheduled for Milestone M4 and is not enabled in M1 MVP.",
                )

            # In-process single-flight lock per video_id
            flight_lock = self._get_flight_lock(video_id)
            with flight_lock:
                result: TranscriptResult | None = None
                if not req.force_refresh:
                    result = self.cache.get(video_id, req.languages)

                if result is None:
                    result = self._run_l1_with_policy(video_id, req, deadline, t_start)
                    self.cache.put(result)

            # Render Markdown / JSON / SRT and handle persistence / truncation
            rendered = render_transcript(result, req.output_format)
            server_saved_path: str | None = None

            if save_target is not None:
                atomic_write_text(save_target, rendered)
                server_saved_path = str(save_target)
            elif len(rendered) > req.max_output_chars:
                ext = {"markdown": ".md", "json": ".json", "srt": ".srt"}[req.output_format.value]
                auto_path = self.allowed_save_root / "transcripts" / f"{video_id}{ext}"
                atomic_write_text(auto_path, rendered)
                server_saved_path = str(auto_path)

            if server_saved_path is not None:
                result = result.model_copy(update={"server_saved_path": server_saved_path})

            output_text = rendered
            if len(rendered) > req.max_output_chars:
                output_text = (
                    rendered[: req.max_output_chars]
                    + f"\n\n... [Truncated at {req.max_output_chars} chars. Full transcript saved at: {server_saved_path}]\n"
                )

            return {
                "ok": True,
                "result": result.model_dump(mode="json"),
                "rendered_text": output_text,
            }

        except CascadeError as err:
            decision = CascadePolicy.decide(
                err.code,
                execution_mode=req.execution_mode,
                enable_colab_fallback=req.enable_colab_fallback,
            )
            return {
                "ok": False,
                "error": {
                    "code": err.code.value,
                    "message": err.message,
                    "user_action_required": decision.user_action_required,
                    "warnings": err.warnings,
                },
            }
        except Exception as exc:
            return {
                "ok": False,
                "error": {
                    "code": ErrorCode.UNKNOWN_ERROR.value,
                    "message": f"Unexpected internal error: {type(exc).__name__}",
                    "user_action_required": False,
                    "warnings": [],
                },
            }

    def _run_l1_with_policy(
        self,
        video_id: str,
        req: GetTranscriptInput,
        deadline: float,
        t_start: float,
    ) -> TranscriptResult:
        attempt = 0
        while True:
            try:
                sub = self.yt_dlp.fetch_subtitle(video_id, req.languages, deadline=deadline)
                quality = analyze_transcript_quality(
                    sub.segments,
                    duration_sec=sub.duration_sec,
                    expected_languages=req.languages,
                    detected_language=sub.source_language,
                )
                return TranscriptResult(
                    video_id=sub.video_id,
                    title=sub.title,
                    channel=sub.channel,
                    duration_sec=sub.duration_sec,
                    source_type=sub.source_type,
                    source_language=sub.source_language,
                    is_generated=(sub.source_type == SourceType.AUTO_SUBTITLE),
                    tier_used=CascadeTier.L1_SUBTITLE,
                    engine_used="yt-dlp",
                    model_used=None,
                    model_fingerprint=None,
                    device_used="network",
                    download_sec=sub.download_sec,
                    model_load_sec=0.0,
                    asr_sec=0.0,
                    total_elapsed_sec=round(time.monotonic() - t_start, 3),
                    rtf=None,
                    server_saved_path=None,
                    cloud_upload_notice=None,
                    segments=sub.segments,
                    quality_report=quality,
                    cached=False,
                    warnings=sub.warnings,
                    metadata={},
                )
            except CascadeError as err:
                decision = CascadePolicy.decide(
                    err.code,
                    attempt=attempt,
                    execution_mode=req.execution_mode,
                    enable_colab_fallback=req.enable_colab_fallback,
                )
                if decision.cascade_action == CascadeAction.RETRY:
                    attempt += 1
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise CascadeError(ErrorCode.TIMEOUT, "Deadline exceeded during retry backoff.") from err
                    time.sleep(min(0.5 * attempt, remaining))
                    continue

                if decision.cascade_action == CascadeAction.FALLBACK_L2:
                    raise CascadeError(
                        err.code,
                        f"{err.message} (Policy selected FALLBACK_L2, which will be implemented in Milestone M2.)",
                        warnings=err.warnings + ["L2 local ASR fallback is scheduled for Milestone M2."],
                    ) from err

                raise

    def check_environment(self) -> dict[str, Any]:
        sys_plat = platform.system().lower()
        plat: Any = "windows" if "windows" in sys_plat else ("darwin" if "darwin" in sys_plat else "linux")
        hw = HardwareProfile(
            platform=plat,
            accelerator="cpu",
            device_name=platform.processor() or "CPU",
            vram_total_mb=None,
            vram_available_mb=None,
            backend="cpu_int8",
            backend_verified=False,
            supported_engines=[],
        )
        return {
            "ok": True,
            "hardware": hw.model_dump(mode="json"),
            "yt_dlp_version": self.yt_dlp.get_version(),
            "ffmpeg_available": shutil.which("ffmpeg") is not None,
            "installed_asr_engines": [],
            "colab_ready": False,
            "pending_colab_sessions": 0,
        }

    def get_cache_stats(self) -> dict[str, Any]:
        return {"ok": True, **self.cache.stats()}

    def clear_cache(self, req: ClearCacheInput) -> dict[str, Any]:
        try:
            mode = CascadePolicy.validate_clear_cache_input(req)
            if mode == "video_id" and req.video_id:
                removed = self.cache.clear_by_video_id(req.video_id)
            elif mode == "expired_only":
                removed = self.cache.clear_expired()
            else:
                removed = self.cache.clear_all()
            return {"ok": True, "action": mode, "removed_entries": removed}
        except CascadeError as err:
            return {
                "ok": False,
                "error": {
                    "code": err.code.value,
                    "message": err.message,
                    "user_action_required": True,
                    "warnings": err.warnings,
                },
            }
