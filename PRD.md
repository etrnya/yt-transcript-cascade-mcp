# 📋 yt-transcript-cascade-mcp 產品需求規格書 (PRD v1.2.2 — M0 最終凍結版)

> **版本**：`v1.2.2`（2026-11-22，完成最終覆審 4 項契約補強＋2 項細部補充，正式進入 M0 凍結確認）
> **作者**：`etrnya`
> **授權**：`Apache License 2.0`（第三方聲明見 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)）
> **產品定位**：**面向 AI Agent 的非侵入式 YouTube 語音轉錄基礎設施——專注「如何可靠地取得 YouTube 逐字稿，並輸出結構化 Markdown (`.md`) 檔供後續 AI Agent 調用與人類查看」**

---

## 1. 背景、定位與成熟度標籤

### 1.1 核心產品價值
將不穩定的 YouTube 逐字稿取得流程，封裝為 AI Agent 可直接呼叫的標準 MCP 服務：
1. 自動處理字幕缺失、限流與跨環境故障轉移，無須使用者手動切換工具。
2. 零瀏覽器 Cookie、免 YouTube 帳號登入、非侵入式架構（不宣稱「絕對零風控」——YouTube 仍可能針對 IP 限流，Colab 亦受其服務條款約束）。
3. 每一份逐字稿皆附帶**不可遺失的來源追溯（Provenance）**、時間戳與獨立品質指標；失敗時回傳結構化 `ErrorCode`。

### 1.2 功能成熟度標籤
全倉文件嚴格標註狀態：`PLANNED`（規劃中）、`EXPERIMENTAL`（實驗性）、`VERIFIED`（特定環境實測通過）、`SUPPORTED`（通過發布閘門正式支援）。

---

## 2. `execution_mode`、雲端授權語意與 `CascadePolicy` 錯誤契約

### 2.1 `execution_mode` 與雙授權參數矩陣（零歧義契約）
將「故障自動轉移雲端」與「主動指定雲端處理」解耦為兩個獨立參數：
* `enable_colab_fallback: bool = False`：僅授權 `AUTO` 模式下，於 L2 無法執行時自動降級至 L3。
* `allow_cloud_processing: bool = False`：僅授權 `COLAB_ONLY` 模式下，使用者主動要求將本機下載之音檔上傳至 Colab 處理。

| `execution_mode` | L1（字幕直取） | L2（本機 ASR） | L3（Colab 雲端） | 必要雲端授權 | 備註 |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **`AUTO`**（預設） | 執行 | L1 失敗且 Policy 允許時執行 | 僅於 L2 無法執行時考慮 | 觸發 L3 需 `enable_colab_fallback=True` | L1/L2 無須授權；**實際進入 L3 時必於音檔上傳前發出 `cloud_upload_notice`** |
| **`L1_ONLY`** | 執行 | **禁止** | **禁止** | 無 | 僅嘗試公開字幕 |
| **`LOCAL_ASR_ONLY`** | **跳過** | 直接下載音檔並執行 | **禁止** | 無 | 強制本機離線辨識 |
| **`COLAB_ONLY`** | **跳過** | **跳過**（仍於本機下載音檔） | 執行 | **強制 `allow_cloud_processing=True`** | 未授權立即回傳 `INVALID_INPUT`；**音檔上傳前必發出 `cloud_upload_notice`** |

> **雲端告知與授權分離原則**：**任何模式（不論 `AUTO` 或 `COLAB_ONLY`）只要實際進入 L3，均須於音檔傳輸前執行雲端上傳告知（寫入 `stderr` 與回應之 `cloud_upload_notice`）**；告知本身絕不可替代使用者事前授權。

### 2.2 完整錯誤決策模型（14 種 `ErrorCode` 全量覆蓋）
* `cascade_action`：首次發生該錯誤時的處置。
* `max_retries`：**額外**重試次數（不含首次執行）。
* `exhausted_action`：重試或有限降級耗盡後的明確最終處置。
* **`ASR_OOM` 有限降級序列**：`turbo (fp16) → small (GPU int8) → small (CPU int8) → TERMINATE`——**從實際可用的第一個策略開始執行、直接跳過不支援的硬體策略、同策略禁止重複執行、所有策略耗盡後終止**。
* `user_action_required`：與「是否自動降級」分離；`ASR_BACKEND_UNAVAILABLE` 在 `enable_colab_fallback=True` 時由系統自動降級 L3（`user_action_required=False`），未授權時終止並設為 `True` 提示安裝。

