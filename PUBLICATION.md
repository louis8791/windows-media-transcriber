# 公開版本紀錄

- 日期：2026-10-19。
- 來源：已推送的朋友包功能分支，提交 `2f1476746848e673c2d05ea41f45cb54a33122bf`。
- 公開版全新歷史；排除原始歷史、內部驗收資料及私人工作紀錄。
- 真實媒體識別替換為合成名稱，範例使用 YOUR_USER。
- 未包含本機 dirty 工作樹修改。
- Python AST 語法與 JSON 解析檢查通過；已檢查發布內容的具名媒體識別與常見憑證格式。
- 依 config.example 建立 ignored config.local 後，以既有 Python 環境執行 107 項測試：97 通過、8 失敗、2 錯誤。不是全綠版本。
- 失敗涉及安全管線對工具／環境的前置條件及 watcher 啟動；錯誤涉及 ASCII 模型路徑限制與 Ollama mock 服務。尚未逐項釐清根因，本次不修改核心功能或測試來消除失敗。
- 初次裸 clone 測試另遇 test_artifacts 目錄缺失，公開版加入空目錄佔位；測試仍須先建立自己的 config.local.json。
- 真實模型、媒體與 fresh install 本輪未執行。
