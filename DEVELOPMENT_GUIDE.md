# 🛠️ yt-transcript-cascade-mcp 開發者指南 (DEVELOPMENT_GUIDE.md)

本文件提供本專案的本機環境設定、架構邊界、測試執行與基準測試操作流程（與 `PRD v1.2.2`、`GLOSSARY v1.2.2` 與 `.agents/AGENTS.md` 同步）。

---

## 1. 分層架構 (Lean DDD)

```text
src/yt_transcript_cascade/
├── domain/                  # 純領域模型、Pydantic Schema、URL 白名單與路徑防穿越
│   ├── models.py
│   └── url_parser.py
├── application/             # 業務流程協調、CascadePolicy、多維品質分析、Markdown/JSON/SRT 渲染
│   ├── cascade_policy.py
│   ├── formatter.py
│   ├── orchestrator.py
│   └── quality_analyzer.py
├── infrastructure/          # 外部適配器：yt-dlp、雙層快取、跨程序鎖、硬體探測、ASR 引擎
│   ├── asr_engines.py
│   ├── file_lock.py
│   ├── hardware_detector.py
│   ├── source_cache.py
│   └── yt_dlp_adapter.py
└── server.py                # FastMCP / MCPServer Stdio 入口（4 個單一職責工具）
```

---

## 2. 本機環境建置（Windows / Linux / macOS）

建議使用 **Python 3.12** 建立獨立虛擬環境（兼顧 `mcp 2.x` 與 `faster-whisper` / CUDA 12 相容性）：

```powershell
# 1. 建立隔離虛擬環境
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip

# 2. 安裝核心套件（可編輯模式）
.\.venv\Scripts\python.exe -m pip install -e .

# 3. 選配：安裝本機高速 ASR（CPU / NVIDIA CUDA）
.\.venv\Scripts\python.exe -m pip install faster-whisper
# 若使用 Windows NVIDIA GPU 且系統未裝 CUDA 12 Toolkit：
.\.venv\Scripts\python.exe -m pip install nvidia-cublas-cu12 nvidia-cudnn-cu12
```

> ℹ️ Windows 環境下，`hardware_detector.register_nvidia_pip_dll_dirs()` 會於啟動時自動透過 `os.add_dll_directory()` 註冊 pip 安裝的 CUDA 12 DLL，免手動設定系統 `PATH`。

---

## 3. 執行離線測試套件與基準測試

```powershell
# 執行 M1 + M2 + M3 全量離線單元、合約、併發與安全測試
.\.venv\Scripts\python.exe -m unittest discover -s tests -v

# 針對固定本地音檔執行 RTF 基準測試
.\.venv\Scripts\python.exe scripts\benchmark_asr.py path\to\sample.m4a --language zh
```

---

## 4. 關鍵工程守則與防坑備忘

1. **Stdio JSON-RPC 純淨**：所有日誌一律輸出至 `sys.stderr`，禁止在 `stdout` 印出任何非協定字元。
2. **安全紅線**：所有 `subprocess.run` 強制使用 `list[str]`（嚴禁 `shell=True`），且於 Windows 顯式指定 `encoding="utf-8", errors="replace"`。
3. **雙層快取與跨程序鎖**：
   - Source Cache 存 L1 字幕（優先 `MANUAL_SUBTITLE` > `AUTO_SUBTITLE`）；
   - ASR Cache 存 L2/L3 語音辨識結果，鍵包含完整 9 元組指紋（含 `model_fingerprint`、`audio_fingerprint`、`config_hash`），任一欄位異動自動失效；
   - 跨程序鎖 (`CrossProcessFileLock`) 於 Windows 使用 `msvcrt.locking`、POSIX 使用 `fcntl.flock`，且絕不刪除常駐鎖檔以避開 Inode TOCTOU。
4. **雙版本 MCP SDK 相容**：`server.py` 採 `try/except ImportError` 同時支援 `mcp >= 2.0` (`MCPServer`) 與 `mcp 1.x` (`FastMCP`)。