```python
class CascadeAction(str, Enum):
    RETRY = "RETRY"
    FALLBACK_L2 = "FALLBACK_L2"
    FALLBACK_L3 = "FALLBACK_L3"
    DOWNGRADE_MODEL = "DOWNGRADE_MODEL"
    TERMINATE = "TERMINATE"

@dataclass(frozen=True)
class ErrorDecision:
    error_code: ErrorCode
    retryable: bool
    max_retries: int
    cascade_action: CascadeAction
    exhausted_action: CascadeAction
    user_action_required: bool
```

| # | `ErrorCode` | 觸發條件 | `retryable` | `max_retries` | `cascade_action` | `exhausted_action` | `user_action_required` |
| :--- | :--- | :--- | :---: | :---: | :--- | :--- | :---: |
| 1 | `NO_SUBTITLE` | 影片無字幕或目標語系不存在 | `False` | `0` | `FALLBACK_L2` | `TERMINATE` | `False` |
| 2 | `RATE_LIMITED` | 字幕端點回傳 `HTTP 429` | `False` | `0` | `FALLBACK_L2` | `TERMINATE` | `False` |
| 3 | `SUBTITLE_403` | 已核實影片公開可存取，僅字幕端點 `403` | `False` | `0` | `FALLBACK_L2` | `TERMINATE` | `False` |
| 4 | `NETWORK_ERROR` | 暫時性連線逾時或 DNS 錯誤 | `True` | `2` | `RETRY` | `TERMINATE` | `False` |
| 5 | `VIDEO_UNAVAILABLE` | 影片已刪除、下架、地區封鎖不可存取 | `False` | `0` | `TERMINATE` | `TERMINATE` | `False` |
| 6 | `AUTH_REQUIRED` | 私人影片、會員限定、需登入驗證之 `403` | `False` | `0` | `TERMINATE` | `TERMINATE` | `True` |
| 7 | `AUDIO_DOWNLOAD_FAILED` | L2/L3 音檔下載失敗或格式毀損 | `True` | `1` | `RETRY` | `TERMINATE` | `False` |
| 8 | `ASR_OOM` | 本機 GPU 載入或推論顯存不足 | `False` | `2` *(降級步數上限)* | `DOWNGRADE_MODEL` | `TERMINATE` | `False` |
| 9 | `ASR_BACKEND_UNAVAILABLE` | 本機無可用 ASR 套件或探測失敗 | `False` | `0` | `FALLBACK_L3` *(需授權)* | `TERMINATE` | 依是否授權 L3 動態決定 |
| 10 | `COLAB_FAILED` | L3 授權失效、無可用 GPU 或遠端執行失敗 | `False` | `0` | `TERMINATE` | `TERMINATE` | `True` |
| 11 | `TIMEOUT` | 超過單次請求共用 `deadline` | `False` | `0` | `TERMINATE` | `TERMINATE` | `False` |
| 12 | `INVALID_INPUT` | 非法 URL、無效／衝突參數或雲端未授權 | `False` | `0` | `TERMINATE` | `TERMINATE` | `True` |
| 13 | `PATH_VIOLATION` | `save_to_path` 企圖穿越允許寫入目錄 | `False` | `0` | `TERMINATE` | `TERMINATE` | `True` |
| 14 | `UNKNOWN_ERROR` | 未預期之系統例外 | `False` | `0` | `TERMINATE` | `TERMINATE` | `False` |

---

## 3. L2 跨硬體探測與「引擎 × 後端 × 模型」契約

### 3.1 分層能力探測 (`HardwareDetector`)
產出不可變之 `HardwareProfile`，獨立區分底層後端與指定引擎實際能力：
1. **Hardware Discovery**：偵測 OS、CPU、RAM、GPU 與總／可用 VRAM。
2. **Backend Probe**：各自檢查 `PyTorch CUDA`、`CTranslate2 CUDA`、`CTranslate2 CPU`、`PyTorch MPS`（絕不將 PyTorch 可用等同於 CTranslate2 可用）。
3. **Engine & Lightweight Inference Probe**：驗證目標 `AsrEngine` 可實際載入與執行。
4. **Execution Strategy Selection**：依 §3.2 矩陣選擇策略（AMD ROCm 未完成實機驗證前不列入正式支援矩陣）。

