"""Unified AsrEngine Protocol and concrete OpenAI Whisper / faster-whisper adapters."""

from __future__ import annotations

import gc
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from ..domain.models import CascadeError, ErrorCode, TranscriptSegment
from .hardware_detector import AsrStrategy


@dataclass(frozen=True)
class AsrTranscribeOutput:
    segments: list[TranscriptSegment]
    detected_language: str | None
    model_load_sec: float
    asr_sec: float
    engine_name: str
    engine_version: str
    model_name: str
    model_fingerprint: str
    device_used: str


class AsrEngine(Protocol):
    """Unified L2 ASR engine contract (PRD v1.2.2 §3.2)."""

    name: str

    def check_available(self, strategy: AsrStrategy) -> bool: ...
    def load_model(self, strategy: AsrStrategy) -> float: ...
    def transcribe(
        self,
        audio_path: Path,
        *,
        language: str | None = None,
        deadline: float | None = None,
    ) -> tuple[list[TranscriptSegment], str | None, float]: ...
    def release(self) -> None: ...


def _decode_audio_ffmpeg_pcm(audio_path: Path, deadline: float | None = None):
    """Decode any audio container via ffmpeg to 16kHz mono float32 ndarray (avoids PyAV issues)."""
    import numpy as np  # type: ignore

    timeout = max(5.0, deadline - time.monotonic()) if deadline is not None else 120.0
    try:
        res = subprocess.run(
            [
                "ffmpeg",
                "-nostdin",
                "-threads",
                "0",
                "-i",
                str(audio_path),
                "-f",
                "s16le",
                "-ac",
                "1",
                "-acodec",
                "pcm_s16le",
                "-ar",
                "16000",
                "-",
            ],
            capture_output=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise CascadeError(ErrorCode.TIMEOUT, "ffmpeg audio decode exceeded request deadline.") from exc

    if res.returncode != 0:
        raise CascadeError(
            ErrorCode.AUDIO_DOWNLOAD_FAILED,
            f"ffmpeg failed to decode audio file: {res.stderr.decode('utf-8', errors='replace')[-300:]}",
        )
    return np.frombuffer(res.stdout, np.int16).flatten().astype(np.float32) / 32768.0


def _is_oom_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(k in msg for k in ("out of memory", "cuda error: out of memory", "cublas_status_alloc_failed", "oom"))


class FasterWhisperEngine:
    """CTranslate2-based faster-whisper adapter supporting float16 / int8_float16 / int8."""

    name = "faster-whisper"

    def __init__(self) -> None:
        self._model = None
        self._strategy: AsrStrategy | None = None

    def check_available(self, strategy: AsrStrategy) -> bool:
        if strategy.engine != self.name:
            return False
        try:
            import faster_whisper  # type: ignore  # noqa: F401
            return True
        except Exception:
            return False

    def load_model(self, strategy: AsrStrategy) -> float:
        t0 = time.monotonic()
        try:
            from faster_whisper import WhisperModel  # type: ignore

            model_id = (
                "deepdml/faster-whisper-large-v3-turbo-ct2"
                if strategy.model_name == "turbo"
                else strategy.model_name
            )
            self._model = WhisperModel(model_id, device=strategy.device, compute_type=strategy.compute_type)
            self._strategy = strategy
            return round(time.monotonic() - t0, 3)
        except Exception as exc:
            self.release()
            if _is_oom_error(exc):
                raise CascadeError(ErrorCode.ASR_OOM, f"faster-whisper OOM loading '{strategy.model_name}': {exc}") from exc
            raise CascadeError(ErrorCode.ASR_BACKEND_UNAVAILABLE, f"faster-whisper load failed: {exc}") from exc

    def transcribe(
        self,
        audio_path: Path,
        *,
        language: str | None = None,
        deadline: float | None = None,
    ) -> tuple[list[TranscriptSegment], str | None, float]:
        if self._model is None:
            raise CascadeError(ErrorCode.ASR_BACKEND_UNAVAILABLE, "faster-whisper model is not loaded.")
        t0 = time.monotonic()
        try:
            audio_np = _decode_audio_ffmpeg_pcm(audio_path, deadline=deadline)
            lang_arg = language.split("-")[0] if language else None
            seg_iter, info = self._model.transcribe(
                audio_np,
                language=lang_arg,
                beam_size=5,
                vad_filter=True,
            )
            segments: list[TranscriptSegment] = []
            for seg in seg_iter:
                if deadline is not None and time.monotonic() > deadline:
                    raise CascadeError(ErrorCode.TIMEOUT, "Request deadline exceeded during faster-whisper transcription.")
                text = (seg.text or "").strip()
                if text:
                    segments.append(
                        TranscriptSegment(
                            start=round(float(seg.start), 3),
                            end=round(max(float(seg.end), float(seg.start)), 3),
                            text=text,
                        )
                    )
            return segments, getattr(info, "language", lang_arg), round(time.monotonic() - t0, 3)
        except CascadeError:
            raise
        except Exception as exc:
            if _is_oom_error(exc):
                raise CascadeError(ErrorCode.ASR_OOM, f"faster-whisper OOM during inference: {exc}") from exc
            raise CascadeError(ErrorCode.UNKNOWN_ERROR, f"faster-whisper transcription failed: {exc}") from exc

    def release(self) -> None:
        self._model = None
        self._strategy = None
        gc.collect()
        try:
            import torch  # type: ignore

            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except Exception:
            pass


class OpenAIWhisperEngine:
    """PyTorch-native openai-whisper adapter (fp16 on CUDA, fp32 on CPU/MPS)."""

    name = "openai-whisper"

    def __init__(self) -> None:
        self._model = None
        self._strategy: AsrStrategy | None = None

    def check_available(self, strategy: AsrStrategy) -> bool:
        if strategy.engine != self.name:
            return False
        try:
            import whisper  # type: ignore  # noqa: F401
            return True
        except Exception:
            return False

    def load_model(self, strategy: AsrStrategy) -> float:
        t0 = time.monotonic()
        try:
            import whisper  # type: ignore

            self._model = whisper.load_model(strategy.model_name, device=strategy.device)
            self._strategy = strategy
            return round(time.monotonic() - t0, 3)
        except Exception as exc:
            self.release()
            if _is_oom_error(exc):
                raise CascadeError(ErrorCode.ASR_OOM, f"openai-whisper OOM loading '{strategy.model_name}': {exc}") from exc
            raise CascadeError(ErrorCode.ASR_BACKEND_UNAVAILABLE, f"openai-whisper load failed: {exc}") from exc

    def transcribe(
        self,
        audio_path: Path,
        *,
        language: str | None = None,
        deadline: float | None = None,
    ) -> tuple[list[TranscriptSegment], str | None, float]:
        if self._model is None or self._strategy is None:
            raise CascadeError(ErrorCode.ASR_BACKEND_UNAVAILABLE, "openai-whisper model is not loaded.")
        t0 = time.monotonic()
        try:
            if deadline is not None and time.monotonic() > deadline:
                raise CascadeError(ErrorCode.TIMEOUT, "Request deadline exceeded before openai-whisper inference.")
            lang_arg = language.split("-")[0] if language else None
            fp16 = self._strategy.device == "cuda" and self._strategy.compute_type == "fp16"
            out = self._model.transcribe(
                str(audio_path),
                language=lang_arg,
                fp16=fp16,
                verbose=False,
            )
            segments: list[TranscriptSegment] = []
            for seg in out.get("segments", []):
                text = (seg.get("text") or "").strip()
                if text:
                    start = round(float(seg["start"]), 3)
                    end = round(max(float(seg["end"]), start), 3)
                    segments.append(TranscriptSegment(start=start, end=end, text=text))
            return segments, out.get("language") or lang_arg, round(time.monotonic() - t0, 3)
        except CascadeError:
            raise
        except Exception as exc:
            if _is_oom_error(exc):
                raise CascadeError(ErrorCode.ASR_OOM, f"openai-whisper OOM during inference: {exc}") from exc
            raise CascadeError(ErrorCode.UNKNOWN_ERROR, f"openai-whisper transcription failed: {exc}") from exc

    def release(self) -> None:
        self._model = None
        self._strategy = None
        gc.collect()
        try:
            import torch  # type: ignore

            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except Exception:
            pass
