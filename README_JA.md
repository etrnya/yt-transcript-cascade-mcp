# 🎙️ yt-transcript-cascade-mcp

> **コア目標：YouTube の文字起こしを確実に取得し、後続の AI Agent や人間がそのまま扱える構造化 Markdown (`.md`) を自動出力する。**
> **ブラウザ Cookie 不要・YouTube ログイン不要** — **L1 単一言語字幕取得 → L2 ローカルマルチハードウェア ASR → L3 オプトイン Google Colab GPU フォールバック**の 3 層カスケード構成に、来歴（Provenance）・構造化 `ErrorCode`・多次元品質レポートを備えています。

[![License](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![Spec](https://img.shields.io/badge/Spec-PRD_v1.2.2-blue.svg)](PRD.md)
[![Stage](https://img.shields.io/badge/Version-v1.0.0-emerald.svg)](PRD.md)

🌐 **言語切替 / Languages**: [繁體中文](README.md) | [English](README_EN.md) | [简体中文](README_ZH_CN.md) | **日本語**

---

## ✨ 主な機能

1. **4 つの単一責務 MCP ツール（Stdio JSON-RPC 純粋出力、全ログは `stderr` へ）**：
   - `get_youtube_transcript`：共通 `deadline` と `execution_mode`（`AUTO` / `L1_ONLY` / `LOCAL_ASR_ONLY` / `COLAB_ONLY`）に対応し、YAML Frontmatter 付き `.md` を出力。
   - `check_cascade_environment`：ハードウェア構成、`yt-dlp`、`ffmpeg`、利用可能 ASR エンジン、未解放 Colab セッション数を診断。
   - `get_transcript_cache_stats`：Source Cache / ASR Cache の読み取り専用統計。
   - `clear_transcript_cache`：厳格な 3 択排他クリア（`video_id` | `expired_only` | `clear_all`）。
2. **3 層フォールバックとクラウド認可の明確な分離**：
   - **L1 (`yt-dlp`)**：メタデータ確認後に一致した単一言語のみを取得し `HTTP 429` を回避。
   - **L2（ローカル ASR）**：`faster-whisper`（NVIDIA CUDA / CPU `int8`）および `openai-whisper` に対応し、有限 OOM ダウングレード連鎖（`turbo fp16 → small GPU int8 → small CPU int8 → TERMINATE`）を内蔵。
   - **L3（オプトイン Colab GPU）**：`AUTO` では `enable_colab_fallback=true`、`COLAB_ONLY` では `allow_cloud_processing=true` を必須とし、音声アップロード前に必ず `cloud_upload_notice` を通知。`ColabRuntimeLifecycleManager` により `owner_id` 分離・3 回失敗時の `STOP_FAILED` 永続化・クールダウン後の安全リカバリを実施。
3. **2 層キャッシュとクロスプロセスロック**：
   - Source Cache（L1）＋ 9 要素フィンガープリント ASR Cache（L2/L3）、Atomic Write、TTL / LRU 容量制御、およびクロスプロセスファイルロック（Windows `msvcrt` / POSIX `fcntl.flock`）。

---

## 📊 実機ベンチマーク参考値 (`RTF`)

* **環境**：Windows 11 / NVIDIA GeForce RTX 4060 Laptop GPU (8 GB VRAM) / `faster-whisper 1.2.1` (`float16`, `large-v3-turbo`)
* **結果**：16 分 32 秒の音声を **`40.94 秒`** で文字起こし完了（**`RTF = 0.0413`**、約 **24.2 倍速**・完全オフライン無料）

---

## 📚 ドキュメント・ライセンス

* [PRD.md](PRD.md) | [GLOSSARY.md](GLOSSARY.md) | [DEVELOPMENT_GUIDE.md](DEVELOPMENT_GUIDE.md) | [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)

Copyright 2026 `etrnya`. コアコードは **[Apache License 2.0](LICENSE)** で公開されています。