### 3.2 「引擎 × 後端 × 模型」預設策略矩陣

| 硬體與後端條件 | 狀態 | 適用引擎與推論精度 | 模型策略與 OOM 降級 |
| :--- | :--- | :--- | :--- |
| **NVIDIA CUDA（可用 VRAM ≥ 7.5 GB）** | `PLANNED` (M2) | `openai-whisper (PyTorch fp16)` 或 `faster-whisper (CTranslate2 float16)` | 優先 `turbo`；OOM 依有限序列降級 |
| **NVIDIA CUDA（6–7.5 GB VRAM）** | `PLANNED` (M2) | `faster-whisper (CTranslate2 int8_float16 / int8)` | 探測 `turbo`，失敗降級 `small (int8)` |
| **NVIDIA CUDA（4–6 GB VRAM）** | `PLANNED` (M2) | `faster-whisper (CTranslate2 int8)` | `small`（禁止強載 `turbo`） |
| **純 CPU / VRAM < 4 GB** | `PLANNED` (M2) | `faster-whisper (CTranslate2 cpu int8)` | `small` / `base` |
| **Apple Silicon (`PyTorch MPS`)** | `EXPERIMENTAL` | `openai-whisper (mps)` / `faster-whisper (cpu int8)` | 探測異常自動退回 CPU `int8` |

*(統一 `AsrEngine` Protocol 維持 `check_available`、`load_model`、`transcribe`、`release` 四方法；`openai-whisper` 不使用 `compute_type="int8"`。)*

---

## 4. L3 Colab 備援：`ColabRuntimeLifecycleManager`（M4 階段）

### 4.1 平台與成本邊界
* **平台支援**：上游 `google-colab-cli` 官方支援 **macOS / Linux / WSL2**；Windows 原生標記 **`EXPERIMENTAL`**。
* **資源與成本**：不保證永遠分配 T4 或固定 CU 費率；2026-11-22 單次實測（Tesla T4、`openai-whisper turbo`、16:32 中文片，轉錄 ~58s、全程 ~94s、約 `0.02 CU`）僅作為特定條件參考基準。

### 4.2 持久化生命週期與最終失敗政策

```python
@dataclass
class ColabSessionRecord:
    session_id: str        # 須通過 ^[a-zA-Z0-9_-]{1,64}$ 白名單驗證，禁直接拼入 shell
    owner_id: str          # 固定前綴識別本專案（如 "yt-cascade-mcp:<host_hash>"）
    created_at: str
    last_activity_at: str
    status: str            # CREATED | ACTIVE | CLEANUP_PENDING | STOPPING | STOPPED | STOP_FAILED | RETRY_PENDING
    cleanup_required: bool
    retry_count: int       # 持久化計數，不因重啟無限重置
    last_error: str | None = None
```

* **狀態流轉與最終失敗規則**：
  1. 狀態檔位於 `~/.cache/yt-transcript-cascade/colab_sessions.json`（權限限制現行使用者讀寫 `0600` / Windows 使用者專屬目錄），讀寫須通過 JSON Schema 驗證並採 **Atomic Write**（`.tmp` + `os.replace`）+ 檔案鎖。
  2. 正常流程：`CREATED → ACTIVE → CLEANUP_PENDING → STOPPING → STOPPED`；若 `colab stop` 回報該 Session 已不存在，直接轉為 `STOPPED` 視為清理完成。
  3. 失敗重試：`STOPPING → STOP_FAILED → RETRY_PENDING → STOPPING`（最大 `retry_count = 3`）。
  4. **達到 3 次重試上限之最終政策**：
     - 狀態停駐於 **`STOP_FAILED`**，**嚴禁刪除該筆持久化紀錄**，且**嚴禁將未確認停止之 Session 標記為 `STOPPED`**。
     - 保留 `last_error` 診斷資訊並於 `warnings` / `check_cascade_environment` 明確提示使用者手動確認 `colab sessions`。
     - 下次 MCP 啟動時**不重置 `retry_count`**；僅於距離 `last_activity_at` 超過冷卻期（預設 15 分鐘）時最多再補試 1 次，**該次補試亦計入同一個持久化 `retry_count`（絕不因程序重啟而重新取得 3 次完整重試額度）**，仍失敗則維持 `STOP_FAILED` 待人工處理。

