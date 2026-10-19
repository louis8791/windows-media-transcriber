# Windows 影音轉文字

本機優先的 Windows 音訊／影片轉錄程式，提供逐字稿、SRT 字幕、獨立校稿候選及人工音訊審查工具。

本公開版從已推送的朋友包功能分支抽出完整程式與測試，建立全新歷史。它不包含私人媒體、逐字稿、內部驗收紀錄、模型、工具執行檔或本機尚未提交的修改。

## 入口與設定

- `開始轉錄.bat`：本機轉錄與操作入口。
- `只做Qwen校稿.bat`：已有文字的獨立校稿入口。
- `config.example.json`：設定範例，請以自己的路徑建立 `config.local.json`。
- `config/model-manifest.json`：模型與工具來源契約。
- `requirements.txt`：Python 依賴。
- `scripts/install_windows.ps1`、`scripts/install_on_this_pc.ps1`：安裝相關腳本，執行前閱讀其下載與安裝行為。
- `scripts/build_friend_package.py`、`packaging/`：朋友包製作來源，並非已打包的下載檔。

公開 Git 不含 Whisper、Silero、Ollama 模型或 FFmpeg 等執行檔；須依設定與安裝腳本準備。離線使用以必要資源已完整安裝為前提。

## 流程與品質邊界

程式涵蓋分段轉錄、checkpoint 續跑、正體中文轉換、逐字稿與字幕交付、獨立校稿及人工審查。校稿候選可能修改錯誤，必須對照原音；測試或檔案產生不等於逐字內容已經人工接受。

## 驗證

```powershell
Copy-Item config.example.json config.local.json
# 先依本機環境修改 config.local.json
python -m unittest discover -s tests -v
```

**本公開快照測試未全數通過：107 項中有 8 項失敗、2 項錯誤，詳 PUBLICATION.md。**

本次公開整理的靜態、測試結果與邊界見 `PUBLICATION.md`。未在本次重新執行真實模型、長片、完整首次下載或朋友電腦驗收。

## 公開邊界

見 `PUBLIC_SCOPE.md`。目前未指定開源授權；公開可讀不代表已授予再散布或商業使用授權。
