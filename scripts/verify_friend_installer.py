"""Acceptance for the one-double-click friend installer: real install into a fresh case, real
ASR/proofread, desktop folders and shortcut, reopen of an installed version, ZIP discovery,
download with confirm page/resume/quota page, and refusal paths. Never seeds models or tools."""
from pathlib import Path
import argparse,ctypes as c,http.server,json,os,shutil,struct,subprocess,sys,threading,time,urllib.parse,zipfile
from ctypes import wintypes as w

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
from build_friend_installer import INSTALLER_NAME,drive_urls,payload_metadata,read_trailer,sha,write_installer

k=c.WinDLL('kernel32',use_last_error=True);u=c.WinDLL('user32',use_last_error=True);g=c.WinDLL('gdi32')
class Entry(c.Structure):
    _fields_=[('size',w.DWORD),('usage',w.DWORD),('pid',w.DWORD),('heap',c.c_size_t),('module',w.DWORD),
              ('threads',w.DWORD),('parent',w.DWORD),('priority',w.LONG),('flags',w.DWORD),('exe',w.WCHAR*260)]
k.CreateToolhelp32Snapshot.argtypes=[w.DWORD,w.DWORD];k.CreateToolhelp32Snapshot.restype=w.HANDLE
k.Process32FirstW.argtypes=[w.HANDLE,c.POINTER(Entry)];k.Process32NextW.argtypes=[w.HANDLE,c.POINTER(Entry)]
k.OpenProcess.argtypes=[w.DWORD,w.BOOL,w.DWORD];k.OpenProcess.restype=w.HANDLE
k.QueryFullProcessImageNameW.argtypes=[w.HANDLE,w.DWORD,w.LPWSTR,c.POINTER(w.DWORD)];k.CloseHandle.argtypes=[w.HANDLE]
callback=c.WINFUNCTYPE(w.BOOL,w.HWND,w.LPARAM)
u.EnumWindows.argtypes=[callback,w.LPARAM];u.GetWindowThreadProcessId.argtypes=[w.HWND,c.POINTER(w.DWORD)]
u.GetWindowTextW.argtypes=[w.HWND,w.LPWSTR,c.c_int];u.PostMessageW.argtypes=[w.HWND,w.UINT,w.WPARAM,w.LPARAM]
u.IsWindowVisible.argtypes=[w.HWND]

def processes(folder):
    found={};handle=k.CreateToolhelp32Snapshot(2,0);entry=Entry();entry.size=c.sizeof(entry);prefix=str(folder).casefold()+'\\'
    try:
        valid=k.Process32FirstW(handle,c.byref(entry))
        while valid:
            process=k.OpenProcess(0x1000,False,entry.pid)
            if process:
                buf=c.create_unicode_buffer(32768);length=w.DWORD(len(buf))
                if k.QueryFullProcessImageNameW(process,0,buf,c.byref(length)) and buf.value.casefold().startswith(prefix):found[entry.pid]=buf.value
                k.CloseHandle(process)
            valid=k.Process32NextW(handle,c.byref(entry))
    finally:k.CloseHandle(handle)
    return found
def windows(folder,title):
    found=[];pids=processes(folder)
    @callback
    def visit(hwnd,_):
        pid=w.DWORD();u.GetWindowThreadProcessId(hwnd,c.byref(pid))
        if pid.value in pids and u.IsWindowVisible(hwnd):
            text=c.create_unicode_buffer(512);u.GetWindowTextW(hwnd,text,len(text))
            if title in text.value:found.append((hwnd,pid.value,text.value))
        return True
    u.EnumWindows(visit,0);return found
