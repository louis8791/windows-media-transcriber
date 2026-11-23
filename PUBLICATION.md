# 公開版本紀錄

## 2026-11-23

- 來源：私人開發庫 main `27927d5`，與上一版相比新增一鍵安裝程式、桌面資料夾遷移、Whisper 教材核對與訓練守門的更新。
- 去識別方式同上一版：本機路徑改為 `E:\media-transcriber`、使用者路徑改為 `YOUR_USER`、具名媒體改為 `synthetic_media_case`；排除內部驗收紀錄、截圖、只依賴私人資料的實驗腳本與帶有雲端分享連結的交付文件。發布前已掃描具名媒體、個人帳號、雲端檔案 ID 與常見憑證格式。
- 補上 `work/`、`logs/` 等程式與測試預期存在的空資料夾佔位，避免全新 clone 時測試找不到目錄。
- PowerShell 與 BAT 檔恢復與開發庫相同的 UTF-8 BOM，避免 Windows PowerShell 5.1 讀錯中文。
- 測試：在 Windows x64 實機，將本快照解壓到全新資料夾，模型以硬連結、whisper-cli 與 FFmpeg 以複本放在專案內，用專案的 Python 環境執行 14 個 Python 測試模組：**120 項全部通過**（0 失敗、0 錯誤、0 略過）。私人開發庫同一組測試同樣 120 項全過。
- 未執行：`test_watcher_startup.py` 與 3 個 `.ps1` 測試，它們會啟動 PowerShell，本次未取得執行授權；真實長片轉錄、Ollama 校稿、全新電腦的首次安裝。
- 已知限制：模型與工具必須位於專案資料夾內，以 junction 連到外部位置會被安全檢查拒絕（屬設計行為）。`config.example.json` 未列出 `ollama_chunk_chars`、`ollama_context_size`、`job_identity_input_root`，程式會使用內建預設值。

## 2026-10-19（上一版）

- 來源：朋友包功能分支 `2f14767`，全新歷史。
- 依 config.example 建立 config.local 後執行 107 項測試：97 通過、8 失敗、2 錯誤。當時未逐項釐清原因；本版補上空資料夾佔位，並在完整環境重測後全數通過。
