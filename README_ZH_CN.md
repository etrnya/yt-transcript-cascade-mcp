# 🎙️ yt-transcript-cascade-mcp

> **核心目标：如何可靠地获取 YouTube 逐字稿，并自动输出结构化 Markdown (`.md`) 文件，方便后续 AI Agent 直接调用与人工查阅。**
> **零浏览器 Cookie、免 YouTube 账号登录**——采用 **L1 单语字幕直取 → L2 本机跨硬件离线 ASR → L3 可选 Google Colab GPU 备援** 三层容灾降级架构，附带完整来源追溯（Provenance）、14 种结构化 `ErrorCode` 与多维度质量报告。

[![License](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![Spec](https://img.shields.io/badge/Spec-PRD_v1.2.2-blue.svg)](PRD.md)
[![Stage](https://img.shields.io/badge/Version-v1.0.0-emerald.svg)](PRD.md)

🌐 **语言切换 / Languages**: [繁體中文](README.md) | [English](README_EN.md) | **简体中文** | [日本語](README_JA.md)

---

## ✨ 核心特性

1. **4 个单一职责 MCP 工具（Stdio JSON-RPC 纯净输出，所有日志走 `stderr`）**：
   - `get_youtube_transcript`：主入口，支持共用 `deadline`、`execution_mode`（`AUTO` / `L1_ONLY` / `LOCAL_ASR_ONLY` / `COLAB_ONLY`）与带 YAML Frontmatter 的结构化 `.md` 输出。
   - `check_cascade_environment`：探测本机硬件、`yt-dlp`、`ffmpeg`、已安装 ASR 引擎与待清理 Colab 会话数。
   - `get_transcript_cache_stats`：只读查看 Source Cache 与 ASR Cache 统计。
   - `clear_transcript_cache`：严格三选一互斥清理（`video_id` | `expired_only` | `clear_all`）。
2. **三层降级与云端双授权解耦**：
   - **L1 (`yt-dlp`)**：先查元数据再精准下载单一命中语种，避免触发 `HTTP 429`。
   - **L2（本机离线 ASR）**：支持 `faster-whisper`（NVIDIA CUDA / CPU `int8`）与 `openai-whisper`，内置有限 OOM 降级链（`turbo fp16 → small GPU int8 → small CPU int8 → TERMINATE`）。
   - **L3（可选 Colab GPU 备援）**：`AUTO` 模式需显式开启 `enable_colab_fallback=true`，`COLAB_ONLY` 模式强制要求 `allow_cloud_processing=true`；上传音频前必先输出 `cloud_upload_notice`，并由 `ColabRuntimeLifecycleManager` 负责 `owner_id` 隔离与 3 次失败停驻 `STOP_FAILED` 持久化恢复。
3. **双层缓存与跨进程文件锁**：
   - L1 Source Cache + L2/L3 9 元组指纹 ASR Cache（含 `model_fingerprint`、`audio_fingerprint`、`config_hash`），支持 Atomic Write、TTL 与 LRU `max_bytes` 淘汰，以及跨平台跨进程文件锁（Windows `msvcrt` / POSIX `fcntl.flock`）。

---

## 📊 实机基准参考 (`RTF`)

* **测试环境**：Windows 11 / NVIDIA GeForce RTX 4060 Laptop GPU (8 GB VRAM) / `faster-whisper 1.2.1` (`float16`, `large-v3-turbo`)
* **测试结果**：16 分 32 秒中文音频纯 ASR 耗时 **`40.94 秒`**（**`RTF = 0.0413`**，约 **24.2x 实时速度**，0 元离线）

---

## 📚 项目文档与开源协议

* [PRD.md](PRD.md) | [GLOSSARY.md](GLOSSARY.md) | [DEVELOPMENT_GUIDE.md](DEVELOPMENT_GUIDE.md) | [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)

Copyright 2026 `etrnya`. 本项目核心代码采用 **[Apache License 2.0](LICENSE)** 协议开源。
