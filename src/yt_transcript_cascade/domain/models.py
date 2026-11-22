"""Core domain schemas and enums strictly aligned with PRD v1.2.2."""

from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class ErrorCode(str, Enum):
    NO_SUBTITLE = "NO_SUBTITLE"
    RATE_LIMITED = "RATE_LIMITED"
    SUBTITLE_403 = "SUBTITLE_403"
    NETWORK_ERROR = "NETWORK_ERROR"
    VIDEO_UNAVAILABLE = "VIDEO_UNAVAILABLE"
    AUTH_REQUIRED = "AUTH_REQUIRED"
    AUDIO_DOWNLOAD_FAILED = "AUDIO_DOWNLOAD_FAILED"
    ASR_OOM = "ASR_OOM"
    ASR_BACKEND_UNAVAILABLE = "ASR_BACKEND_UNAVAILABLE"
    COLAB_FAILED = "COLAB_FAILED"
    TIMEOUT = "TIMEOUT"
    INVALID_INPUT = "INVALID_INPUT"
    PATH_VIOLATION = "PATH_VIOLATION"
    UNKNOWN_ERROR = "UNKNOWN_ERROR"


class CascadeAction(str, Enum):
    RETRY = "RETRY"
    FALLBACK_L2 = "FALLBACK_L2"
    FALLBACK_L3 = "FALLBACK_L3"
    DOWNGRADE_MODEL = "DOWNGRADE_MODEL"
    TERMINATE = "TERMINATE"


class ExecutionMode(str, Enum):
    AUTO = "AUTO"
    L1_ONLY = "L1_ONLY"
    LOCAL_ASR_ONLY = "LOCAL_ASR_ONLY"
    COLAB_ONLY = "COLAB_ONLY"


class CascadeTier(str, Enum):
    L1_SUBTITLE = "L1_SUBTITLE"
    L2_LOCAL_ASR = "L2_LOCAL_ASR"
    L3_COLAB_GPU = "L3_COLAB_GPU"


class SourceType(str, Enum):
    MANUAL_SUBTITLE = "MANUAL_SUBTITLE"
    AUTO_SUBTITLE = "AUTO_SUBTITLE"
    LOCAL_ASR = "LOCAL_ASR"
    CLOUD_ASR = "CLOUD_ASR"


class OutputFormat(str, Enum):
    MARKDOWN = "markdown"
    JSON = "json"
    SRT = "srt"


class ErrorDecision(BaseModel):
    model_config = ConfigDict(frozen=True)

    error_code: ErrorCode
    retryable: bool
    max_retries: int
    cascade_action: CascadeAction
    exhausted_action: CascadeAction
    user_action_required: bool


class CascadeError(Exception):
    """Structured domain error carrying a canonical ErrorCode."""

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        *,
        warnings: list[str] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.warnings = warnings or []


class TranscriptSegment(BaseModel):
    start: float = Field(..., ge=0.0)
    end: float = Field(..., ge=0.0)
    text: str
    confidence: float | None = None
    speaker_id: str | None = None


class TranscriptQualityReport(BaseModel):
    structural_ok: bool
    empty_segment_count: int = Field(..., ge=0)
    repetition_anomaly_count: int = Field(..., ge=0)
    duration_coverage_ratio: float | None = Field(default=None, ge=0.0)
    language_consistent: bool | None = None
    confidence_note: str | None = None
    status: Literal["OK", "WARNING"]


class HardwareProfile(BaseModel):
    model_config = ConfigDict(frozen=True)

    platform: Literal["windows", "darwin", "linux"]
    accelerator: Literal["nvidia", "apple_silicon", "amd", "cpu"]
    device_name: str
    vram_total_mb: int | None = None
    vram_available_mb: int | None = None
    backend: Literal["pytorch_cuda", "ctranslate2_cuda", "pytorch_mps", "cpu_int8"]
    backend_verified: bool
    supported_engines: list[str] = Field(default_factory=list)


class TranscriptResult(BaseModel):
    video_id: str = Field(..., pattern=r"^[A-Za-z0-9_-]{11}$")
    title: str | None = None
    channel: str | None = None
    duration_sec: float | None = None
    source_type: SourceType
    source_language: str | None = None
    is_generated: bool
    tier_used: CascadeTier
    engine_used: str | None = None
    model_used: str | None = None
    model_fingerprint: str | None = None
    device_used: str | None = None
    download_sec: float | None = None
    model_load_sec: float | None = None
    asr_sec: float | None = None
    total_elapsed_sec: float = Field(..., ge=0.0)
    rtf: float | None = None
    server_saved_path: str | None = None
    cloud_upload_notice: str | None = None
    segments: list[TranscriptSegment]
    quality_report: TranscriptQualityReport
    cached: bool = False
    warnings: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class GetTranscriptInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str = Field(..., min_length=11)
    languages: list[str] = Field(
        default_factory=lambda: ["zh-Hant", "zh-Hans", "zh", "en"],
        min_length=1,
        max_length=10,
    )
    execution_mode: ExecutionMode = ExecutionMode.AUTO
    enable_colab_fallback: bool = False
    allow_cloud_processing: bool = False
    engine: Literal["openai-whisper", "faster-whisper", "sensevoice"] | None = None
    model: str | None = None
    output_format: OutputFormat = OutputFormat.MARKDOWN
    save_to_path: str | None = None
    max_output_chars: int = Field(default=12000, ge=1000, le=200000)
    timeout_sec: int = Field(default=300, ge=10, le=3600)
    force_refresh: bool = False
    keep_audio: bool = False


class ClearCacheInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    video_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{11}$")
    expired_only: bool = False
    clear_all: bool = False