---

## 5. 資料模型、Provenance、品質評估與快取契約

### 5.1 `TranscriptResult` 與獨立多維度 `TranscriptQualityReport`
* **來源追溯（Provenance — 不可遺失）**：
  - `source_type`（統一枚舉，取代易誤導之 `source_quality`）：`MANUAL_SUBTITLE` | `AUTO_SUBTITLE` | `LOCAL_ASR` | `CLOUD_ASR`
  - `is_generated`、`tier_used`、`engine_used`、`model_used`、`model_fingerprint`、`device_used`、`source_language`、`cached`
* **四段耗時與效能指標**：`download_sec`、`model_load_sec`、`asr_sec`、`total_elapsed_sec`、`rtf`（`asr_sec / duration_sec`）。
* **獨立品質指標（不合併單一 `quality_score`）**：
  - `structural_ok`（`timestamp_monotonic`、`empty_segment_count`）
  - `duration_coverage_ratio`（時間軸覆蓋度，**不代表辨識正確率**）
  - `repetition_anomaly_count`（連續重複異常片段數）
  - `language_consistent`（辨識語系與預期是否一致）
  - `confidence_note`（若有則附註「僅為引擎估計指標，非校準正確率」）
  - `status`：`OK` / `WARNING`
* **存檔路徑語義**：`server_saved_path` 明確代表 MCP Server 本機路徑（遠端部署時 Client 未必可直接存取；短於 `max_output_chars` 時內聯回傳）。

### 5.2 雙層快取與併發契約
* **Source Cache (L1)**：鍵為 `(video_id, language, source_type, schema_version)`。
* **ASR Cache (L2/L3)**：鍵為 `(video_id, language, engine, engine_version, model, model_fingerprint, audio_fingerprint, config_hash, schema_version)`。
  - `audio_fingerprint`：下載後音檔內容之 `SHA-256`（完整 64 hex）。
  - `model_fingerprint`：本機模型權重檔／manifest 之 SHA-256 摘要或官方權重版本識別。
  - `config_hash`：標準化轉錄設定 JSON（鍵排序）之 `SHA-256`。
  - 快取檔案損壞、JSON 驗證失敗或指紋不符時，自動淘汰並重新產出。
* **寫入與併發**：所有寫入採 **Atomic Write**；M1/M2 採 **In-process Single-flight**；**跨程序檔案鎖歸屬 M3**；SQLite 僅預留儲存介面。

---

## 6. MCP Tools 完整契約與同步取消規則

### 6.1 同步執行、共用 `deadline` 與取消清理時間邊界（M1 減法決策）
* M1 採**同步執行**（完整非同步 `TaskStatus` 任務系統正式延後至 Post-M1）。
* **`timeout_sec` 限制的是主要請求執行時間**，L1 / L2 / L3 共用同一個 `deadline = time.monotonic() + timeout_sec`。
* **取消與逾時清理時間邊界**：
  - `Request Cancelled / Deadline Exceeded → 終止子進程（yt-dlp/ffmpeg/worker）→ 釋放本機資源 → 刪除暫存音檔（除非 keep_audio=True）→ 若已建立 Colab Session，寫入 CLEANUP_PENDING 排程遠端清理`。
  - **本機資源釋放與暫存清理屬於必要的取消收尾（享獨立短暫收尾寬限期，預設 5 秒，不受主請求已耗盡之 `deadline` 阻斷）**；Colab 遠端清理透過持久化 `CLEANUP_PENDING` 延後完成，主請求逾時**絕不可省略清理紀錄**。
* **Stdio 純淨原則**：所有日誌一律輸出至 `stderr`，嚴禁污染 `stdout` JSON-RPC。

### 6.2 4 個單一職責 MCP Tools 正式 JSON Schema 契約
所有 MCP Tools 之輸入與輸出必須遵守可機器驗證之 JSON Schema（於 M1 以 Pydantic Model 生成並由 Contract Tests 驗證），以下為核心正式定義：

