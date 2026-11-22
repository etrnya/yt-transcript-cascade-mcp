# 📖 yt-transcript-cascade-mcp 術語字典 (GLOSSARY v1.2.2)

> 遵循全域「字典先行 (Dictionary First)」規範：所有文件、MCP Tool 參數、回傳 Schema、錯誤碼與 Python 模組命名，必須嚴格對齊以下標準（與 `PRD v1.2.2` 100% 同步）。

---

## 1. 核心領域與策略模型 (Domain & Policy)

| 中文術語 | 英文標準名稱 | 代碼變數 / 枚舉識別碼 | 定義與邊界說明 |
| :--- | :--- | :--- | :--- |
| **三層容災降級引擎** | Three-Tier Cascade Engine | `CascadeOrchestrator` | 依 `CascadePolicy` 協調 L1 → L2 → L3 的主控應用服務。 |
| **降級決策策略** | Cascade Policy | `CascadePolicy` | 集中封裝錯誤決策邏輯，禁止散落於各 Adapter。 |
| **錯誤決策物件** | Error Decision | `ErrorDecision` (`error_code`, `retryable`, `max_retries`, `cascade_action`, `exhausted_action`, `user_action_required`) | 統一映射首次處置 (`cascade_action`) 與重試耗盡處置 (`exhausted_action`)；`max_retries` 不含首次執行。 |
| **降級動作枚舉** | Cascade Action | `CascadeAction` (`RETRY`, `FALLBACK_L2`, `FALLBACK_L3`, `DOWNGRADE_MODEL`, `TERMINATE`) | 定義錯誤發生後的標準控制流動作。 |
| **執行模式** | Execution Mode | `ExecutionMode` (`AUTO`, `L1_ONLY`, `LOCAL_ASR_ONLY`, `COLAB_ONLY`) | 明確控制 L1/L2/L3 啟用矩陣；與雲端授權參數解耦。 |
| **故障雲端降級授權** | Colab Fallback Opt-in | `enable_colab_fallback` (`bool`, 預設 `False`) | 僅於 `AUTO` 模式下授權 L2 失敗時自動降級 L3。 |
| **主動雲端處理授權** | Cloud Processing Opt-in | `allow_cloud_processing` (`bool`, 預設 `False`) | 僅於 `COLAB_ONLY` 模式下強制要求，授權本機下載音檔後上傳 Colab。 |
| **降級層級** | Cascade Tier | `CascadeTier` (`L1_SUBTITLE`, `L2_LOCAL_ASR`, `L3_COLAB_GPU`) | 標示最終實際產出逐字稿的執行層級。 |
| **標準錯誤碼（14 種）** | Standard Error Code | `ErrorCode` (`NO_SUBTITLE`, `RATE_LIMITED`, `SUBTITLE_403`, `NETWORK_ERROR`, `VIDEO_UNAVAILABLE`, `AUTH_REQUIRED`, `AUDIO_DOWNLOAD_FAILED`, `ASR_OOM`, `ASR_BACKEND_UNAVAILABLE`, `COLAB_FAILED`, `TIMEOUT`, `INVALID_INPUT`, `PATH_VIOLATION`, `UNKNOWN_ERROR`) | 全量覆蓋之結構化錯誤分類。 |
| **共用請求期限** | Shared Request Deadline | `deadline` (`time.monotonic() + timeout_sec`) | M1 同步執行下，L1 / L2 / L3 共用同一個總時間上限。 |

---

## 2. 逐字稿、來源追溯與品質評估 (Transcript, Provenance & Quality)

