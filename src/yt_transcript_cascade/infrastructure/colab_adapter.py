"""L3 Google Colab GPU ASR adapter (PyTorch CUDA + openai-whisper turbo, zero shell=True)."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from ..domain.models import CascadeError, ErrorCode, TranscriptSegment
from .colab_lifecycle import ColabRuntimeLifecycleManager

# Native PyTorch CUDA + openai-whisper script for Colab CUDA 13 (avoids ctranslate2/PyAV conflicts)
_REMOTE_WHISPER_SCRIPT = r'''
import json, os, sys, time

def main():
    audio_file = sys.argv[1]
    model_name = sys.argv[2]
    lang = sys.argv[3] if len(sys.argv) > 3 and sys.argv[3] != "auto" else None

    import torch
    import whisper

    device = "cuda" if torch.cuda.is_available() else "cpu"
    device_name = torch.cuda.get_device_name(0) if device == "cuda" else "cpu"

    t_load = time.monotonic()
    model = whisper.load_model(model_name, device=device)
    load_sec = round(time.monotonic() - t_load, 3)

    t_asr = time.monotonic()
    out = model.transcribe(audio_file, language=lang, fp16=(device == "cuda"), verbose=False)
    asr_sec = round(time.monotonic() - t_asr, 3)

    segments = []
    for seg in out.get("segments", []):
        text = (seg.get("text") or "").strip()
        if text:
            start = round(float(seg["start"]), 3)
            end = round(max(float(seg["end"]), start), 3)
            segments.append({"start": start, "end": end, "text": text})

    try:
        os.remove(audio_file)
    except OSError:
        pass

    payload = {
        "segments": segments,
        "language": out.get("language") or lang,
        "model_load_sec": load_sec,
        "asr_sec": asr_sec,
        "device_used": f"colab:{device}:{device_name}",
    }
    print("__YT_CASCADE_JSON_START__")
    print(json.dumps(payload, ensure_ascii=False))
    print("__YT_CASCADE_JSON_END__")

if __name__ == "__main__":
    main()
'''


@dataclass(frozen=True)
class ColabTranscribeOutput:
    segments: list[TranscriptSegment]
    detected_language: str | None
    model_load_sec: float
    asr_sec: float
    device_used: str
    model_used: str
    model_fingerprint: str
    warnings: list[str]


class ColabAsrAdapter:
    """Opt-in L3 Colab GPU ASR adapter backed by `google-colab-cli` and ColabRuntimeLifecycleManager."""

    def __init__(self, colab_bin: str = "colab") -> None:
        self.colab_bin = colab_bin

    def is_available(self) -> bool:
        return shutil.which(self.colab_bin) is not None

    def transcribe(
        self,
        audio_path: Path,
        *,
        video_id: str,
        language: str | None,
        model: str = "turbo",
        deadline: float,
        lifecycle: ColabRuntimeLifecycleManager,
    ) -> ColabTranscribeOutput:
        if not self.is_available():
            raise CascadeError(
                ErrorCode.COLAB_FAILED,
                "google-colab-cli (`colab`) is not installed or not on PATH.",
            )

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise CascadeError(ErrorCode.TIMEOUT, "Request deadline exceeded before starting Colab L3.")

        session_id = f"ytc-{video_id}-{uuid.uuid4().hex[:8]}"
        lifecycle.register_session(session_id, status="CREATED")
        warnings: list[str] = []

        try:
            lifecycle.update_status(session_id, "ACTIVE")
            lang_arg = language.split("-")[0] if language else "auto"

            with tempfile.TemporaryDirectory(prefix=f"yt_colab_{video_id}_") as td:
                script_path = Path(td) / f"{video_id}_transcribe.py"
                script_path.write_text(_REMOTE_WHISPER_SCRIPT, encoding="utf-8")

                # `colab run` creates a disposable T4 GPU runtime, runs the script, and tears it down
                cmd = [
                    self.colab_bin,
                    "run",
                    "--gpu",
                    "T4",
                    "--upload",
                    str(audio_path),
                    str(script_path),
                    "--",
                    audio_path.name,
                    model,
                    lang_arg,
                ]
                try:
                    res = subprocess.run(
                        cmd,
                        capture_output=True,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                        timeout=max(1.0, deadline - time.monotonic()),
                    )
                except subprocess.TimeoutExpired as exc:
                    lifecycle.mark_cleanup_pending(session_id, reason="Colab run exceeded shared deadline.")
                    raise CascadeError(
                        ErrorCode.TIMEOUT,
                        "Colab L3 transcription exceeded request deadline; remote cleanup scheduled.",
                    ) from exc

                if res.returncode != 0:
                    err_tail = (res.stderr or res.stdout or "").strip()[-300:]
                    lifecycle.mark_cleanup_pending(session_id, reason=f"colab run exited {res.returncode}")
                    raise CascadeError(
                        ErrorCode.COLAB_FAILED,
                        f"Colab L3 execution failed (rc={res.returncode}): {err_tail}",
                    )

                raw_out = res.stdout
                start_marker = "__YT_CASCADE_JSON_START__"
                end_marker = "__YT_CASCADE_JSON_END__"
                if start_marker not in raw_out or end_marker not in raw_out:
                    raise CascadeError(
                        ErrorCode.COLAB_FAILED,
                        "Colab L3 output did not contain structured JSON markers.",
                    )
                json_str = raw_out.split(start_marker, 1)[1].split(end_marker, 1)[0].strip()
                data = json.loads(json_str)

                segments = [
                    TranscriptSegment(
                        start=float(s["start"]),
                        end=float(s["end"]),
                        text=str(s["text"]),
                    )
                    for s in data.get("segments", [])
                ]
                return ColabTranscribeOutput(
                    segments=segments,
                    detected_language=data.get("language") or lang_arg,
                    model_load_sec=float(data.get("model_load_sec", 0.0)),
                    asr_sec=float(data.get("asr_sec", 0.0)),
                    device_used=str(data.get("device_used", "colab:cuda")),
                    model_used=model,
                    model_fingerprint=f"colab-openai-whisper:{model}:fp16",
                    warnings=warnings,
                )
        except Exception as exc:
            lifecycle.mark_cleanup_pending(session_id, reason=f"{type(exc).__name__}: {exc}")
            if isinstance(exc, CascadeError):
                raise
            raise CascadeError(ErrorCode.COLAB_FAILED, f"Colab L3 error: {type(exc).__name__}") from exc
        finally:
            # Always persist CLEANUP_PENDING and run grace-period cleanup even if deadline expired
            lifecycle.mark_cleanup_pending(session_id)
            rec = lifecycle.cleanup_session(session_id)
            if rec is not None and rec.status == "STOP_FAILED":
                msg = (
                    f"Colab session '{session_id}' cleanup reached STOP_FAILED "
                    f"(retries={rec.retry_count}). Verify manually via `colab sessions`."
                )
                print(msg, file=sys.stderr)
                warnings.append(msg)
