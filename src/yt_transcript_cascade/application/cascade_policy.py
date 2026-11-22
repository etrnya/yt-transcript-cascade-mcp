"""Centralized CascadePolicy governing all 14 ErrorCodes, execution_mode, and cache exclusivity."""

from __future__ import annotations

from ..domain.models import (
    CascadeAction,
    CascadeError,
    ClearCacheInput,
    ErrorCode,
    ErrorDecision,
    ExecutionMode,
    GetTranscriptInput,
)

# Canonical 14-ErrorCode decision table (PRD v1.2.2 §2.2)
_ERROR_DECISIONS: dict[ErrorCode, ErrorDecision] = {
    ErrorCode.NO_SUBTITLE: ErrorDecision(
        error_code=ErrorCode.NO_SUBTITLE,
        retryable=False,
        max_retries=0,
        cascade_action=CascadeAction.FALLBACK_L2,
        exhausted_action=CascadeAction.TERMINATE,
        user_action_required=False,
    ),
    ErrorCode.RATE_LIMITED: ErrorDecision(
        error_code=ErrorCode.RATE_LIMITED,
        retryable=False,
        max_retries=0,
        cascade_action=CascadeAction.FALLBACK_L2,
        exhausted_action=CascadeAction.TERMINATE,
        user_action_required=False,
    ),
    ErrorCode.SUBTITLE_403: ErrorDecision(
        error_code=ErrorCode.SUBTITLE_403,
        retryable=False,
        max_retries=0,
        cascade_action=CascadeAction.FALLBACK_L2,
        exhausted_action=CascadeAction.TERMINATE,
        user_action_required=False,
    ),
    ErrorCode.NETWORK_ERROR: ErrorDecision(
        error_code=ErrorCode.NETWORK_ERROR,
        retryable=True,
        max_retries=2,
        cascade_action=CascadeAction.RETRY,
        exhausted_action=CascadeAction.TERMINATE,
        user_action_required=False,
    ),
    ErrorCode.VIDEO_UNAVAILABLE: ErrorDecision(
        error_code=ErrorCode.VIDEO_UNAVAILABLE,
        retryable=False,
        max_retries=0,
        cascade_action=CascadeAction.TERMINATE,
        exhausted_action=CascadeAction.TERMINATE,
        user_action_required=False,
    ),
    ErrorCode.AUTH_REQUIRED: ErrorDecision(
        error_code=ErrorCode.AUTH_REQUIRED,
        retryable=False,
        max_retries=0,
        cascade_action=CascadeAction.TERMINATE,
        exhausted_action=CascadeAction.TERMINATE,
        user_action_required=True,
    ),
    ErrorCode.AUDIO_DOWNLOAD_FAILED: ErrorDecision(
        error_code=ErrorCode.AUDIO_DOWNLOAD_FAILED,
        retryable=True,
        max_retries=1,
        cascade_action=CascadeAction.RETRY,
        exhausted_action=CascadeAction.TERMINATE,
        user_action_required=False,
    ),
    ErrorCode.ASR_OOM: ErrorDecision(
        error_code=ErrorCode.ASR_OOM,
        retryable=False,
        max_retries=2,
        cascade_action=CascadeAction.DOWNGRADE_MODEL,
        exhausted_action=CascadeAction.TERMINATE,
        user_action_required=False,
    ),
    ErrorCode.ASR_BACKEND_UNAVAILABLE: ErrorDecision(
        error_code=ErrorCode.ASR_BACKEND_UNAVAILABLE,
        retryable=False,
        max_retries=0,
        cascade_action=CascadeAction.FALLBACK_L3,
        exhausted_action=CascadeAction.TERMINATE,
        user_action_required=True,
    ),
    ErrorCode.COLAB_FAILED: ErrorDecision(
        error_code=ErrorCode.COLAB_FAILED,
        retryable=False,
        max_retries=0,
        cascade_action=CascadeAction.TERMINATE,
        exhausted_action=CascadeAction.TERMINATE,
        user_action_required=True,
    ),
    ErrorCode.TIMEOUT: ErrorDecision(
        error_code=ErrorCode.TIMEOUT,
        retryable=False,
        max_retries=0,
        cascade_action=CascadeAction.TERMINATE,
        exhausted_action=CascadeAction.TERMINATE,
        user_action_required=False,
    ),
    ErrorCode.INVALID_INPUT: ErrorDecision(
        error_code=ErrorCode.INVALID_INPUT,
        retryable=False,
        max_retries=0,
        cascade_action=CascadeAction.TERMINATE,
        exhausted_action=CascadeAction.TERMINATE,
        user_action_required=True,
    ),
    ErrorCode.PATH_VIOLATION: ErrorDecision(
        error_code=ErrorCode.PATH_VIOLATION,
        retryable=False,
        max_retries=0,
        cascade_action=CascadeAction.TERMINATE,
        exhausted_action=CascadeAction.TERMINATE,
        user_action_required=True,
    ),
    ErrorCode.UNKNOWN_ERROR: ErrorDecision(
        error_code=ErrorCode.UNKNOWN_ERROR,
        retryable=False,
        max_retries=0,
        cascade_action=CascadeAction.TERMINATE,
        exhausted_action=CascadeAction.TERMINATE,
        user_action_required=False,
    ),
}