```json
{
  "$defs": {
    "ErrorCode": {
      "type": "string",
      "enum": [
        "NO_SUBTITLE", "RATE_LIMITED", "SUBTITLE_403", "NETWORK_ERROR",
        "VIDEO_UNAVAILABLE", "AUTH_REQUIRED", "AUDIO_DOWNLOAD_FAILED",
        "ASR_OOM", "ASR_BACKEND_UNAVAILABLE", "COLAB_FAILED", "TIMEOUT",
        "INVALID_INPUT", "PATH_VIOLATION", "UNKNOWN_ERROR"
      ]
    },
    "SourceType": {
      "type": "string",
      "enum": ["MANUAL_SUBTITLE", "AUTO_SUBTITLE", "LOCAL_ASR", "CLOUD_ASR"]
    },
    "HardwareProfile": {
      "type": "object",
      "required": ["platform", "accelerator", "device_name", "backend", "backend_verified", "supported_engines"],
      "properties": {
        "platform": {"type": "string", "enum": ["windows", "darwin", "linux"]},
        "accelerator": {"type": "string", "enum": ["nvidia", "apple_silicon", "amd", "cpu"]},
        "device_name": {"type": "string"},
        "vram_total_mb": {"type": ["integer", "null"]},
        "vram_available_mb": {"type": ["integer", "null"]},
        "backend": {"type": "string", "enum": ["pytorch_cuda", "ctranslate2_cuda", "pytorch_mps", "cpu_int8"]},
        "backend_verified": {"type": "boolean"},
        "supported_engines": {"type": "array", "items": {"type": "string"}}
      }
    },
    "TranscriptResult": {
      "type": "object",
      "required": [
        "video_id", "source_type", "is_generated", "tier_used", "segments",
        "quality_report", "cached", "total_elapsed_sec", "warnings"
      ],
      "properties": {
        "video_id": {"type": "string", "pattern": "^[A-Za-z0-9_-]{11}$"},
        "title": {"type": ["string", "null"]},
        "channel": {"type": ["string", "null"]},
        "duration_sec": {"type": ["number", "null"]},
        "source_type": {"$ref": "#/$defs/SourceType"},
        "source_language": {"type": ["string", "null"]},
        "is_generated": {"type": "boolean"},
        "tier_used": {"type": "string", "enum": ["L1_SUBTITLE", "L2_LOCAL_ASR", "L3_COLAB_GPU"]},
        "engine_used": {"type": ["string", "null"]},
        "model_used": {"type": ["string", "null"]},
        "model_fingerprint": {"type": ["string", "null"]},
        "device_used": {"type": ["string", "null"]},
        "download_sec": {"type": ["number", "null"]},
        "model_load_sec": {"type": ["number", "null"]},
        "asr_sec": {"type": ["number", "null"]},
        "total_elapsed_sec": {"type": "number"},
        "rtf": {"type": ["number", "null"]},
        "server_saved_path": {"type": ["string", "null"]},
        "cloud_upload_notice": {"type": ["string", "null"]},
        "segments": {
          "type": "array",
          "items": {
            "type": "object",
            "required": ["start", "end", "text"],
            "properties": {
              "start": {"type": "number"},
              "end": {"type": "number"},
              "text": {"type": "string"},
              "confidence": {"type": ["number", "null"]},
              "speaker_id": {"type": ["string", "null"]}
            }
          }
        },
        "quality_report": {
          "type": "object",
          "required": ["structural_ok", "empty_segment_count", "repetition_anomaly_count", "duration_coverage_ratio", "status"],
          "properties": {
            "structural_ok": {"type": "boolean"},
            "empty_segment_count": {"type": "integer"},
            "repetition_anomaly_count": {"type": "integer"},
            "duration_coverage_ratio": {"type": ["number", "null"]},
            "language_consistent": {"type": ["boolean", "null"]},
            "confidence_note": {"type": ["string", "null"]},
            "status": {"type": "string", "enum": ["OK", "WARNING"]}
          }
        },
        "cached": {"type": "boolean"},
        "warnings": {"type": "array", "items": {"type": "string"}}
      }
    }
  }
}
```