def screenshot(hwnd,target):
    u.GetDC.argtypes=[w.HWND];u.GetDC.restype=w.HDC;u.ReleaseDC.argtypes=[w.HWND,w.HDC]
    u.GetClientRect.argtypes=[w.HWND,c.POINTER(w.RECT)];u.PrintWindow.argtypes=[w.HWND,w.HDC,w.UINT]
    g.CreateCompatibleDC.argtypes=[w.HDC];g.CreateCompatibleDC.restype=w.HDC
    g.CreateCompatibleBitmap.argtypes=[w.HDC,c.c_int,c.c_int];g.CreateCompatibleBitmap.restype=w.HBITMAP
    g.SelectObject.argtypes=[w.HDC,w.HGDIOBJ];g.SelectObject.restype=w.HGDIOBJ
    g.GetDIBits.argtypes=[w.HDC,w.HBITMAP,w.UINT,w.UINT,c.c_void_p,c.c_void_p,w.UINT]
    g.DeleteObject.argtypes=[w.HGDIOBJ];g.DeleteDC.argtypes=[w.HDC]
    rect=w.RECT();assert u.GetClientRect(hwnd,c.byref(rect));width,height=rect.right,rect.bottom
    dc=u.GetDC(hwnd);mem=g.CreateCompatibleDC(dc);bmp=g.CreateCompatibleBitmap(dc,width,height);old=g.SelectObject(mem,bmp)
    try:
        assert u.PrintWindow(hwnd,mem,3);g.SelectObject(mem,old)
        info=struct.pack('<IiiHHIIiiII',40,width,height,1,32,0,width*height*4,0,0,0,0)
        pixels=c.create_string_buffer(width*height*4);header=c.create_string_buffer(info)
        assert g.GetDIBits(mem,bmp,0,height,pixels,header,0)
        Path(target).write_bytes(struct.pack('<2sIHHI',b'BM',54+len(pixels),0,0,54)+info+pixels.raw)
    finally:g.DeleteObject(bmp);g.DeleteDC(mem);u.ReleaseDC(hwnd,dc)
def kill(folder):
    for pid in processes(folder):
        subprocess.run(['taskkill','/PID',str(pid),'/T','/F'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,creationflags=subprocess.CREATE_NO_WINDOW)

def sanitized():
    env=dict(os.environ,PATH=os.environ['SystemRoot']+'\\System32;'+os.environ['SystemRoot'],BROADCAST_TRANSCRIBE_NO_PAUSE='1')
    for key in ('PYTHONPATH','PYTHONHOME','PYTHONSTARTUP','OLLAMA_HOST','OLLAMA_MODELS','TCL_LIBRARY','TK_LIBRARY'):env.pop(key,None)
    return env
def run(args,timeout):
    return subprocess.run([str(a) for a in args],env=sanitized(),stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=timeout)
def isolated(case,name,exe):
    """A folder holding only the installer, plus empty Downloads/Desktop, so nothing real is found."""
    root=case/name;(root/'exe').mkdir(parents=True);(root/'Downloads').mkdir();(root/'Desktop').mkdir()
    shutil.copy2(exe,root/'exe'/exe.name);return root,root/'exe'/exe.name
def silent(exe,root,target,log,timeout=3600):
    return run([exe,'--silent','--target',target,'--desktop-dir',root/'Desktop','--downloads-dir',root/'Downloads','--log',log],timeout).returncode

class Server(http.server.ThreadingHTTPServer):
    daemon_threads=True
def serve(payload,mode):
    """Local stand-in for Google Drive: confirm page, then the ZIP with Range; drops the first
    full transfer after 64 MB so the installer must resume. mode='quota' serves a quota page."""
    log=[]
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self,*_):pass
        def html(self,text):
            data=text.encode('utf-8');self.send_response(200);self.send_header('Content-Type','text/html; charset=utf-8')
            self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
        def do_GET(self):
            url=urllib.parse.urlparse(self.path);query=dict(urllib.parse.parse_qsl(url.query));log.append((url.path,query,self.headers.get('Range')))
            if mode=='quota':return self.html('<html><title>Google Drive - Quota exceeded</title>Too many users have viewed or downloaded this file recently.</html>')
            if url.path=='/share':
                return self.html('<form id="download-form" action="http://127.0.0.1:%d/download" method="get">'
                    '<input type="hidden" name="id" value="X"><input type="hidden" name="confirm" value="t"><input type="hidden" name="uuid" value="u&amp;1"></form>'%self.server.server_port)
            if url.path!='/download' or query.get('uuid')!='u&1':self.send_error(404);return
            size=payload.stat().st_size;start=0;header=self.headers.get('Range')
            if header:start=int(header.split('=')[1].split('-')[0]);self.send_response(206);self.send_header('Content-Range',f'bytes {start}-{size-1}/{size}')
            else:self.send_response(200)
            self.send_header('Content-Type','application/zip');self.send_header('Content-Length',str(size-start));self.end_headers()
            limit=64*1024**2 if not header and len([x for x in log if x[0]=='/download'])==1 else None
            with payload.open('rb') as source:
                source.seek(start);sent=0
                while block:=source.read(1024**2):
                    try:self.wfile.write(block)
                    except OSError:return
                    sent+=len(block)
                    if limit and sent>=limit:self.close_connection=True;self.connection.shutdown(2);return
    server=Server(('127.0.0.1',0),Handler);threading.Thread(target=server.serve_forever,daemon=True).start()
    return server,log