class CascadePolicy:
    """Single source of truth for retry, fallback, termination, and parameter validation."""

    @staticmethod
    def validate_request(req: GetTranscriptInput) -> None:
        """Validate execution_mode and decoupled cloud authorization rules (PRD §2.1)."""
        if req.execution_mode == ExecutionMode.COLAB_ONLY and not req.allow_cloud_processing:
            raise CascadeError(
                ErrorCode.INVALID_INPUT,
                "execution_mode='COLAB_ONLY' requires allow_cloud_processing=True to authorize uploading audio to Google Colab.",
            )

    @staticmethod
    def validate_clear_cache_input(req: ClearCacheInput) -> str:
        """Enforce strict 3-way mutual exclusion truth table (PRD §6.2 Tool 4).

        Returns the single chosen action: 'video_id' | 'expired_only' | 'clear_all'.
        """
        chosen: list[str] = []
        if req.video_id is not None:
            chosen.append("video_id")
        if req.expired_only:
            chosen.append("expired_only")
        if req.clear_all:
            chosen.append("clear_all")

        if len(chosen) != 1:
            raise CascadeError(
                ErrorCode.INVALID_INPUT,
                "clear_transcript_cache requires exactly one of: video_id, expired_only=True, or clear_all=True.",
            )
        return chosen[0]

    @staticmethod
    def decide(
        code: ErrorCode,
        *,
        attempt: int = 0,
        execution_mode: ExecutionMode = ExecutionMode.AUTO,
        enable_colab_fallback: bool = False,
    ) -> ErrorDecision:
        """Return an ErrorDecision accounting for retries, execution_mode, and cloud opt-in."""
        base = _ERROR_DECISIONS[code]

        # Dynamic user_action_required decoupling for ASR_BACKEND_UNAVAILABLE (PRD §2.2)
        if code == ErrorCode.ASR_BACKEND_UNAVAILABLE:
            if execution_mode == ExecutionMode.AUTO and enable_colab_fallback:
                return ErrorDecision(
                    error_code=code,
                    retryable=False,
                    max_retries=0,
                    cascade_action=CascadeAction.FALLBACK_L3,
                    exhausted_action=CascadeAction.TERMINATE,
                    user_action_required=False,
                )
            return ErrorDecision(
                error_code=code,
                retryable=False,
                max_retries=0,
                cascade_action=CascadeAction.TERMINATE,
                exhausted_action=CascadeAction.TERMINATE,
                user_action_required=True,
            )

        # Enforce execution_mode tier prohibitions
        if execution_mode == ExecutionMode.L1_ONLY and base.cascade_action in (
            CascadeAction.FALLBACK_L2,
            CascadeAction.FALLBACK_L3,
        ):
            return base.model_copy(update={"cascade_action": CascadeAction.TERMINATE})

        if execution_mode == ExecutionMode.LOCAL_ASR_ONLY and base.cascade_action == CascadeAction.FALLBACK_L3:
            return base.model_copy(update={"cascade_action": CascadeAction.TERMINATE})

        # Retry exhaustion transition -> exhausted_action
        if base.retryable and attempt >= base.max_retries:
            return base.model_copy(
                update={
                    "retryable": False,
                    "cascade_action": base.exhausted_action,
                }
            )

        return base

    @staticmethod
    def all_error_codes() -> tuple[ErrorCode, ...]:
        return tuple(_ERROR_DECISIONS.keys())
