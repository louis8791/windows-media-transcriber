// One-double-click installer for the offline friend package. Windows refuses to start any EXE
// larger than 4 GB, so the ~8 GB package ZIP is a separate file. Appended to this EXE is UTF-8
// JSON {"payload":name,"bytes":n,"sha256":hex,"manifest_sha256":hex,"url":text}, its 8-byte
// little-endian length and the magic "YYZWPKG1".
// On start it installs to C:\影音轉文字朋友版 without questions: if that folder already holds this
// exact package it only restores the desktop items and opens the app; otherwise it finds the ZIP
// (its own folder, Downloads, Desktop; exact name or exact size), or downloads it from "url" when
// one is configured, checks size and SHA-256, extracts, creates or reuses the desktop folders
// 音檔/文字檔 (recorded in system/user_folders.json), runs the package's own offline
// --prepare-only bootstrap with a sanitized environment, creates the 開始轉錄 shortcut and opens
// the app. An EXE built with --upgrade-from also carries the changed files of one previous release and
// updates such an install in place. It never needs admin rights; without a configured URL it never uses the network.
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.IO.Compression;
using System.Net;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Text;
using System.Text.RegularExpressions;
using System.Threading;
using System.Windows.Forms;

sealed class InstallError : Exception
{
    public readonly int Code;
    public InstallError(int code, string message) : base(message) { Code = code; }
}

sealed class Installer
{
    public const string Magic = "YYZWPKG1";
    public const string PackageFolder = "影音轉文字朋友版";
    public const string ShortcutName = "開始轉錄";
    public static readonly string DefaultTarget = @"C:\" + PackageFolder;

    public Action<string> Status = delegate { };
    public Action<int> Progress = delegate { };
    public volatile bool Cancel;
    public string Target = DefaultTarget, DesktopDir, DownloadsDir, LogPath, PayloadOverride;
    public readonly string PayloadName, PayloadSha256, ManifestSha256, Url, Page, UpgradeFrom, PatchSha256;
    public readonly long PayloadBytes, PatchBytes;
    StreamWriter log;

    public Installer()
    {
        string json = MetadataJson();
        PayloadName = Field(json, "payload"); PayloadBytes = long.Parse(Field(json, "bytes"));
        PayloadSha256 = Field(json, "sha256").ToLowerInvariant(); ManifestSha256 = Field(json, "manifest_sha256").ToLowerInvariant();
        Url = Field(json, "url"); Page = json.Contains("\"page\":") ? Field(json, "page") : "";
        UpgradeFrom = json.Contains("\"upgrade_from\":") ? Field(json, "upgrade_from").ToLowerInvariant() : "";
        PatchSha256 = json.Contains("\"patch_sha256\":") ? Field(json, "patch_sha256").ToLowerInvariant() : "";
        PatchBytes = json.Contains("\"patch_bytes\":") ? long.Parse(Field(json, "patch_bytes")) : 0;
    }

    static string Field(string json, string name)
    {
        string key = "\"" + name + "\":";
        int at = json.IndexOf(key, StringComparison.Ordinal);
        if (at < 0) throw new InstallError(2, "安裝程式資訊不完整，請重新下載安裝程式。");
        at += key.Length;
        while (json[at] == ' ') at++;
        if (json[at] == '"') { int end = json.IndexOf('"', at + 1); return json.Substring(at + 1, end - at - 1); }
        int stop = at;
        while (stop < json.Length && char.IsDigit(json[stop])) stop++;
        return json.Substring(at, stop - at);
    }

    static long metadataStart;
    static string MetadataJson()
    {
        using (FileStream file = new FileStream(Assembly.GetExecutingAssembly().Location, FileMode.Open, FileAccess.Read, FileShare.Read))
        {
            byte[] trailer = new byte[16];
            if (file.Length < 16) throw new InstallError(2, "安裝程式不完整，請重新下載。");
            file.Position = file.Length - 16; file.Read(trailer, 0, 16);
            if (Encoding.ASCII.GetString(trailer, 8, 8) != Magic) throw new InstallError(2, "安裝程式不完整（可能下載中斷），請重新下載。");
            long size = BitConverter.ToInt64(trailer, 0);
            if (size <= 0 || size > 4096 || size > file.Length - 16) throw new InstallError(2, "安裝程式格式錯誤，請重新下載。");
            byte[] data = new byte[size];
            metadataStart = file.Length - 16 - size;
            file.Position = metadataStart; file.Read(data, 0, (int)size);
            return Encoding.UTF8.GetString(data);
        }
    }

