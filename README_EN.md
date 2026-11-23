# 🎙️ yt-transcript-cascade-mcp

> **Core Goal: Reliably fetch YouTube transcripts and emit structured Markdown (`.md`) files ready for downstream AI Agents and human reading.**
> **Zero browser cookies, no YouTube account login required** — powered by a three-tier cascade (**L1 Single-Language Subtitle → L2 Local Cross-Hardware Offline ASR → L3 Opt-In Google Colab GPU Fallback**) with full provenance, structured `ErrorCode` classification, and multi-dimensional quality reports.

[![License](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![Spec](https://img.shields.io/badge/Spec-PRD_v1.2.2-blue.svg)](PRD.md)
[![Stage](https://img.shields.io/badge/Version-v1.0.0-emerald.svg)](PRD.md)

🌐 **Languages**: [繁體中文](README.md) | **English** | [简体中文](README_ZH_CN.md) | [日本語](README_JA.md)

---

## ✨ Key Capabilities

1. **4 Single-Responsibility MCP Tools (Stdio JSON-RPC pure, all logs to `stderr`)**:
   - `get_youtube_transcript`: Main entry point with shared `deadline`, `execution_mode` (`AUTO` / `L1_ONLY` / `LOCAL_ASR_ONLY` / `COLAB_ONLY`), and default YAML Frontmatter `.md` output.
   - `check_cascade_environment`: Reports `HardwareProfile`, `yt-dlp`, `ffmpeg`, installed ASR engines, `colab_ready`, and `pending_colab_sessions`.
   - `get_transcript_cache_stats`: Read-only stats for both Source Cache and ASR Cache.
   - `clear_transcript_cache`: Strict 3-way mutual exclusion (`video_id` | `expired_only` | `clear_all`).
2. **Three-Tier Cascade & Decoupled Cloud Authorization**:
   - **L1 (`yt-dlp`)**: Inspects metadata first and downloads only the single matched language to prevent `HTTP 429`.
   - **L2 (Local Offline ASR)**: Supports `faster-whisper` (NVIDIA CUDA / CPU `int8`) and `openai-whisper` (PyTorch CUDA / MPS `EXPERIMENTAL`) with finite OOM downgrade chain (`turbo fp16 → small GPU int8 → small CPU int8 → TERMINATE`).
   - **L3 (Opt-In Google Colab GPU)**: Requires explicit `enable_colab_fallback=true` (in `AUTO`) or `allow_cloud_processing=true` (in `COLAB_ONLY`). Emits `cloud_upload_notice` to `stderr` and response **before** uploading audio. Protected by `ColabRuntimeLifecycleManager` (`owner_id` isolation, 3-strike `STOP_FAILED` persistence, cooldown recovery).
3. **Dual-Layer Cache & Cross-Process Locking**:
   - **Source Cache (L1)** + **ASR Cache (L2/L3)** with 9-tuple fingerprint key (`video_id`, `language`, `engine`, `engine_version`, `model`, `model_fingerprint`, `audio_fingerprint`, `config_hash`, `schema_version`), Atomic Write, TTL, and LRU `max_bytes` eviction.
   - Portable cross-process lock (`msvcrt` on Windows / `fcntl.flock` on POSIX) + in-process single-flight deduplication.

---

## 📊 Verified Local Benchmark (`RTF`)

* **Hardware**: Windows 11 / Intel i5-12500H / **NVIDIA GeForce RTX 4060 Laptop GPU (8,188 MiB VRAM)**
* **Engine**: `faster-whisper 1.2.1` (`cuda`, `float16`, `deepdml/faster-whisper-large-v3-turbo-ct2`, `beam_size=5`, `vad_filter=True`)
* **Sample**: 16m 32s (`992.3s`) Mandarin talk audio decoded via `ffmpeg` to 16kHz mono PCM
* **Result**: **`40.94s`** pure ASR → **`RTF = 0.0413`** (~**24.2x real-time**, 488 segments, \$0 offline)

---

## 🚀 Quick Start

```bash
# 1. Create a Python 3.12 virtual environment and install
python -m venv .venv
.venv/bin/pip install -e .

# 2. Optional: install local ASR engine
.venv/bin/pip install faster-whisper

# 3. Run offline test suite (M1–M4)
.venv/bin/python -m unittest discover -s tests -v
```

---

## 📚 Documentation & License

* [PRD.md](PRD.md) — Product Requirements Document v1.2.2 (39-row traceability matrix)
* [GLOSSARY.md](GLOSSARY.md) — Canonical naming & enum dictionary
* [DEVELOPMENT_GUIDE.md](DEVELOPMENT_GUIDE.md) — Local setup, architecture, and benchmarking guide
* [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) — Upstream licenses and model weight disclosures

Copyright 2026 `etrnya`. Licensed under the **[Apache License 2.0](LICENSE)**.