def show_command(link):
    """ShowCommand of a .lnk (ShellLinkHeader offset 0x3C): 1 normal, 3 maximized, 7 minimized."""
    return struct.unpack_from('<I',Path(link).read_bytes(),0x3C)[0]

def launch_gui(exe,args,title):
    """Start the installer window; return (process, window) once visible."""
    process=subprocess.Popen([str(exe),*map(str,args)],env=sanitized());deadline=time.monotonic()+60;owned=[]
    while time.monotonic()<deadline and not owned and process.poll() is None:owned=windows(exe.parent,title);time.sleep(.2)
    return process,owned

def main():
    parser=argparse.ArgumentParser();parser.add_argument('folder',type=Path,help='安裝包 folder from build_friend_installer')
    parser.add_argument('--real-link',help='also install for real by downloading from this Google Drive share link')
    parser.add_argument('--previous',type=Path,help='previous release 安裝包 folder; its install must be upgraded in place')
    args=parser.parse_args()
    folder=args.folder.resolve();receipt=json.loads((folder.parent/'installer_build.json').read_text(encoding='utf-8'))
    report={'installer_folder':str(folder)}
    case=ROOT/'work/acceptance'/time.strftime('friend_installer_%Y%m%d_%H%M%S');case.mkdir(parents=True)
    installer=Path(receipt['installer']);payload=Path(receipt['payload'])
    print('核對安裝程式與安裝資料指紋。',flush=True)
    assert sha(installer)==receipt['installer_sha256'];assert sha(payload)==receipt['payload_sha256']
    full=read_trailer(installer);meta={k:v for k,v in full.items() if k not in ('upgrade_from','patch_bytes','patch_sha256')}
    assert meta==payload_metadata(payload,receipt['payload_sha256'],receipt['url'],receipt.get('page',''))
    with zipfile.ZipFile(payload) as archive:assert archive.testzip() is None;entries=len(archive.infolist())
    report.update(installer_sha256=receipt['installer_sha256'],installer_bytes=installer.stat().st_size,payload_sha256=receipt['payload_sha256'],
        payload_bytes=payload.stat().st_size,payload_testzip=True,payload_entries=entries,url_embedded=bool(receipt['url']))

    print('自動找安裝資料：exe 同資料夾、下載資料夾。',flush=True)
    side,exe=isolated(case,'find_beside',installer);os.link(payload,exe.parent/payload.name)
    assert run([exe,'--probe',case/'probe_beside.txt','--desktop-dir',side/'Desktop','--downloads-dir',side/'Downloads'],60).returncode==0
    assert f'entries={entries}' in (case/'probe_beside.txt').read_text(encoding='utf-8')
    main_root,main_exe=isolated(case,'main',installer);renamed=main_root/'Downloads'/'影音轉文字朋友版 (1).zip';os.link(payload,renamed)
    assert run([main_exe,'--probe',case/'probe_downloads.txt','--desktop-dir',main_root/'Desktop','--downloads-dir',main_root/'Downloads'],60).returncode==0
    assert str(renamed) in (case/'probe_downloads.txt').read_text(encoding='utf-8')
    report['finds_payload_beside_exe_and_in_downloads_even_if_renamed']=True

    print('拒絕路徑：找不到資料、大小不符、內容不符、目標非空、桌面同名檔案。',flush=True)
    offline_exe=case/'offline_installer.exe';write_installer(offline_exe,{**meta,'url':'','page':''})
    root,exe=isolated(case,'refusal_missing',offline_exe)
    assert silent(exe,root,root/'target',case/'refusal_missing.log',60)==2 and not (root/'target').exists()
    root,exe=isolated(case,'refusal_size',offline_exe);(root/'Downloads'/payload.name).write_bytes(b'PK\x05\x06'+b'\0'*18)
    assert silent(exe,root,root/'target',case/'refusal_size.log',60)==2 and not (root/'target').exists()
    root,exe=isolated(case,'refusal_content',offline_exe)
    with (root/'Downloads'/payload.name).open('wb') as fake:fake.truncate(payload.stat().st_size)
    assert silent(exe,root,root/'target',case/'refusal_content.log',600)==2 and not (root/'target').exists()
    (root/'Downloads'/payload.name).unlink()
    root,exe=isolated(case,'refusal_occupied',installer);os.link(payload,root/'Downloads'/payload.name)
    (root/'target').mkdir();(root/'target'/'keep.txt').write_text('owner file',encoding='utf-8')
    assert silent(exe,root,root/'target',case/'refusal_occupied.log',600)==3
    assert (root/'target'/'keep.txt').read_text(encoding='utf-8')=='owner file' and len(list((root/'target').iterdir()))==1
    (root/'Desktop'/'音檔').write_text('not a folder',encoding='utf-8')
    assert silent(exe,root,root/'target2',case/'refusal_desktop.log',600)==3 and not (root/'target2').exists() and not (root/'Desktop'/'文字檔').exists()
    report['refusals_missing_size_content_occupied_desktop_file']=True

    print('下載模式（本機模擬雲端）：確認頁、續傳、下載額度頁。',flush=True)
    server,log=serve(payload,'normal')
    try:
        download_exe=case/'download_installer.exe'
        write_installer(download_exe,{**meta,'url':f'http://127.0.0.1:{server.server_port}/share','page':''})
        root,exe=isolated(case,'download',download_exe);(root/'target').mkdir();(root/'target'/'keep.txt').write_text('x',encoding='utf-8')
        assert silent(exe,root,root/'target',case/'download.log',1800)==3
        fetched=root/'Downloads'/payload.name;assert fetched.is_file() and sha(fetched)==receipt['payload_sha256']
        downloads=[entry for entry in log if entry[0]=='/download']
        assert log[0][0]=='/share' and len(downloads)>=2 and downloads[0][2] is None
        assert downloads[1][2] and downloads[1][2].startswith('bytes=') and int(downloads[1][2][6:].rstrip('-'))>=64*1024**2
        fetched.unlink()
    finally:server.shutdown()
    server,_=serve(payload,'quota')
    try:
        quota_exe=case/'quota_installer.exe'
        write_installer(quota_exe,{**meta,'url':f'http://127.0.0.1:{server.server_port}/share','page':''})
        root,exe=isolated(case,'quota',quota_exe)
        assert silent(exe,root,root/'target',case/'quota.log',120)==6 and not (root/'target').exists()
    finally:server.shutdown()
    report.update(download_follows_confirm_page_and_resumes=True,download_sha_matches=True,quota_page_reported=True)

    print('實際安裝：資料在「下載」資料夾且已改名，桌面已有「音檔」。',flush=True)
    package=main_root/'影音轉文字朋友版';desktop=main_root/'Desktop'
    existing=desktop/'音檔';existing.mkdir();(existing/'keep_me.txt').write_text('friend file',encoding='utf-8')
    started=time.monotonic()
    assert silent(main_exe,main_root,package,case/'install.log')==0,(case/'install.log').read_text(encoding='utf-8',errors='replace')[-3000:]
    report['install_seconds']=round(time.monotonic()-started)
    shortcut=desktop/'開始轉錄.lnk';assert shortcut.is_file() and (package/'system/config.local.json').is_file()
    folders=json.loads((package/'system/user_folders.json').read_text(encoding='utf-8'))
    assert Path(folders['input_dir'])==desktop/'音檔' and Path(folders['output_dir'])==desktop/'文字檔'
    assert (desktop/'文字檔').is_dir() and (existing/'keep_me.txt').read_text(encoding='utf-8')=='friend file'
    assert not (package/'音檔').exists() and not (package/'文字檔').exists()
    report.update(silent_install_passed=True,desktop_folders_created_or_reused=True,existing_desktop_folder_content_kept=True,desktop_shortcut_created=True)

    print('解壓後真Whisper兩段辨識與Qwen3 8B校稿。',flush=True)
    verify=subprocess.run([sys.executable,'-X','utf8',str(ROOT/'scripts/verify_friend_package.py'),str(payload),'--resume-case',str(main_root)],timeout=3600)
    assert verify.returncode==0;package_report=json.loads((main_root/'report.json').read_text(encoding='utf-8'))
    assert Path(package_report['input_dir'])==desktop/'音檔' and Path(package_report['output_dir'])==desktop/'文字檔'
    assert (desktop/'文字檔/朋友測試.txt').is_file()
    for key in ('offline_prepare_passed','actual_two_chunk_asr','actual_qwen3_8b_cleanup','timeline_equals_srt','completed_skip_preserves_checkpoints'):
        assert package_report[key] is True;report[key]=True
    report['asr_reads_desktop_input_writes_desktop_output']=True

    print('再次雙擊同版本：不重裝，只補回捷徑。',flush=True)
    marker=package/'system/package-manifest.json';before=marker.stat().st_mtime_ns;count=sum(1 for _ in package.rglob('*'))
    shortcut.unlink();renamed.unlink()
    assert silent(main_exe,main_root,package,case/'reopen.log',120)==0
    assert shortcut.is_file() and marker.stat().st_mtime_ns==before and sum(1 for _ in package.rglob('*'))==count
    report['reopen_same_version_restores_shortcut_without_reinstall']=True

    print('雙擊桌面捷徑：一般視窗開啟、再點一次有提示、關閉後無殘留。',flush=True)
    assert show_command(shortcut)==1,'desktop shortcut must open normally, not minimized'
    system=package/'system';assert not processes(system)
    config=system/'config.local.json';before=config.stat().st_mtime_ns
    saved=dict(os.environ);clean=sanitized();os.environ.clear();os.environ.update(clean)
    try:os.startfile(str(shortcut))
    finally:os.environ.clear();os.environ.update(saved)
    try:
        deadline=time.monotonic()+180;owned=[]
        while time.monotonic()<deadline:
            owned=windows(system,'影音轉文字')
            if owned and config.stat().st_mtime_ns>before and not any(Path(p).name=='ollama.exe' for p in processes(system).values()):
                time.sleep(2);break
            time.sleep(.2)
        assert owned and config.stat().st_mtime_ns>before,'shortcut did not open a prepared GUI'
        screenshot(owned[0][0],case/'shortcut_home.bmp');first=set(processes(system))
        saved=dict(os.environ);os.environ.clear();os.environ.update(clean)
        try:os.startfile(str(shortcut))
        finally:os.environ.clear();os.environ.update(saved)
        deadline=time.monotonic()+60;notice=[]
        while time.monotonic()<deadline and not notice:notice=windows(system,'影音轉文字朋友版 提示');time.sleep(.2)
        assert notice,'second double-click must show a visible notice'
        time.sleep(1);screenshot(notice[0][0],case/'second_launch_notice.bmp')
        u.PostMessageW(notice[0][0],0x10,0,0)
        deadline=time.monotonic()+20
        while set(processes(system))-first and time.monotonic()<deadline:time.sleep(.1)
        assert not set(processes(system))-first and first<=set(processes(system)),'second launch must exit and leave the first window running'
        owned=windows(system,'影音轉文字')
        for hwnd,_,_ in owned:u.PostMessageW(hwnd,0x10,0,0)
        deadline=time.monotonic()+20
        while processes(system) and time.monotonic()<deadline:time.sleep(.1)
        assert not processes(system),'package process remained after closing the GUI'
    finally:kill(system)
    report.update(shortcut_opens_normal_window=True,shortcut_opens_gui=True,second_launch_shows_notice_and_keeps_first=True,owned_processes_gone_after_close=True)

    if args.previous:
        print('從上一版升級：只換變更檔，保留文字稿與紀錄，不需安裝資料。',flush=True)
        old_exe=next(args.previous.resolve().glob('*.exe'));old_zip=next(args.previous.resolve().glob('*.zip'))
        root,exe=isolated(case,'upgrade_old',old_exe);os.link(old_zip,root/'Downloads'/old_zip.name)
        target=root/'影音轉文字朋友版'
        assert silent(exe,root,target,case/'upgrade_old_install.log')==0
        (root/'Downloads'/old_zip.name).unlink()
        keep=[root/'Desktop/文字檔/朋友的稿.txt',target/'system/output_final/朋友的稿.srt',target/'system/logs/completed_media/keep.json']
        for path in keep:path.parent.mkdir(parents=True,exist_ok=True);path.write_text('friend data',encoding='utf-8')
        new_exe=root/'exe'/installer.name;shutil.copy2(installer,new_exe)
        started=time.monotonic()
        assert silent(new_exe,root,target,case/'upgrade.log',300)==0,(case/'upgrade.log').read_text(encoding='utf-8',errors='replace')[-2000:]
        with zipfile.ZipFile(payload) as archive:
            expected=json.loads(archive.read('影音轉文字朋友版/system/package-manifest.json'))
        for name,digest in expected['files'].items():
            if name.startswith('system/scripts/') or name=='開始轉錄.bat':assert sha(target/name)==digest,name
        assert sha(target/'system/package-manifest.json')==full['manifest_sha256']
        assert all(path.read_text(encoding='utf-8')=='friend data' for path in keep)
        assert show_command(root/'Desktop/開始轉錄.lnk')==1 and not list((root/'Downloads').glob('*.zip'))
        report.update(upgrade_in_place_without_payload=True,upgrade_keeps_friend_data=True,upgrade_seconds=round(time.monotonic()-started))
        shutil.rmtree(target)

    print('安裝視窗：已安裝時自動完成並自行關閉；缺資料時說明並等待。',flush=True)
    process,owned=launch_gui(main_exe,['--target',package,'--desktop-dir',desktop,'--downloads-dir',main_root/'Downloads','--no-launch'],'安裝影音轉文字朋友版')
    try:
        assert owned,'installer window did not open';time.sleep(1.5);screenshot(owned[0][0],case/'installer_reopen.bmp')
        assert process.wait(30)==0
    finally:
        if process.poll() is None:process.kill()
    root,exe=isolated(case,'gui_missing',offline_exe)
    process,owned=launch_gui(exe,['--target',root/'target','--desktop-dir',root/'Desktop','--downloads-dir',root/'Downloads','--no-launch'],'安裝影音轉文字朋友版')
    try:
        assert owned;time.sleep(2);screenshot(owned[0][0],case/'installer_missing.bmp')
        assert process.poll() is None,'window must wait for the user when the ZIP is missing'
        u.PostMessageW(owned[0][0],0x10,0,0);assert process.wait(20)==0
    finally:
        if process.poll() is None:process.kill()
    assert not (root/'target').exists()
    report.update(gui_reopen_completes_and_closes_itself=True,gui_missing_payload_waits_without_creating_target=True)

    if args.real_link:
        print('真實雲端：從分享連結下載到全新位置並完整安裝。',flush=True)
        url,page=drive_urls(args.real_link);real_exe=case/'real_link_installer.exe';write_installer(real_exe,{**meta,'url':url,'page':page})
        root,exe=isolated(case,'real_download',real_exe);started=time.monotonic()
        code=silent(exe,root,root/'影音轉文字朋友版',case/'real_download.log',4*3600)
        assert code==0,(case/'real_download.log').read_text(encoding='utf-8',errors='replace')[-3000:]
        fetched=next((root/'Downloads').glob('*.zip'));assert sha(fetched)==receipt['payload_sha256']
        assert (root/'Desktop'/'開始轉錄.lnk').is_file() and (root/'影音轉文字朋友版/system/config.local.json').is_file()
        report.update(real_link_download_and_install=True,real_download_seconds=round(time.monotonic()-started))
        shutil.rmtree(root/'影音轉文字朋友版');fetched.unlink()

    report.update(admin_required=False,seeded_models_or_tools=False,powershell_executed=False,
        human_content_accepted=False,friend_clean_machine_tested=False,case=str(case))
    (case/'installer_report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(report,ensure_ascii=False),flush=True)
if __name__=='__main__':main()