    [DllImport("shell32.dll")]
    static extern int SHGetKnownFolderPath([MarshalAs(UnmanagedType.LPStruct)] Guid id, uint flags, IntPtr token, out IntPtr path);
    static string KnownFolder(Guid id)
    {
        IntPtr pointer;
        if (SHGetKnownFolderPath(id, 0, IntPtr.Zero, out pointer) != 0) return null;
        try { return Marshal.PtrToStringUni(pointer); } finally { Marshal.FreeCoTaskMem(pointer); }
    }
    public string Desktop { get { return Path.GetFullPath(DesktopDir ?? KnownFolder(new Guid("B4BFCC3A-DB2C-424C-B029-7FE99A87C641")) ?? Environment.GetFolderPath(Environment.SpecialFolder.DesktopDirectory)); } }
    public string Downloads { get { return DownloadsDir ?? KnownFolder(new Guid("374DE290-123F-4565-9164-39C4925E467B")) ?? Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.UserProfile), "Downloads"); } }

    void Say(string message)
    {
        Status(message);
        if (log != null) { log.WriteLine(DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss") + " " + message); log.Flush(); }
    }
    void OpenLog(string path)
    {
        if (log != null || path == null) return;
        Directory.CreateDirectory(Path.GetDirectoryName(Path.GetFullPath(path)));
        log = new StreamWriter(path, true, new UTF8Encoding(false));
    }
    public void CloseLog() { if (log != null) { log.Dispose(); log = null; } }

    public static string NormalizeTarget(string target)
    {
        target = (target ?? "").Trim().Trim('"');
        if (target.Length == 0) throw new InstallError(3, "請選擇安裝位置。");
        target = Path.GetFullPath(target);
        if (Path.GetPathRoot(target).TrimEnd('\\') == target.TrimEnd('\\')) target = Path.Combine(target, PackageFolder);
        return target.TrimEnd('\\');
    }

    public static string Json(string value)
    {
        StringBuilder text = new StringBuilder("\"");
        foreach (char ch in value)
        {
            if (ch == '\\' || ch == '"') text.Append('\\').Append(ch);
            else if (ch < ' ') text.Append("\\u").Append(((int)ch).ToString("x4"));
            else text.Append(ch);
        }
        return text.Append('"').ToString();
    }

    static string Sha256(string path, long bytes, Action<int> progress, Func<bool> cancel)
    {
        using (FileStream file = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.Read, 1 << 20))
        using (System.Security.Cryptography.SHA256 hash = System.Security.Cryptography.SHA256.Create())
        {
            byte[] buffer = new byte[4 << 20]; long done = 0; int read;
            while ((read = file.Read(buffer, 0, buffer.Length)) > 0)
            {
                if (cancel()) throw new InstallError(1, "已取消安裝。");
                hash.TransformBlock(buffer, 0, read, null, 0);
                done += read; progress((int)(done * 100 / Math.Max(bytes, 1)));
            }
            hash.TransformFinalBlock(buffer, 0, 0);
            return BitConverter.ToString(hash.Hash).Replace("-", "").ToLowerInvariant();
        }
    }

    string InstalledManifestSha()
    {
        string manifest = Path.Combine(NormalizeTarget(Target), "system\\package-manifest.json");
        return File.Exists(manifest) ? Sha256(manifest, 1, delegate { }, delegate { return false; }) : "";
    }

    // True when the target already holds exactly this package (same package-manifest.json).
    public bool IsInstalled() { return ManifestSha256.Length > 0 && InstalledManifestSha() == ManifestSha256; }

    // True when the target holds the previous release this EXE carries a small patch for.
    public bool CanUpgrade() { return UpgradeFrom.Length > 0 && PatchBytes > 0 && InstalledManifestSha() == UpgradeFrom; }

    // Replaces only the files that changed since the previous release (carried inside this EXE),
    // keeping all transcripts, records and settings, then refreshes the desktop items.
    public string Upgrade()
    {
        string target = NormalizeTarget(Target);
        OpenLog(LogPath ?? Path.Combine(target, "system\\logs\\install.log"));
        try
        {
            Say("更新已安裝的版本（只替換有變更的檔案，文字稿與紀錄都保留）：" + target);
            byte[] patch = new byte[PatchBytes];
            using (FileStream self = new FileStream(Assembly.GetExecutingAssembly().Location, FileMode.Open, FileAccess.Read, FileShare.Read))
            {
                self.Position = metadataStart - PatchBytes;
                int done = 0;
                while (done < patch.Length) { int read = self.Read(patch, done, patch.Length - done); if (read <= 0) break; done += read; }
            }
            using (System.Security.Cryptography.SHA256 hash = System.Security.Cryptography.SHA256.Create())
                if (BitConverter.ToString(hash.ComputeHash(patch)).Replace("-", "").ToLowerInvariant() != PatchSha256)
                    throw new InstallError(2, "安裝程式內的更新資料不完整，請重新下載安裝程式。");
            string root = Path.GetFullPath(target) + "\\";
            string prefix = PackageFolder + "/";
            using (ZipArchive zip = new ZipArchive(new MemoryStream(patch), ZipArchiveMode.Read, false, Encoding.UTF8))
                foreach (ZipArchiveEntry entry in zip.Entries)
                {
                    string name = entry.FullName.Replace('\\', '/');
                    if (!name.StartsWith(prefix) || name.EndsWith("/")) continue;
                    string path = Path.GetFullPath(Path.Combine(root, name.Substring(prefix.Length).Replace('/', '\\')));
                    if (!path.StartsWith(root, StringComparison.OrdinalIgnoreCase)) throw new InstallError(4, "更新資料路徑無效：" + name);
                    Directory.CreateDirectory(Path.GetDirectoryName(path));
                    string partial = path + ".update";
                    using (Stream input = entry.Open()) using (FileStream output = new FileStream(partial, FileMode.Create, FileAccess.Write)) input.CopyTo(output);
                    if (File.Exists(path)) File.Replace(partial, path, null); else File.Move(partial, path);
                    Say("已更新：" + name.Substring(prefix.Length));
                }
            if (InstalledManifestSha() != ManifestSha256) throw new InstallError(4, "更新後版本核對不符，請重新下載安裝程式。");
            CheckDesktopFolders(target);
            EnsureDesktopFolders();
            return CreateShortcut(target, Desktop);
        }
        finally { CloseLog(); }
    }

    // Finds the ZIP without the user arranging files: own folder, Downloads, Desktop.
    public string Locate()
    {
        if (PayloadOverride != null)
        {
            if (!File.Exists(PayloadOverride) || new FileInfo(PayloadOverride).Length != PayloadBytes)
                throw new InstallError(2, "選擇的檔案不是這個安裝程式的安裝資料（大小不符）。請選「" + PayloadName + "」。");
            return PayloadOverride;
        }
        List<string> folders = new List<string>();
        folders.Add(Path.GetDirectoryName(Assembly.GetExecutingAssembly().Location));
        folders.Add(Downloads); folders.Add(Desktop);
        foreach (string folder in folders)
        {
            if (folder == null || !Directory.Exists(folder)) continue;
            string exact = Path.Combine(folder, PayloadName);
            if (File.Exists(exact) && new FileInfo(exact).Length == PayloadBytes) return exact;
        }
        foreach (string folder in folders)
        {
            if (folder == null || !Directory.Exists(folder)) continue;
            try
            {
                foreach (string candidate in Directory.GetFiles(folder, "*.zip"))
                    if (new FileInfo(candidate).Length == PayloadBytes) return candidate;
            }
            catch (Exception) { }
        }
        return null;
    }

    // Downloads the ZIP into Downloads with resume and retry; follows Google Drive's large-file
    // confirmation page. Only used when the EXE was built with a URL.
    public string DownloadPayload()
    {
        if (Url.Length == 0) throw new InstallError(2, "找不到安裝資料「" + PayloadName + "」。");
        ServicePointManager.SecurityProtocol = SecurityProtocolType.Tls12;
        string folder = Downloads; Directory.CreateDirectory(folder);
        string destination = Path.Combine(folder, PayloadName);
        if (File.Exists(destination)) destination = Path.Combine(folder, Path.GetFileNameWithoutExtension(PayloadName) + "_安裝程式下載.zip");
        string partial = destination + ".partial";
        CookieContainer cookies = new CookieContainer();
        string url = Url; byte[] buffer = new byte[1 << 20];
        for (int attempt = 1; ; attempt++)
        {
            if (Cancel) throw new InstallError(1, "已取消安裝。");
            long have = File.Exists(partial) ? new FileInfo(partial).Length : 0;
            if (have > PayloadBytes) { File.Delete(partial); have = 0; }
            if (have == PayloadBytes) break;
            try
            {
                HttpWebRequest request = (HttpWebRequest)WebRequest.Create(url);
                request.CookieContainer = cookies; request.UserAgent = "Mozilla/5.0"; request.AllowAutoRedirect = true;
                request.Timeout = 60000; request.ReadWriteTimeout = 120000;
                if (have > 0) request.AddRange(have);
                using (HttpWebResponse response = (HttpWebResponse)request.GetResponse())
                {
                    if ((response.ContentType ?? "").StartsWith("text/html", StringComparison.OrdinalIgnoreCase))
                    {
                        string page; using (StreamReader reader = new StreamReader(response.GetResponseStream())) page = reader.ReadToEnd();
                        if (Regex.IsMatch(page, "quota|too many users|配額|太多使用者", RegexOptions.IgnoreCase) && !Regex.IsMatch(page, "download-form", RegexOptions.IgnoreCase))
                            throw new InstallError(6, "Google 雲端硬碟暫時限制這個檔案的下載次數（短時間太多人下載）。\n請稍後（通常 24 小時內）再雙擊本程式，或按「開啟下載頁」手動下載「" + PayloadName + "」到「下載」資料夾後再雙擊本程式。");
                        string next = ConfirmUrl(page);
                        if (next == null) throw new InstallError(2, "雲端下載頁面無法辨識（可能沒有開放「知道連結的人」下載），請手動下載「" + PayloadName + "」到「下載」資料夾後再雙擊本程式。");
                        url = next; continue;
                    }
                    bool append = have > 0 && response.StatusCode == HttpStatusCode.PartialContent;
                    if (!append) have = 0;
                    using (Stream input = response.GetResponseStream())
                    using (FileStream output = new FileStream(partial, append ? FileMode.Append : FileMode.Create, FileAccess.Write, FileShare.None, 1 << 20))
                    {
                        int read;
                        while ((read = input.Read(buffer, 0, buffer.Length)) > 0)
                        {
                            if (Cancel) throw new InstallError(1, "已取消安裝（已下載的部分會保留，下次接續）。");
                            output.Write(buffer, 0, read); have += read;
                            if (have > PayloadBytes) throw new IOException("下載內容比預期大。");
                            Progress((int)(have * 100 / PayloadBytes));
                            if ((have & ((64 << 20) - 1)) < read) Say(string.Format("下載安裝資料中：{0:N1} / {1:N1} GB（可中斷，下次接續）", have / 1073741824.0, PayloadBytes / 1073741824.0));
                        }
                    }
                }
            }
            catch (InstallError) { throw; }
            catch (Exception error)
            {
                if (attempt >= 8) throw new InstallError(2, "下載一直失敗：" + error.Message + "\n請確認網路後再雙擊本程式，已下載的部分會接續。");
                Say("下載中斷，" + (attempt * 5) + " 秒後重試（第 " + attempt + " 次）。");
                Thread.Sleep(attempt * 5000);
            }
        }
        if (File.Exists(destination)) File.Delete(destination);
        File.Move(partial, destination);
        return destination;
    }

    static string ConfirmUrl(string page)
    {
        Match form = Regex.Match(page, "<form[^>]*id=\"download-form\"[^>]*action=\"([^\"]+)\"", RegexOptions.IgnoreCase);
        if (!form.Success) form = Regex.Match(page, "<form[^>]*action=\"([^\"]+)\"", RegexOptions.IgnoreCase);
        if (!form.Success) return null;
        List<string> query = new List<string>();
        foreach (Match input in Regex.Matches(page, "<input[^>]*type=\"hidden\"[^>]*>", RegexOptions.IgnoreCase))
        {
            Match name = Regex.Match(input.Value, "name=\"([^\"]*)\""), value = Regex.Match(input.Value, "value=\"([^\"]*)\"");
            if (name.Success) query.Add(Uri.EscapeDataString(name.Groups[1].Value) + "=" + Uri.EscapeDataString(value.Success ? WebUtility.HtmlDecode(value.Groups[1].Value) : ""));
        }
        string action = WebUtility.HtmlDecode(form.Groups[1].Value);
        return query.Count == 0 ? action : action + (action.Contains("?") ? "&" : "?") + string.Join("&", query.ToArray());
    }

    string InputDir { get { return Path.Combine(Desktop, "音檔"); } }
    string OutputDir { get { return Path.Combine(Desktop, "文字檔"); } }

    void CheckDesktopFolders(string target)
    {
        foreach (string folder in new string[] { InputDir, OutputDir })
        {
            if (File.Exists(folder)) throw new InstallError(3, "桌面已有名為「" + Path.GetFileName(folder) + "」的檔案（不是資料夾），請先改名後再雙擊本程式。");
            string full = Path.GetFullPath(folder).TrimEnd('\\') + "\\";
            if ((target + "\\").StartsWith(full, StringComparison.OrdinalIgnoreCase)) throw new InstallError(3, "不能安裝在桌面的「音檔」或「文字檔」資料夾裡，請選其他位置。");
        }
    }

    List<string> EnsureDesktopFolders()
    {
        List<string> created = new List<string>();
        foreach (string folder in new string[] { InputDir, OutputDir })
        {
            if (!Directory.Exists(folder)) { Directory.CreateDirectory(folder); created.Add(folder); Say("已在桌面建立資料夾：" + folder); }
            else Say("沿用桌面既有資料夾（不會更動裡面的檔案）：" + folder);
        }
        return created;
    }

    // Already installed: only restore missing desktop folders and shortcut, never touch the package.
    public string Repair()
    {
        string target = NormalizeTarget(Target);
        OpenLog(LogPath ?? Path.Combine(target, "system\\logs\\install.log"));
        try
        {
            Say("已安裝這個版本：" + target);
            CheckDesktopFolders(target);
            EnsureDesktopFolders();
            return CreateShortcut(target, Desktop);
        }
        finally { CloseLog(); }
    }

    public string Run(string payload)
    {
        string target = NormalizeTarget(Target);
        bool created = false;
        List<string> createdFolders = new List<string>();
        try
        {
            OpenLog(LogPath);
            Say("安裝位置：" + target);
            if (File.Exists(target)) throw new InstallError(3, "安裝位置是一個檔案，請選其他資料夾。");
            if (Directory.Exists(target) && Directory.GetFileSystemEntries(target).Length > 0)
                throw new InstallError(3, "「" + target + "」已有其他檔案或舊版本。請把它改名或移走（裡面的文字稿請先備份）後再雙擊本程式，或在下方換一個空的位置。");
            DriveInfo drive = new DriveInfo(Path.GetPathRoot(target));
            if (!drive.IsReady) throw new InstallError(3, "找不到安裝位置的磁碟。");
            if (string.Equals(drive.DriveFormat, "FAT32", StringComparison.OrdinalIgnoreCase))
                throw new InstallError(3, "這個磁碟是 FAT32，無法存放大型模型。請改裝到 C 槽或 NTFS / exFAT 磁碟。");
            CheckDesktopFolders(target);
            if (!File.Exists(payload) || new FileInfo(payload).Length != PayloadBytes)
                throw new InstallError(2, "安裝資料「" + Path.GetFileName(payload) + "」大小不符（可能還沒下載完或已損壞），請重新下載。");
            Say("核對安裝資料是否完整（約 1–3 分鐘）。");
            if (Sha256(payload, PayloadBytes, delegate(int p) { Progress(p * 15 / 100); }, delegate { return Cancel; }) != PayloadSha256)
                throw new InstallError(2, "安裝資料內容不符（下載不完整或已損壞），請重新下載「" + PayloadName + "」。");
            using (FileStream file = new FileStream(payload, FileMode.Open, FileAccess.Read, FileShare.Read, 1 << 20))
            using (ZipArchive zip = new ZipArchive(file, ZipArchiveMode.Read, false, Encoding.UTF8))
            {
                long need = 0;
                foreach (ZipArchiveEntry entry in zip.Entries) need += entry.Length;
                long reserve = 2L * 1024 * 1024 * 1024;
                if (drive.AvailableFreeSpace < need + reserve)
                    throw new InstallError(3, string.Format("磁碟空間不足：需要約 {0:N1} GB，目前剩 {1:N1} GB。", (need + reserve) / 1073741824.0, drive.AvailableFreeSpace / 1073741824.0));
                created = !Directory.Exists(target);
                Directory.CreateDirectory(target);
                string root = Path.GetFullPath(target) + "\\";
                string prefix = PackageFolder + "/";
                long done = 0; byte[] buffer = new byte[1 << 20];
                Say("正在解壓（約 8 GB，視電腦速度需數分鐘）。");
                foreach (ZipArchiveEntry entry in zip.Entries)
                {
                    if (Cancel) throw new InstallError(1, "已取消安裝。");
                    string name = entry.FullName.Replace('\\', '/');
                    if (!name.StartsWith(prefix)) throw new InstallError(4, "安裝資料內容不符：" + name);
                    name = name.Substring(prefix.Length);
                    if (name.Length == 0 || name == "音檔/" || name == "文字檔/") continue;
                    string path = Path.GetFullPath(Path.Combine(root, name.Replace('/', '\\')));
                    if (!path.StartsWith(root, StringComparison.OrdinalIgnoreCase)) throw new InstallError(4, "安裝資料路徑無效：" + name);
                    if (name.EndsWith("/")) { Directory.CreateDirectory(path); continue; }
                    Directory.CreateDirectory(Path.GetDirectoryName(path));
                    using (Stream input = entry.Open())
                    using (FileStream output = new FileStream(path, FileMode.CreateNew, FileAccess.Write, FileShare.None, 1 << 20))
                    {
                        int read;
                        while ((read = input.Read(buffer, 0, buffer.Length)) > 0)
                        {
                            if (Cancel) throw new InstallError(1, "已取消安裝。");
                            output.Write(buffer, 0, read);
                            done += read;
                            Progress(15 + (int)(done * 70 / Math.Max(need, 1)));
                        }
                    }
                    File.SetLastWriteTime(path, entry.LastWriteTime.DateTime);
                }
            }
            OpenLog(Path.Combine(target, "system\\logs\\install.log"));
            createdFolders = EnsureDesktopFolders();
            File.WriteAllText(Path.Combine(target, "system\\user_folders.json"),
                "{\"input_dir\": " + Json(InputDir) + ", \"output_dir\": " + Json(OutputDir) + "}\n", new UTF8Encoding(false));
            Say("解壓完成，正在準備包內環境（離線核對所有檔案，不需下載）。");
            Prepare(target);
            Progress(97);
            string shortcut = CreateShortcut(target, Desktop);
            Say("已建立桌面捷徑：" + shortcut);
            Progress(100);
            Say("安裝完成。");
            return shortcut;
        }
        catch (Exception)
        {
            if (created && Directory.Exists(target))
            {
                CloseLog();
                try { Directory.Delete(target, true); } catch (Exception) { }
            }
            foreach (string folder in createdFolders)
                try { if (Directory.Exists(folder) && Directory.GetFileSystemEntries(folder).Length == 0) Directory.Delete(folder); } catch (Exception) { }
            throw;
        }
        finally { CloseLog(); }
    }

    void Prepare(string target)
    {
        string bat = Path.Combine(target, "開始轉錄.bat");
        if (!File.Exists(bat)) throw new InstallError(4, "解壓內容缺少 開始轉錄.bat。");
        string windows = Environment.GetEnvironmentVariable("SystemRoot") ?? @"C:\Windows";
        ProcessStartInfo info = new ProcessStartInfo(Path.Combine(windows, "System32\\cmd.exe"), "/d /c \"\"" + bat + "\" --prepare-only\"");
        info.WorkingDirectory = target;
        info.UseShellExecute = false; info.CreateNoWindow = true;
        info.RedirectStandardOutput = true; info.RedirectStandardError = true;
        info.StandardOutputEncoding = Encoding.UTF8; info.StandardErrorEncoding = Encoding.UTF8;
        foreach (string key in new string[] { "PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "OLLAMA_HOST", "OLLAMA_MODELS", "TCL_LIBRARY", "TK_LIBRARY" })
            info.EnvironmentVariables.Remove(key);
        info.EnvironmentVariables["PATH"] = Path.Combine(windows, "System32") + ";" + windows;
        info.EnvironmentVariables["BROADCAST_TRANSCRIBE_NO_PAUSE"] = "1";
        using (Process process = new Process())
        {
            process.StartInfo = info;
            List<string> tail = new List<string>();
            DataReceivedEventHandler handler = delegate(object sender, DataReceivedEventArgs e)
            {
                if (string.IsNullOrEmpty(e.Data)) return;
                lock (tail) { tail.Add(e.Data); if (tail.Count > 20) tail.RemoveAt(0); }
                Say(e.Data.Trim());
            };
            process.OutputDataReceived += handler; process.ErrorDataReceived += handler;
            process.Start(); process.BeginOutputReadLine(); process.BeginErrorReadLine();
            if (!process.WaitForExit(60 * 60 * 1000)) { try { process.Kill(); } catch (Exception) { } throw new InstallError(5, "環境準備超過 60 分鐘未完成。"); }
            process.WaitForExit();
            if (process.ExitCode != 0)
            {
                string last; lock (tail) last = string.Join("\n", tail.ToArray());
                throw new InstallError(5, "環境準備失敗：\n" + last);
            }
        }
    }

    static object Get(object target, string name) { return target.GetType().InvokeMember(name, BindingFlags.GetProperty, null, target, null); }
    static void Set(object target, string name, object value) { target.GetType().InvokeMember(name, BindingFlags.SetProperty, null, target, new object[] { value }); }

    string CreateShortcut(string target, string desktop)
    {
        Directory.CreateDirectory(desktop);
        string bat = Path.Combine(target, "開始轉錄.bat");
        Type type = Type.GetTypeFromProgID("WScript.Shell");
        object shell = Activator.CreateInstance(type);
        string path = Path.Combine(desktop, ShortcutName + ".lnk");
        if (File.Exists(path))
        {
            object existing = type.InvokeMember("CreateShortcut", BindingFlags.InvokeMethod, null, shell, new object[] { path });
            string old = (Get(existing, "TargetPath") as string) ?? "";
            if (old.IndexOf(PackageFolder, StringComparison.OrdinalIgnoreCase) < 0)
                path = Path.Combine(desktop, ShortcutName + "（影音轉文字）.lnk");
        }
        object link = type.InvokeMember("CreateShortcut", BindingFlags.InvokeMethod, null, shell, new object[] { path });
        Set(link, "TargetPath", bat);
        Set(link, "WorkingDirectory", target);
        Set(link, "WindowStyle", 1);
        Set(link, "Description", "影音轉文字朋友版：開啟轉錄視窗");
        Set(link, "IconLocation", Path.Combine(target, "system\\runtime\\python\\pythonw.exe") + ",0");
        link.GetType().InvokeMember("Save", BindingFlags.InvokeMethod, null, link, null);
        return path;
    }

    // Shared by the window and --silent: open if installed, else find or download, then install.
    public string InstallOrOpen()
    {
        if (IsInstalled()) return Repair();
        if (CanUpgrade()) return Upgrade();
        string payload = Locate();
        if (payload == null) payload = DownloadPayload();
        return Run(payload);
    }
}

sealed class InstallerForm : Form
{
    readonly Installer installer;
    readonly bool launch;
    readonly Label status = new Label(), where = new Label();
    readonly TextBox targetBox = new TextBox();
    readonly ProgressBar bar = new ProgressBar();
    readonly Button primary = new Button(), pick = new Button(), close = new Button(), page = new Button();
    Thread worker;

    public InstallerForm(Installer installer, bool launch)
    {
        this.installer = installer; this.launch = launch;
        Text = "安裝影音轉文字朋友版";
        Font = new Font("Microsoft JhengHei UI", 10F);
        ClientSize = new Size(640, 330); FormBorderStyle = FormBorderStyle.FixedDialog; MaximizeBox = false; StartPosition = FormStartPosition.CenterScreen;
        Label intro = new Label();
        intro.Text = "正在自動安裝，不需要任何操作；第一次約 10–30 分鐘，完成後會自動開啟。\n安裝後桌面會有「音檔」「文字檔」和「開始轉錄」：影片放進「音檔」→ 雙擊「開始轉錄」→ 結果在「文字檔」。";
        intro.SetBounds(20, 14, 600, 64);
        bar.SetBounds(20, 86, 600, 22);
        status.Text = "準備中…"; status.SetBounds(20, 116, 600, 110);
        where.Text = "安裝位置："; where.SetBounds(20, 232, 90, 26);
        targetBox.Text = installer.Target; targetBox.SetBounds(110, 230, 510, 26);
        primary.SetBounds(270, 280, 110, 34); primary.Click += delegate { Begin(); };
        pick.Text = "選擇安裝資料…"; pick.SetBounds(390, 280, 130, 34); pick.Click += delegate { Pick(); };
        close.Text = "關閉"; close.SetBounds(530, 280, 90, 34); close.Click += delegate { Close(); };
        page.Text = "開啟下載頁"; page.SetBounds(20, 280, 120, 34); page.Visible = false;
        page.Click += delegate { try { Process.Start(installer.Page.Length > 0 ? installer.Page : installer.Url); } catch (Exception) { } };
        Controls.AddRange(new Control[] { intro, bar, status, where, targetBox, primary, pick, close, page });
        Shown += delegate { Begin(); };
    }

    void Ui(Action action) { if (IsHandleCreated && !IsDisposed) BeginInvoke(action); }

    void Idle(bool showTarget, bool showPick, string button)
    {
        where.Visible = targetBox.Visible = showTarget; targetBox.Enabled = true;
        pick.Visible = showPick; pick.Enabled = true;
        primary.Visible = button != null; primary.Enabled = true; primary.Text = button ?? "";
        page.Visible = button != null && installer.Url.Length > 0;
    }

    void Begin()
    {
        if (worker != null && worker.IsAlive) return;
        Idle(false, false, null); bar.Value = 0;
        installer.Target = targetBox.Text; installer.Cancel = false;
        installer.Status = delegate(string message) { Ui(delegate { status.Text = message; }); };
        installer.Progress = delegate(int value) { Ui(delegate { bar.Value = Math.Max(0, Math.Min(100, value)); }); };
        worker = new Thread(delegate()
        {
            try
            {
                string shortcut;
                if (installer.IsInstalled()) shortcut = installer.Repair();
                else if (installer.CanUpgrade()) shortcut = installer.Upgrade();
                else
                {
                    string payload = installer.Locate();
                    if (payload == null && installer.Url.Length == 0) { Ui(Missing); return; }
                    if (payload == null) payload = installer.DownloadPayload();
                    shortcut = installer.Run(payload);
                }
                Ui(delegate { Done(shortcut); });
            }
            catch (Exception error) { Ui(delegate { Failed(error.Message); }); }
        });
        worker.IsBackground = true; worker.Start();
    }

    void Missing()
    {
        status.Text = "找不到安裝資料「" + installer.PayloadName + "」（約 7.6 GB）。\n請把它下載到「下載」資料夾（或和本程式放在同一個資料夾），下載完成後按「重新尋找」；\n或按「選擇安裝資料…」直接指定檔案。";
        Idle(false, true, "重新尋找");
    }

    void Failed(string message)
    {
        status.Text = "安裝未完成：" + message;
        bar.Value = 0;
        Idle(true, true, "重試");
    }

    void Pick()
    {
        using (OpenFileDialog dialog = new OpenFileDialog())
        {
            dialog.Title = "選擇「" + installer.PayloadName + "」";
            dialog.Filter = "安裝資料 (*.zip)|*.zip";
            dialog.InitialDirectory = installer.Downloads;
            if (dialog.ShowDialog(this) == DialogResult.OK) { installer.PayloadOverride = dialog.FileName; Begin(); }
        }
    }

    void Done(string shortcut)
    {
        status.Text = "完成！正在開啟轉錄視窗…\n之後請雙擊桌面「開始轉錄」使用；這個安裝程式和安裝資料可以刪除。";
        bar.Value = 100;
        if (launch) { try { Process.Start(shortcut); } catch (Exception) { } }
        System.Windows.Forms.Timer timer = new System.Windows.Forms.Timer();
        timer.Interval = 4000; timer.Tick += delegate { timer.Stop(); Close(); }; timer.Start();
    }

    protected override void OnFormClosing(FormClosingEventArgs e)
    {
        if (worker != null && worker.IsAlive)
        {
            if (MessageBox.Show(this, "安裝還在進行，確定要取消嗎？", Text, MessageBoxButtons.YesNo) != DialogResult.Yes) { e.Cancel = true; return; }
            installer.Cancel = true;
            status.Text = "正在取消並清除未完成的檔案…";
            worker.Join(TimeSpan.FromMinutes(2));
        }
        base.OnFormClosing(e);
    }
}

static class Program
{
    [STAThread]
    static int Main(string[] args)
    {
        bool silent = false, launch = true; string probe = null;
        Installer installer;
        try { installer = new Installer(); }
        catch (InstallError error) { MessageBox.Show(error.Message, "安裝影音轉文字朋友版"); return error.Code; }
        for (int i = 0; i < args.Length; i++)
        {
            string arg = args[i];
            if (arg == "--silent") silent = true;
            else if (arg == "--no-launch") launch = false;
            else if (arg == "--target" && i + 1 < args.Length) installer.Target = args[++i];
            else if (arg == "--desktop-dir" && i + 1 < args.Length) installer.DesktopDir = args[++i];
            else if (arg == "--downloads-dir" && i + 1 < args.Length) installer.DownloadsDir = args[++i];
            else if (arg == "--log" && i + 1 < args.Length) installer.LogPath = args[++i];
            else if (arg == "--probe" && i + 1 < args.Length) probe = args[++i];
        }
        if (probe != null)
        {
            try
            {
                string payload = installer.Locate();
                string text = payload == null ? "payload=none" : "payload=" + payload + " payload_bytes=" + new FileInfo(payload).Length;
                if (payload != null)
                    using (FileStream file = new FileStream(payload, FileMode.Open, FileAccess.Read, FileShare.Read))
                    using (ZipArchive zip = new ZipArchive(file, ZipArchiveMode.Read, false, Encoding.UTF8)) text += " entries=" + zip.Entries.Count;
                File.WriteAllText(probe, text + " installed=" + installer.IsInstalled(), new UTF8Encoding(false));
                return 0;
            }
            catch (Exception error) { File.WriteAllText(probe, "error=" + error.Message, new UTF8Encoding(false)); return 2; }
        }
        if (silent)
        {
            try { installer.InstallOrOpen(); return 0; }
            catch (InstallError error) { if (installer.LogPath != null) File.AppendAllText(installer.LogPath, "FAILED " + error.Message + "\n", new UTF8Encoding(false)); return error.Code; }
            catch (Exception error) { if (installer.LogPath != null) File.AppendAllText(installer.LogPath, "FAILED " + error + "\n", new UTF8Encoding(false)); return 9; }
        }
        Application.EnableVisualStyles();
        Application.Run(new InstallerForm(installer, launch));
        return 0;
    }
}