#### Tool 1：`get_youtube_transcript` (`inputSchema` 核心契約)
```json
{
  "type": "object",
  "required": ["url"],
  "additionalProperties": false,
  "properties": {
    "url": {"type": "string", "minLength": 11},
    "languages": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 10, "default": ["zh-Hant", "zh-Hans", "zh", "en"]},
    "execution_mode": {"type": "string", "enum": ["AUTO", "L1_ONLY", "LOCAL_ASR_ONLY", "COLAB_ONLY"], "default": "AUTO"},
    "enable_colab_fallback": {"type": "boolean", "default": false},
    "allow_cloud_processing": {"type": "boolean", "default": false},
    "engine": {"type": ["string", "null"], "enum": ["openai-whisper", "faster-whisper", "sensevoice", null], "default": null},
    "model": {"type": ["string", "null"], "default": null},
    "output_format": {"type": "string", "enum": ["markdown", "json", "srt"], "default": "markdown"},
    "save_to_path": {"type": ["string", "null"], "default": null},
    "max_output_chars": {"type": "integer", "minimum": 1000, "maximum": 200000, "default": 12000},
    "timeout_sec": {"type": "integer", "minimum": 10, "maximum": 3600, "default": 300},
    "force_refresh": {"type": "boolean", "default": false},
    "keep_audio": {"type": "boolean", "default": false}
  }
}
```
* **輸出契約**：成功回傳 `{ "ok": true, "result": <TranscriptResult>, "rendered_text": "<Markdown/JSON/SRT>" }`；失敗回傳 `{ "ok": false, "error": { "code": <ErrorCode>, "message": "...", "user_action_required": bool, "warnings": [...] } }`。預設 `output_format="markdown"` 產出附 YAML Frontmatter（含來源追溯與品質報告）之結構化 `.md` 內容，供後續 AI Agent 直接調用與人類查閱。

#### Tool 2：`check_cascade_environment`
* `inputSchema`: `{"type": "object", "properties": {}, "additionalProperties": false}`。
* `outputSchema`: `{ "ok": true, "hardware": <HardwareProfile>, "yt_dlp_version": string|null, "ffmpeg_available": boolean, "installed_asr_engines": string[], "colab_ready": boolean, "pending_colab_sessions": integer }`。

#### Tool 3：`get_transcript_cache_stats`
* `inputSchema`: `{"type": "object", "properties": {}, "additionalProperties": false}`。
* `outputSchema`: `{ "ok": true, "source_cache": {"entries": int, "bytes": int}, "asr_cache": {"entries": int, "bytes": int}, "ttl_days": int, "max_bytes": int }`。

#### Tool 4：`clear_transcript_cache`（完整布林互斥真值表）
* `inputSchema`: `{"type": "object", "additionalProperties": false, "properties": {"video_id": {"type": ["string", "null"], "pattern": "^[A-Za-z0-9_-]{11}$", "default": null}, "expired_only": {"type": "boolean", "default": false}, "clear_all": {"type": "boolean", "default": false}}}`。

| `video_id` | `expired_only` | `clear_all` | 合法性與行為 |
| :--- | :---: | :---: | :--- |
| `None` | `False` | `False` | **`INVALID_INPUT`**（未指定任何有效操作） |
| 有效 11 碼 ID | `False` | `False` | 清理該指定影片快取 |
| `None` | `True` | `False` | 僅清理已過期快取條目 |
| `None` | `False` | `True` | 清理全部快取條目 |
| 有效 11 碼 ID | `True` | `False` | **`INVALID_INPUT`**（衝突組合） |
| 有效 11 碼 ID | `False` | `True` | **`INVALID_INPUT`**（衝突組合） |
| `None` | `True` | `True` | **`INVALID_INPUT`**（衝突組合） |
| 有效 11 碼 ID | `True` | `True` | **`INVALID_INPUT`**（衝突組合） |

---

## 7. 安全性、測試與 5 階段開發 Roadmap

