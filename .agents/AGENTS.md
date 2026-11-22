# 🤖 yt-transcript-cascade-mcp AI 專案專規 (.agents/AGENTS.md v1.2.2)

本專案遵守全域 `AGENTS.md` 所有規範，並嚴格執行以下專案級工程約束（與 `PRD v1.2.2`、`GLOSSARY v1.2.2` 100% 同步）：

---

## 1. 字典對齊、減法治理與精簡分層
1. 所有變數、枚舉值、`ErrorCode`、`ErrorDecision` 與 MCP Tool 參數必須嚴格對齊 [GLOSSARY.md](../GLOSSARY.md) 與 [PRD.md](../PRD.md)。
2. **減法治理原則**：禁止新增無實際價值的抽象層（Manager / Factory / Repository 泛濫）；僅保留 `domain/`、`application/`、`infrastructure/`、`server.py` 四層必要邊界。
3. M1 採**同步執行 + 共用 `deadline`**；完整非同步任務系統延後至 Post-M1，M1 不得提前實作背景任務佇列、SQLite 或跨程序鎖。
4. 所有錯誤重試與降級決策**必須集中於 `CascadePolicy`**（含 `cascade_action` 與 `exhausted_action`），禁止散落於各 Adapter。

## 2. 安全性、隱私與 Stdio 純淨紅線
1. **Stdio JSON-RPC 純淨**：所有日誌與除錯輸出**一律寫入 `stderr`**，嚴禁在 `stdout` 印出非 MCP 協定文字。
2. **零瀏覽器 Cookie / 非侵入式**：嚴禁加入讀取瀏覽器個人 Cookie 或逆向 NotebookLM RPC 的功能。
3. **防注入與路徑穿越**：
   - URL 必須通過 YouTube 官方網域白名單與 11 碼 `video_id` 正則驗證。
   - 所有 `subprocess.run` 強制使用 `list[str]` 參數陣列，**嚴禁 `shell=True`**。
   - `save_to_path` 必須 `Path.resolve()` 驗證位於允許目錄內（回傳欄位命名為 `server_saved_path`）；暫存與快取檔名**一律以 `video_id` 命名，嚴禁以影片標題命名**。
   - 雲端授權嚴格分離：`AUTO` 降級看 `enable_colab_fallback`、`COLAB_ONLY` 看 `allow_cloud_processing`；`clear_transcript_cache` 嚴格三選一互斥。
4. **憑證零洩漏**：OAuth 憑證與環境變數嚴禁寫入日誌或回傳至 MCP Client。

## 3. 實戰防坑與可靠性鐵律
1. **L1 防 429 單語請求**：嚴禁一次拉取多語系（`zh.*,en.*`），必須先 `--dump-json` 確認可用語系，僅下載命中的單一語系。
2. **L2 分層探測與後端分離**：
   - 獨立探測 `PyTorch CUDA`、`CTranslate2 CUDA`、`CTranslate2 CPU`、`PyTorch MPS`；嚴格區分 `openai-whisper`（`fp16/fp32`）與 `faster-whisper`（`int8/float16`）。
   - AMD ROCm 未經實機驗證前不列入正式支援矩陣。
   - `ASR_OOM` 必走有限降級序列（`turbo fp16 → small GPU int8 → small CPU int8 → TERMINATE`）。
   - 快取與狀態檔寫入一律採 **Atomic Write**（`.tmp` + `os.replace`）。
3. **L3 `ColabRuntimeLifecycleManager`（M4）**：
   - 持久化紀錄須含 `owner_id` 與通過正則白名單驗證之 `session_id`；狀態檔權限限制現行使用者讀寫。
   - 遠端 Session 已不存在即視為 `STOPPED`；清理 3 次失敗停駐 `STOP_FAILED`（不刪除紀錄、不誤標 `STOPPED`、重啟不無限重置 `retry_count`）。
   - Colab CUDA 13 遠端腳本固定採原生 PyTorch CUDA + `openai-whisper (turbo)`，避開 `ctranslate2` (`libcublas.so.12`) 與 PyAV 衝突。
4. **Windows 編碼與誠實狀態**：所有 `subprocess.run` 顯式指定 `encoding="utf-8", errors="replace"`；未通過實機驗證之功能僅可標記 `PLANNED` 或 `EXPERIMENTAL`，不可預先宣稱 `SUPPORTED`；品質報告一律獨立多維指標（`source_type` 不作正確率背書，不合併單一總分）。
