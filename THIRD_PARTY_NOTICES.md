# Third-Party Notices & License Disclosures

本文件揭露 `yt-transcript-cascade-mcp`（核心程式碼採 **Apache License 2.0**，`Copyright 2026 etrnya`）所使用、參考或由使用者選配安裝之所有第三方專案、模型權重與外部工具之授權條款。

> **重要合規說明**：本專案核心程式碼採用 Apache License 2.0，並不代表下述所有上游套件、模型權重或外部二進位檔皆自動適用 Apache 2.0。使用者應依實際安裝之元件、模型權重與系統工具之原始授權條件使用。

---

## 1. Python 依賴與框架（未打包原始碼，由使用者透過套件管理器安裝）

| 元件名稱 | 原始碼網址 | 程式碼授權 | 模型權重授權 | 是否隨本倉庫發布 | 備註 |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **mcp (Python SDK)** | https://github.com/modelcontextprotocol/python-sdk | `MIT` | N/A | 否（pip 依賴） | 核心 MCP 伺服器框架 |
| **yt-dlp** | https://github.com/yt-dlp/yt-dlp | `Unlicense` | N/A | 否（外部工具/pip） | L1 字幕與 L2 音檔擷取 |
| **openai-whisper** | https://github.com/openai/whisper | `MIT` | `MIT` | 否（L2 選配） | PyTorch 原生 ASR 引擎 |
| **faster-whisper** | https://github.com/SYSTRAN/faster-whisper | `MIT` | 依所選轉換模型（Whisper 為 `MIT`） | 否（L2 選配） | CTranslate2 量化 ASR 引擎 |
| **SenseVoice** | https://github.com/FunAudioLLM/SenseVoice | `MIT` | **依官方 Model Card 條款**（使用者自行下載） | 否（L2 選配） | 程式碼為 MIT，模型權重須另行遵守其 Model Card 授權 |
| **google-colab-cli** | https://github.com/googlecolab/google-colab-cli | `Apache-2.0` | N/A | 否（L3 選配） | L3 雲端 GPU 備援（上游官方支援 Linux/macOS） |

---

## 2. 系統外部二進位工具（不打包、不靜態連結）

| 工具名稱 | 官方網址 | 授權說明 | 呼叫方式 |
| :--- | :--- | :--- | :--- |
| **FFmpeg / FFprobe** | https://ffmpeg.org/ | 基礎為 `LGPL v2.1+`；若使用者系統安裝的是啟用 GPL 編碼器（如 `libx264`）之建置版本，則該二進位檔適用 `GPL v2/v3`。 | 僅透過獨立系統 CLI 子進程（`subprocess.run` 參數陣列）呼叫，本倉庫不打包亦不靜態連結 FFmpeg。 |

---

## 3. 架構參考與同作者衍生經驗

| 參考專案 | 原始碼網址 | 授權 | 參考範圍 |
| :--- | :--- | :--- | :--- |
| **YTtomp3** | https://github.com/etrnya/YTtomp3 | `etrnya` 自研開源 | 參考其 YouTube 音檔擷取與 Windows 編碼處理經驗 |
| **colab-mcp-starter** | https://github.com/etrnya/colab-mcp-starter | `Apache-2.0` | 參考其零明文憑證 OAuth 授權流程與 Colab GPU 實戰避坑經驗 |