* **安全性紅線**：YouTube 網域白名單 + 11 碼 `video_id` 正則、`subprocess.run` 參數陣列（禁 `shell=True`）、`save_to_path` `Path.resolve()` 防穿越、暫存檔一律以 `video_id` 命名（禁以影片標題作檔名）、L3 明確揭露音檔上傳雲端。
* **測試分層**：CI 離線測試（Unit / Contract / Failure Injection）＋ 發布閘門實機測試（真實 YouTube 影片、固定多場景測試音檔 `RTF` 與四段耗時基準、L3 實機清理）。
* **5 階段 Roadmap**：
  - **M0（契約凍結）**：`PRD v1.2.2` + 39 列追溯矩陣 + 全倉 5 份文件一致。
  - **M1（MVP）**：FastMCP（4 Tools + 正式 JSON Schema）、URL Parser、L1 `yt-dlp` 單語字幕、基礎 Source Cache、`CascadePolicy`、基礎 `QualityAnalyzer`、預設結構化 `.md` 輸出、離線與實機驗收。
  - **M2（L2 本機 ASR）**：`HardwareDetector`、音檔下載、`openai-whisper` / `faster-whisper`（CUDA + CPU `int8`，MPS 實驗性）、`RTF` 實測。
  - **M3（可靠性與安全產品化）**：ASR Cache 完整鍵（含 `model_fingerprint`）與 LRU/TTL、跨程序鎖、安全測試與 Benchmark。
  - **M4（L3 Colab 備援）**：`ColabRuntimeLifecycleManager`、持久化恢復、實機中斷清理驗證 → 多語 `README` 發布 `v1.0.0`。

---

## 8. 需求追溯矩陣 (Requirement Traceability Matrix — 共 39 列獨立驗收)

*狀態：`CLOSED` = 文件與契約修訂完成；`CLOSED*` = 文件與契約已修訂，待對應里程碑實機／自動化測試驗證。*