| 中文術語 | 英文標準名稱 | 代碼變數 / 識別碼 | 定義與說明 |
| :--- | :--- | :--- | :--- |
| **來源類型（統一命名）** | Source Type | `source_type` (`MANUAL_SUBTITLE`, `AUTO_SUBTITLE`, `LOCAL_ASR`, `CLOUD_ASR`) | 標示逐字稿真實來源類型（已淘汰易誤導的舊名，品質改由 `TranscriptQualityReport` 獨立評估）。 |
| **逐字稿分段** | Transcript Segment | `TranscriptSegment` (`start`, `end`, `text`, `confidence`, `speaker_id`) | 單一句帶秒數起訖點之純文字記錄（`confidence` 僅為引擎估計指標，非校準正確率）。 |
| **逐字稿結果封包** | Transcript Result | `TranscriptResult` | 含完整 Provenance（`source_type`、`model_fingerprint` 等）、四段耗時（`download_sec`、`model_load_sec`、`asr_sec`、`total_elapsed_sec`）、`rtf`、`server_saved_path`、`segments`、`quality_report`、`warnings`。 |
| **獨立品質報告** | Transcript Quality Report | `TranscriptQualityReport` | 多項獨立指標（結構完整性、覆蓋率、重複異常、語言一致性）與 `OK`/`WARNING`，不合併為單一總分。 |
| **伺服器端存檔路徑** | Server Saved Path | `server_saved_path` | MCP Server 本機之輸出路徑，明確區分遠端部署下 Client 未必可直接存取。 |
| **即時係數** | Real-Time Factor | `RTF` (`asr_sec / duration_sec`) | 跨硬體 ASR 效能基準指標（數值越小越快）。 |

---

## 3. 硬體探測、快取指紋與 Colab 生命週期

| 中文術語 | 英文標準名稱 | 代碼變數 / 識別碼 | 定義與說明 |
| :--- | :--- | :--- | :--- |
| **標準硬體設定檔** | Hardware Profile | `HardwareProfile` | 獨立區分 `PyTorch CUDA`、`CTranslate2 CUDA`、`CTranslate2 CPU`、`PyTorch MPS` 探測結果。 |
| **語音辨識引擎契約** | ASR Engine Protocol | `AsrEngine` (`check_available`, `load_model`, `transcribe`, `release`) | 所有 L2 ASR 引擎實作的統一協定。 |
| **音檔指紋** | Audio Fingerprint | `audio_fingerprint` | 下載後音檔內容之 `SHA-256` 雜湊值。 |
| **模型權重指紋** | Model Fingerprint | `model_fingerprint` | 模型權重檔／manifest 之 SHA-256 摘要或官方權重版本識別。 |
| **單飛併發去重鎖** | Single-Flight Lock | `single_flight_lock` | M1/M2 採 In-process 併發合併；跨程序檔案鎖歸屬 M3。 |
| **Colab Session 紀錄** | Colab Session Record | `ColabSessionRecord` (`session_id`, `owner_id`, `created_at`, `last_activity_at`, `status`, `cleanup_required`, `retry_count`, `last_error`) | 含資源擁有權 (`owner_id`)、持久化重試次數與錯誤診斷；3 次重試失敗停駐 `STOP_FAILED` 不刪除。 |
| **Colab 生命週期管理器** | Colab Runtime Lifecycle Manager | `ColabRuntimeLifecycleManager` | 採 Atomic Write + 檔案鎖 + `session_id` 白名單校驗與啟動殘留恢復。 |

---

## 4. MCP 工具介面命名 (4 個單一職責工具)

| MCP Tool 名稱 | 職責 |
| :--- | :--- |
| `get_youtube_transcript` | 主入口（同步執行 + 共用 `deadline`）：依 `execution_mode` 取得逐字稿、Provenance 與品質報告。 |
| `check_cascade_environment` | 環境與能力探測：回傳 `HardwareProfile`、`yt-dlp`/`ffmpeg` 狀態、可用引擎與 L3 就緒度。 |
| `get_transcript_cache_stats` | 唯讀查詢：回傳 Source Cache 與 ASR Cache 的筆數、容量與 TTL 統計。 |
| `clear_transcript_cache` | 寫入清理：嚴格三選一互斥（`video_id` \| `expired_only` \| `clear_all`），衝突組合回傳 `INVALID_INPUT`。 |
