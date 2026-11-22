"""M1/M2 CascadeOrchestrator: synchronous execution, shared deadline, single-flight, L1 -> L2 ASR."""

from __future__ import annotations

import shutil
import tempfile
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
    SourceType,
    TranscriptResult,
)
from ..domain.url_parser import parse_youtube_video_id, validate_save_path
from ..infrastructure.asr_engines import AsrEngine, FasterWhisperEngine, OpenAIWhisperEngine
from ..infrastructure.hardware_detector import HardwareDetector, build_oom_fallback_chain
from ..infrastructure.source_cache import SourceCache
from ..infrastructure.yt_dlp_adapter import YtDlpAdapter
from .cascade_policy import CascadePolicy
from .formatter import atomic_write_text, render_transcript
from .quality_analyzer import analyze_transcript_quality


class CascadeOrchestrator:
    """Coordinates URL validation, single-flight dedup, Source Cache, L1 subtitle, and L2 local ASR."""

    def __init__(
        self,
        *,
        yt_dlp_adapter: YtDlpAdapter | None = None,
        source_cache: SourceCache | None = None,
        hardware_detector: HardwareDetector | None = None,
        asr_engines: dict[str, AsrEngine] | None = None,
        allowed_save_root: Path | None = None,
    ) -> None:
        self.yt_dlp = yt_dlp_adapter or YtDlpAdapter()
        self.cache = source_cache or SourceCache()
        self.hardware_detector = hardware_detector or HardwareDetector()
        self.asr_engines: dict[str, AsrEngine] = (
            asr_engines
            if asr_engines is not None
            else {
                "faster-whisper": FasterWhisperEngine(),
                "openai-whisper": OpenAIWhisperEngine(),
            }
        )
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
        """Execute transcript retrieval synchronously under a shared request deadline."""
        t_start = time.monotonic()
        deadline = t_start + req.timeout_sec

        try:
            CascadePolicy.validate_request(req)
            video_id = parse_youtube_video_id(req.url)

            save_target: Path | None = None
            if req.save_to_path is not None:
                save_target = validate_save_path(req.save_to_path, self.allowed_save_root)

            if req.execution_mode == ExecutionMode.COLAB_ONLY:
                raise CascadeError(
                    ErrorCode.COLAB_FAILED,
                    "L3 Colab GPU runtime is scheduled for Milestone M4 and is not enabled yet.",
                )

            flight_lock = self._get_flight_lock(video_id)
            with flight_lock:
                result: TranscriptResult | None = None
                if not req.force_refresh and req.execution_mode in (ExecutionMode.AUTO, ExecutionMode.L1_ONLY):
                    result = self.cache.get(video_id, req.languages)

                if result is None:
                    if req.execution_mode == ExecutionMode.LOCAL_ASR_ONLY:
                        result = self._run_l2_local_asr(video_id, req, deadline, t_start)
                    else:
                        result = self._run_l1_then_maybe_l2(video_id, req, deadline, t_start)
                    self.cache.put(result)

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

    def _run_l1_then_maybe_l2(
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
                    return self._run_l2_local_asr(
                        video_id,
                        req,
                        deadline,
                        t_start,
                        inherited_warnings=err.warnings + [f"L1 fallback triggered due to {err.code.value}."],
                    )

                raise

    def _run_l2_local_asr(
        self,
        video_id: str,
        req: GetTranscriptInput,
        deadline: float,
        t_start: float,
        *,
        inherited_warnings: list[str] | None = None,
    ) -> TranscriptResult:
        warnings = list(inherited_warnings or [])
        profile = self.hardware_detector.detect()
        strategies = build_oom_fallback_chain(
            profile,
            preferred_engine=req.engine,
            preferred_model=req.model,
        )
        if not strategies:
            raise CascadeError(
                ErrorCode.ASR_BACKEND_UNAVAILABLE,
                "No local ASR engine (openai-whisper / faster-whisper) is installed or verified.",
                warnings=warnings + ["Install optional local ASR extras: `pip install openai-whisper` or `faster-whisper`."],
            )

        # Download audio with retry policy (AUDIO_DOWNLOAD_FAILED max_retries=1)
        tmp_dir_obj = tempfile.TemporaryDirectory(prefix=f"yt_asr_{video_id}_")
        audio_dir = (self.allowed_save_root / "transcripts" / "audio") if req.keep_audio else Path(tmp_dir_obj.name)
        audio_out = None
        try:
            dl_attempt = 0
            while True:
                try:
                    audio_out = self.yt_dlp.download_audio(video_id, audio_dir, deadline=deadline)
                    break
                except CascadeError as err:
                    decision = CascadePolicy.decide(err.code, attempt=dl_attempt, execution_mode=req.execution_mode)
                    if decision.cascade_action == CascadeAction.RETRY:
                        dl_attempt += 1
                        continue
                    raise

            # Execute finite OOM fallback strategy chain (never repeats a strategy)
            last_err: CascadeError | None = None
            target_lang = req.languages[0] if req.languages else None

            for strat in strategies:
                engine = self.asr_engines.get(strat.engine)
                if engine is None or not engine.check_available(strat):
                    continue
                try:
                    load_sec = engine.load_model(strat)
                    segments, detected_lang, asr_sec = engine.transcribe(
                        audio_out.audio_path,
                        language=target_lang,
                        deadline=deadline,
                    )
                    quality = analyze_transcript_quality(
                        segments,
                        duration_sec=audio_out.duration_sec,
                        expected_languages=req.languages,
                        detected_language=detected_lang,
                    )
                    rtf = (
                        round(asr_sec / audio_out.duration_sec, 4)
                        if audio_out.duration_sec and audio_out.duration_sec > 0
                        else None
                    )
                    return TranscriptResult(
                        video_id=video_id,
                        title=audio_out.title,
                        channel=audio_out.channel,
                        duration_sec=audio_out.duration_sec,
                        source_type=SourceType.LOCAL_ASR,
                        source_language=detected_lang or target_lang,
                        is_generated=True,
                        tier_used=CascadeTier.L2_LOCAL_ASR,
                        engine_used=strat.engine,
                        model_used=strat.model_name,
                        model_fingerprint=f"{strat.engine}:{strat.model_name}:{strat.compute_type}",
                        device_used=f"{strat.device}:{strat.compute_type}",
                        download_sec=audio_out.download_sec,
                        model_load_sec=load_sec,
                        asr_sec=asr_sec,
                        total_elapsed_sec=round(time.monotonic() - t_start, 3),
                        rtf=rtf,
                        server_saved_path=None,
                        cloud_upload_notice=None,
                        segments=segments,
                        quality_report=quality,
                        cached=False,
                        warnings=warnings,
                        metadata={"audio_fingerprint": audio_out.audio_fingerprint},
                    )
                except CascadeError as err:
                    last_err = err
                    if err.code == ErrorCode.ASR_OOM:
                        warnings.append(
                            f"ASR_OOM on {strat.engine}/{strat.device}/{strat.model_name}; downgrading to next strategy."
                        )
                        continue
                    raise
                finally:
                    engine.release()

            if last_err is not None:
                raise last_err
            raise CascadeError(
                ErrorCode.ASR_BACKEND_UNAVAILABLE,
                "All candidate local ASR engines were unavailable.",
                warnings=warnings,
            )
        finally:
            if not req.keep_audio:
                tmp_dir_obj.cleanup()

    def check_environment(self) -> dict[str, Any]:
        hw = self.hardware_detector.detect()
        return {
            "ok": True,
            "hardware": hw.model_dump(mode="json"),
            "yt_dlp_version": self.yt_dlp.get_version(),
            "ffmpeg_available": shutil.which("ffmpeg") is not None,
            "installed_asr_engines": list(hw.supported_engines),
            "colab_ready": shutil.which("colab") is not None,
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