| # | 審查 ID | 對應章節 | 修訂狀態 | 獨立驗收方式與里程碑 |
| :--- | :--- | :--- | :---: | :--- |
| 1 | **R1 P0-1** Colab Windows 相容性 | §4.1 | `CLOSED` | 文件平台宣告一致檢查（M0） |
| 2 | **R1 P0-2** 引擎 × 硬體 × 模型契約 | §3.1, §3.2 | `CLOSED*` | M2 實機 CUDA / CPU / OOM 降級測試 |
| 3 | **R1 P0-3** 錯誤分類與 CascadePolicy | §2.2 | `CLOSED*` | M1 全部 14 種 ErrorCode 單元測試 |
| 4 | **R1 P0-4** Colab 異常清理機制 | §4.2 | `CLOSED*` | M4 中斷注入與殘留 Session 恢復測試 |
| 5 | **R1 P0-5** Google AI Pro / CU 宣稱 | §4.1 | `CLOSED` | 文件宣稱與基準標註檢查（M0） |
| 6 | **R1 P0-6** 零風控宣稱收斂 | §1.1 | `CLOSED` | README / PRD 標語一致性檢查（M0） |
| 7 | **R1 P1-1** 四階段硬體探測 | §3.1 | `CLOSED*` | M2 Backend + Engine Capability Probe 測試 |
| 8 | **R1 P1-2** 統一 ASR Adapter Contract | §3.2 | `CLOSED*` | M2 Contract Tests 驗證一致輸出 |
| 9 | **R1 P1-3** TranscriptResult & Quality | §5.1 | `CLOSED*` | M1/M2 Schema 與品質異常單元測試 |
| 10 | **R1 P1-4** 雙層快取與併發去重 | §5.2 | `CLOSED*` | M1 基礎快取 + M3 Single-flight / 跨程序鎖 |
| 11 | **R1 P1-5** MCP 參數與 Stdio 純淨 | §6.1, §6.2 | `CLOSED*` | M1 Stdio JSON-RPC 純淨性實測 |
| 12 | **R1 P2-1** 精簡 DDD 與獨立 Policy | §2.2, `AGENTS.md` | `CLOSED` | 架構審查與模組依賴檢查（M1） |
| 13 | **R1 P2-2** 快取查詢與清理拆分 | §6.2 | `CLOSED` | 4 個單一職責 MCP Tool 契約檢查（M1） |
| 14 | **R1 P2-3** 測試分層與實機閘門 | §7 | `CLOSED*` | CI 離線測試 + 實機發布閘門（M1–M4） |
| 15 | **R1 P2-4** 第三方授權獨立聲明 | `THIRD_PARTY_NOTICES.md` | `CLOSED` | 授權合規與模型權重揭露檢查（M0） |
| 16 | **R1 P2-5** 改用 `RTF` 基準指標 | §5.1, §7 | `CLOSED*` | M2/M3 固定音檔 `RTF` 實測 |
| 17 | **R2 P0-A** `execution_mode` 完整矩陣 | §2.1 | `CLOSED*` | M1 四模式單元測試 |
| 18 | **R2 P0-B** 14 種 ErrorCode 完整契約 | §2.2 | `CLOSED*` | M1 `ErrorDecision` 100% 覆蓋測試 |
| 19 | **R2 P0-C** Colab Ownership & 狀態機 | §4.2 | `CLOSED*` | M4 `owner_id` 隔離與不存在即停止測試 |
| 20 | **R2 P0-D** `TaskStatus` 延後決策 | §6.1 | `CLOSED` | M1 採同步執行 + Timeout（減法治理） |
| 21 | **R2 P1-A** ROCm 與 PyTorch/CTranslate2 分離 | §3.1, §3.2 | `CLOSED*` | M2 後端獨立探測測試；ROCm 移出正式矩陣 |
| 22 | **R2 P1-B** 多維品質指標與 confidence 語意 | §5.1 | `CLOSED*` | M1/M2 獨立指標單元測試（無單一總分） |
| 23 | **R2 P1-C** Cache Key、Atomic Write、Single-flight | §5.2 | `CLOSED*` | M1 原子寫入 + M3 跨程序併發測試 |
| 24 | **R2 P1-D** `server_saved_path` 語義 | §5.1, §6.2 | `CLOSED` | M1 輸出 Schema 欄位命名檢查 |
| 25 | **R2 P1-E** 四段耗時與 Provenance | §5.1 | `CLOSED*` | M1/M2 Provenance 與耗時欄位斷言 |
| 26 | **R3 P0-1** `execution_mode` 雲端授權語意分離 | §2.1, §6.2 | `CLOSED*` | M1 `enable_colab_fallback` vs `allow_cloud_processing` 測試 |
| 27 | **R3 P0-2** `ErrorDecision` 重試耗盡與 OOM 有限序列 | §2.2 | `CLOSED*` | M1 `exhausted_action` 與 M2 有限降級測試 |
| 28 | **R3 P0-3** Colab 三次清理失敗最終政策與安全驗證 | §4.2 | `CLOSED*` | M4 保留 `STOP_FAILED`、不重置計數與 ID 白名單測試 |
| 29 | **R3 P0-4** 同步共用 `deadline` 與取消清理鏈 | §6.1 | `CLOSED*` | M1/M2 逾時與取消子進程/暫存清理測試 |
| 30 | **R3 P1-1** ASR Cache `model_fingerprint` 與 `audio_fingerprint` | §5.2 | `CLOSED*` | M2/M3 指紋變更觸發快取失效測試 |
| 31 | **R3 P1-2** `source_quality` 統一改為 `source_type` | §5.1, `GLOSSARY.md` | `CLOSED` | 全倉命名一致性檢查（M0） |
| 32 | **R3 P1-3** 4 個 MCP Tools 完整 JSON Schema | §6.2 | `CLOSED*` | M1 MCP Tool Schema 校驗測試 |
| 33 | **R3 P1-4** `clear_transcript_cache` 互斥規則 | §6.2 | `CLOSED*` | M1 衝突組合回傳 `INVALID_INPUT` 測試 |
| 34 | **R3 P1-5** 追溯矩陣獨立拆分無合併列 | §8 | `CLOSED` | 本表共 35 列逐項獨立追蹤（M0） |
| 35 | **R3 P1-6** 修正 `§3.2` 引用與全倉 5 檔同步 | 全倉 5 檔 | `CLOSED` | 跨文件交叉引用與關鍵字靜態檢查（M0） |
| 36 | **R4 P0-1** `AUTO` 觸發 L3 雲端上傳前告知 | §2.1, §6.2 | `CLOSED*` | M4 `AUTO` 進 L3 上傳前觸發 `cloud_upload_notice` 測試 |
| 37 | **R4 P0-2** 正式可機器驗證之 MCP JSON Schema | §6.2 | `CLOSED*` | M1 Pydantic Model 與 Contract Tests 自動驗證 |
| 38 | **R4 P1-1** Cache 清理 Boolean 預設值真值表 | §6.2 | `CLOSED*` | M1 全部 8 種布林組合單元測試 |
| 39 | **R4 P1-2** 共用 `deadline` 與取消收尾時間邊界 | §6.1 | `CLOSED*` | M1/M4 逾時寬限收尾與 `CLEANUP_PENDING` 持久化測試 |
