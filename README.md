# Windows 影音轉文字

本機離線的 Windows 音訊／影片轉錄工具。把檔案放進資料夾、雙擊開始，產生臺灣正體中文逐字稿與 SRT 字幕，並附一份審查報告，列出該回去聽的時間點。

設計目標是**長時間無人看管也能穩定跑完**：卡住會受控退出，中斷後從斷點接續，開跑前先擋下會失敗的條件，出問題時留下可排查的紀錄。

## 能做什麼

- **一般使用**：放檔、雙擊「開始轉錄」。轉檔、切段、辨識、繁體化、合併字幕、產生審查報告都自動完成，可以排到下班後跑。
- **審查報告**：檢查 6 類問題，包括重複牆、樣板幻覺（例如音樂段冒出影音平台的字幕套語）、超長字幕、語速異常、字幕重疊、疑似漏句，並標出時間點。原稿不改寫，只告訴人該去聽哪裡。
- **局部修補**：某一段誤判語言或出錯時，用 `scripts/retranscribe_range.ps1` 只重轉那個時間範圍，替換對應字幕，不必整支重跑。
- **選用校稿**：本機 Ollama 模型產生校稿候選，和正式字幕分開存放。校稿服務失敗時，正式字幕照常交付並標示警告，不產生假的校稿檔。
- **一鍵安裝給別人用**：`packaging/installer/FriendInstaller.cs` 會編成一個約 30 KB 的 exe。雙擊後它會自己找到或下載約 8 GB 的離線環境（支援續傳），核對大小與 SHA-256，解壓、建立桌面「音檔」「文字檔」資料夾和捷徑，最後自動開啟程式。不需要管理員權限、7-Zip 或 Docker。

## 穩定性設計

| 情境 | 系統行為 |
|---|---|
| 子程序卡住、沒有輸出 | 無進度超過時間上限就透過 Windows Job Object 終止整棵程序樹，不留孤兒程序 |
| 長片轉到一半中斷 | 每 5 分鐘一段 checkpoint，依來源、模型、工具、參數指紋驗證；重開只重做未完成的段落 |
| 磁碟不足、缺工具、模型被換、媒體無法解碼 | 開跑前檢查並擋下，原音檔與舊稿都不動 |
| 同一個檔案一直失敗 | 監看模式最多 3 次，之後隔離該檔，同批其他檔案照常完成 |
| 本機校稿服務掛掉 | 主稿照常交付並標 `completed_with_warning`，本次不再反覆呼叫 |
| 同時開兩個視窗 | Windows named mutex 只允許一個轉錄程序 |

每份工作會在 `logs/status/` 留下逐階段時間戳、失敗原因與重試次數，出問題時可以直接查到卡在哪一步。

## 技術組成

whisper.cpp（large-v3）· Silero VAD · FFmpeg · OpenCC（s2tw）· Ollama（選用）· Python · PowerShell · C#（安裝程式）

## 入口與設定

- `開始轉錄.bat`：本機轉錄與操作入口。
- `只做Qwen校稿.bat`：已有文字的獨立校稿入口。
- `config.example.json`：設定範例，請以自己的路徑建立 `config.local.json`。
- `config/model-manifest.json`：模型與工具的來源與 SHA 契約。
- `scripts/install_windows.ps1`、`scripts/install_on_this_pc.ps1`：安裝相關腳本，執行前請先閱讀它們的下載與安裝行為。
- `scripts/build_friend_package.py`、`scripts/build_friend_installer.py`、`packaging/`：離線包與一鍵安裝程式的製作來源。Repo 裡沒有已打包好的下載檔。
- `scripts/migrate_desktop_folders.py`：把舊版桌面資料夾名稱搬到「音檔」「文字檔」，既有 checkpoint 仍可沿用。

公開 Git 不含 Whisper、Silero、Ollama 模型或 FFmpeg 等執行檔，須依設定與安裝腳本準備。

## 驗證

```powershell
Copy-Item config.example.json config.local.json
# 先依本機環境修改 config.local.json，指向已安裝的模型與工具
python -m unittest discover -s tests -v
```

2026-11-23 在 Windows 實機上以完整模型與工具環境執行 14 個 Python 測試模組，共 120 項，全部通過。另有 4 個呼叫 PowerShell 的測試本次沒有執行，詳見 `PUBLICATION.md`。測試通過代表程式行為符合契約，不代表任何一份逐字稿的內容已經人工接受。

## 公開邊界

見 `PUBLIC_SCOPE.md`。目前未指定開源授權；公開可讀不代表已授予再散布或商業使用授權。
