"""Four-stage HardwareDetector producing HardwareProfile (Discovery -> Backend -> Probe -> Strategy)."""

from __future__ import annotations

import platform
import shutil
import subprocess
from dataclasses import dataclass
from typing import Literal

from ..domain.models import HardwareProfile


@dataclass(frozen=True)
class AsrStrategy:
    """Concrete execution strategy pairing one engine, backend, compute type, and model."""

    engine: Literal["openai-whisper", "faster-whisper"]
    device: Literal["cuda", "mps", "cpu"]
    compute_type: str  # "fp16"/"fp32" for openai-whisper; "float16"/"int8_float16"/"int8" for faster-whisper
    model_name: str


def _query_nvidia_smi() -> tuple[str, int, int] | None:
    """Return (gpu_name, total_mb, free_mb) via nvidia-smi if available."""
    exe = shutil.which("nvidia-smi")
    if not exe:
        return None
    try:
        res = subprocess.run(
            [exe, "--query-gpu=name,memory.total,memory.free", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
        )
        if res.returncode != 0 or not res.stdout.strip():
            return None
        first_line = res.stdout.strip().splitlines()[0]
        parts = [p.strip() for p in first_line.split(",")]
        if len(parts) >= 3:
            return parts[0], int(float(parts[1])), int(float(parts[2]))
    except Exception:
        return None
    return None


def _probe_pytorch_cuda() -> bool:
    try:
        import torch  # type: ignore

        if not torch.cuda.is_available():
            return False
        # Stage 3 lightweight capability probe
        t = torch.zeros((2, 2), device="cuda", dtype=torch.float16)
        _ = float((t + 1.0).sum().item())
        return True
    except Exception:
        return False


def _probe_pytorch_mps() -> bool:
    try:
        import torch  # type: ignore

        if not (hasattr(torch.backends, "mps") and torch.backends.mps.is_available()):
            return False
        t = torch.zeros((2, 2), device="mps")
        _ = float((t + 1.0).sum().item())
        return True
    except Exception:
        return False


def _probe_ctranslate2_cuda() -> bool:
    try:
        import ctranslate2  # type: ignore

        return ctranslate2.get_cuda_device_count() > 0
    except Exception:
        return False


def _has_module(name: str) -> bool:
    try:
        __import__(name)
        return True
    except Exception:
        return False


class HardwareDetector:
    """Detects hardware and verifies independent PyTorch / CTranslate2 backends."""

    def detect(self) -> HardwareProfile:
        sys_plat = platform.system().lower()
        plat: Literal["windows", "darwin", "linux"] = (
            "windows" if "windows" in sys_plat else ("darwin" if "darwin" in sys_plat else "linux")
        )

        # Stage 1: Hardware Discovery
        nv = _query_nvidia_smi()
        accelerator: Literal["nvidia", "apple_silicon", "amd", "cpu"] = "cpu"
        device_name = platform.processor() or "CPU"
        vram_total_mb: int | None = None
        vram_available_mb: int | None = None

        if nv is not None:
            accelerator = "nvidia"
            device_name, vram_total_mb, vram_available_mb = nv
        elif plat == "darwin" and platform.machine().lower() in ("arm64", "aarch64"):
            accelerator = "apple_silicon"
            device_name = "Apple Silicon"

        # Stage 2 & 3: Independent Backend & Capability Probes
        has_openai_whisper = _has_module("whisper")
        has_faster_whisper = _has_module("faster_whisper")

        supported_engines: list[str] = []
        if has_openai_whisper:
            supported_engines.append("openai-whisper")
        if has_faster_whisper:
            supported_engines.append("faster-whisper")

        backend: Literal["pytorch_cuda", "ctranslate2_cuda", "pytorch_mps", "cpu_int8"] = "cpu_int8"
        backend_verified = False

        if accelerator == "nvidia":
            if has_faster_whisper and _probe_ctranslate2_cuda():
                backend = "ctranslate2_cuda"
                backend_verified = True
            elif has_openai_whisper and _probe_pytorch_cuda():
                backend = "pytorch_cuda"
                backend_verified = True
            elif has_faster_whisper or has_openai_whisper:
                backend = "cpu_int8"
                backend_verified = True
        elif accelerator == "apple_silicon":
            if has_openai_whisper and _probe_pytorch_mps():
                backend = "pytorch_mps"
                backend_verified = True
            elif has_faster_whisper or has_openai_whisper:
                backend = "cpu_int8"
                backend_verified = True
        else:
            if has_faster_whisper or has_openai_whisper:
                backend = "cpu_int8"
                backend_verified = True

        return HardwareProfile(
            platform=plat,
            accelerator=accelerator,
            device_name=device_name,
            vram_total_mb=vram_total_mb,
            vram_available_mb=vram_available_mb,
            backend=backend,
            backend_verified=backend_verified,
            supported_engines=supported_engines,
        )


def build_oom_fallback_chain(
    profile: HardwareProfile,
    *,
    preferred_engine: str | None = None,
    preferred_model: str | None = None,
) -> list[AsrStrategy]:
    """Build a finite, deduplicated ASR strategy chain starting from the first viable option.

    Strictly follows PRD v1.2.2 §2.2 & §3.2:
    - Never uses compute_type='int8' on openai-whisper.
    - Starts from the first supported hardware strategy; skips unsupported hardware.
    - Guarantees no strategy is repeated.
    """
    has_fw = "faster-whisper" in profile.supported_engines
    has_ow = "openai-whisper" in profile.supported_engines
    if not (has_fw or has_ow):
        return []

    vram_avail = profile.vram_available_mb or profile.vram_total_mb or 0
    candidates: list[AsrStrategy] = []

    def add(strat: AsrStrategy) -> None:
        if strat not in candidates:
            candidates.append(strat)

    # Explicit user preference goes first if compatible with installed engines
    if preferred_engine == "faster-whisper" and has_fw:
        if profile.backend == "ctranslate2_cuda" and vram_avail >= 4000:
            ct = "float16" if vram_avail >= 7500 else "int8_float16"
            add(AsrStrategy("faster-whisper", "cuda", ct, preferred_model or ("turbo" if vram_avail >= 6000 else "small")))
        add(AsrStrategy("faster-whisper", "cpu", "int8", preferred_model or "small"))
    elif preferred_engine == "openai-whisper" and has_ow:
        if profile.backend == "pytorch_cuda" and vram_avail >= 6000:
            add(AsrStrategy("openai-whisper", "cuda", "fp16", preferred_model or "turbo"))
        elif profile.backend == "pytorch_mps":
            add(AsrStrategy("openai-whisper", "mps", "fp32", preferred_model or "small"))
        add(AsrStrategy("openai-whisper", "cpu", "fp32", preferred_model or "small"))
    else:
        # Automatic PRD §3.2 matrix
        if profile.backend == "ctranslate2_cuda" and has_fw:
            if vram_avail >= 7500:
                add(AsrStrategy("faster-whisper", "cuda", "float16", preferred_model or "turbo"))
                add(AsrStrategy("faster-whisper", "cuda", "int8", "small"))
            elif vram_avail >= 6000:
                add(AsrStrategy("faster-whisper", "cuda", "int8_float16", preferred_model or "turbo"))
                add(AsrStrategy("faster-whisper", "cuda", "int8", "small"))
            elif vram_avail >= 4000:
                add(AsrStrategy("faster-whisper", "cuda", "int8", preferred_model or "small"))
        elif profile.backend == "pytorch_cuda" and has_ow:
            if vram_avail >= 7500:
                add(AsrStrategy("openai-whisper", "cuda", "fp16", preferred_model or "turbo"))
                add(AsrStrategy("openai-whisper", "cuda", "fp16", "small"))
            elif vram_avail >= 6000:
                add(AsrStrategy("openai-whisper", "cuda", "fp16", preferred_model or "small"))
        elif profile.backend == "pytorch_mps" and has_ow:
            add(AsrStrategy("openai-whisper", "mps", "fp32", preferred_model or "small"))

        # Universal CPU fallback tail (finite termination)
        if has_fw:
            add(AsrStrategy("faster-whisper", "cpu", "int8", "small"))
        elif has_ow:
            add(AsrStrategy("openai-whisper", "cpu", "fp32", "small"))

    return candidates
