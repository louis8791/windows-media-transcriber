"""Offline portable setup and native GUI; never calls PowerShell or global runtimes."""
import argparse,hashlib,json,os,queue,socket,subprocess,sys,threading,time
from pathlib import Path
import urllib.request,urllib.error

SYSTEM=Path(__file__).resolve().parents[1];PACKAGE=SYSTEM.parent
def digest(path):
    with Path(path).open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()

def download(url,destination,expected):
    # Retained for the bounded download regression; offline setup never calls it.
    destination=Path(destination)
    if destination.is_file() and digest(destination)==expected:return
    destination.parent.mkdir(parents=True,exist_ok=True);partial=destination.with_suffix(destination.suffix+'.partial')
    offset=partial.stat().st_size if partial.exists() else 0
    request=urllib.request.Request(url,headers={'Range':f'bytes={offset}-'} if offset else {})
    with urllib.request.urlopen(request,timeout=45) as response:
        append=offset>0 and response.status==206
        if append and not response.headers.get('Content-Range','').startswith(f'bytes {offset}-'):raise RuntimeError('下載續傳位置不符。')
        with partial.open('ab' if append else 'wb') as output:
            while block:=response.read(1024**2):output.write(block)
    if digest(partial)!=expected:partial.unlink();raise RuntimeError('下載指紋不符。')
    partial.replace(destination)

def environment():
    windows=os.environ['SystemRoot'];runtime=SYSTEM/'runtime/python'
    os.environ.update(TCL_LIBRARY=str(runtime/'tcl/tcl8.6'),TK_LIBRARY=str(runtime/'tcl/tk8.6'),
        PATH=os.pathsep.join([str(runtime),str(SYSTEM/'tools/ollama'),windows+'\\System32',windows]),
        OLLAMA_MODELS=str(SYSTEM/'models/ollama'),OLLAMA_NO_CLOUD='1',OLLAMA_NUM_PARALLEL='1',OLLAMA_MAX_LOADED_MODELS='1',
        OLLAMA_CONTEXT_LENGTH='8192',USERPROFILE=str(SYSTEM/'userhome'),LOCALAPPDATA=str(SYSTEM/'userhome/AppData/Local'),
        APPDATA=str(SYSTEM/'userhome/AppData/Roaming'),TEMP=str(SYSTEM/'temp'),TMP=str(SYSTEM/'temp'),
        PYTHONIOENCODING='utf-8',PYTHONDONTWRITEBYTECODE='1',NO_PROXY='127.0.0.1,localhost',no_proxy='127.0.0.1,localhost')

def prepare(args,say,services):
    from process_guard import OwnedProcess,run_guarded
    from merge_whisper_chunks import atomic_write_text
    if sys.platform!='win32' or __import__('ctypes').sizeof(__import__('ctypes').c_void_p)!=8:raise RuntimeError('僅支援Windows x64。')
    manifest=json.loads((SYSTEM/'package-manifest.json').read_text(encoding='utf-8'))
    if not manifest.get('offline'):raise RuntimeError('不是完整離線包。')
    for name,expected in manifest['files'].items():
        path=(PACKAGE/name).resolve()
        if not path.is_relative_to(PACKAGE.resolve()):raise RuntimeError('包內清單路徑無效。')
        if path.stat().st_size>100*1024**2:say('核對包內環境：'+path.name)
        if digest(path)!=expected:raise RuntimeError('環境檔案缺少或指紋不符，請重新解壓：'+name)
    for name in ('音檔','文字檔'):(PACKAGE/name).mkdir(exist_ok=True)
    for name in ('logs','temp','userhome/AppData/Local','userhome/AppData/Roaming'):(SYSTEM/name).mkdir(parents=True,exist_ok=True)
    from opencc import OpenCC
    if OpenCC('s2tw').convert('汉字')!='漢字':raise RuntimeError('包內OpenCC驗證失敗。')
    for tool in ('ffmpeg.exe','ffprobe.exe'):
        run_guarded([str(SYSTEM/'tools/ffmpeg'/tool),'-version'],'環境檢查',cwd=SYSTEM,timeout=20,idle_timeout=20,emit=lambda _:None)
    with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    host=f'http://127.0.0.1:{port}';os.environ['OLLAMA_HOST']=host
    log=(SYSTEM/'logs/ollama.log').open('ab');services.append(log)
    service=OwnedProcess([str(SYSTEM/'tools/ollama/ollama.exe'),'serve'],cwd=SYSTEM,env=dict(os.environ),
        stdout=log,stderr=subprocess.STDOUT,stdin=subprocess.DEVNULL);services.append(service)
    say('啟動包內校稿服務。');deadline=time.monotonic()+40
    tags=None
    while time.monotonic()<deadline:
        if service.process.poll() is not None:raise RuntimeError('包內校稿服務啟動失敗。')
        try:
            with urllib.request.urlopen(host+'/api/tags',timeout=2) as response:tags=json.load(response)
            break
        except OSError:time.sleep(.2)
    if tags is None:raise RuntimeError('包內校稿服務啟動逾時。')
    model_manifest=json.loads((SYSTEM/'config/model-manifest.json').read_text(encoding='utf-8'))
    qwen=next(m for m in model_manifest['models'] if m['role']=='transcript_cleanup')
    if not any(m['name']==qwen['name'] and m['digest']==qwen['model_digest'] for m in tags['models']):raise RuntimeError('包內Qwen模型版本不符。')
    config=dict(project_root=str(SYSTEM),workspace_dir=str(SYSTEM),desktop_input_dir=str(PACKAGE/'音檔'),desktop_output_dir=str(PACKAGE/'文字檔'),
        desktop_outputs=['timeline','cleaned'],model_path=str(SYSTEM/'models/ggml-large-v3.bin'),vad_model_path=str(SYSTEM/'models/ggml-silero-v6.2.0.bin'),
        language=args.language,whisper_chunk_seconds=args.chunk_seconds,use_ollama_cleanup='false' if args.skip_cleanup else 'auto',
        ollama_model=qwen['name'],ollama_context_size=8192,ollama_chunk_chars=900,ollama_api_url=host+'/api/generate',isolated_ollama=True,
        ollama_models_dir=str(SYSTEM/'models/ollama'),ollama_path=str(SYSTEM/'tools/ollama/ollama.exe'),
        ffmpeg_path=str(SYSTEM/'tools/ffmpeg/ffmpeg.exe'),whisper_cli_path=str(SYSTEM/'tools/whisper/whisper-cli.exe'),
        process_timeout_seconds=7200,process_idle_seconds=900,max_attempts=3,retry_backoff_seconds=30,disk_reserve_mb=256)
    if args.only_name:
        matches=[p for p in (PACKAGE/'音檔').rglob('*') if p.is_file() and p.name==args.only_name]
        if len(matches)!=1:raise RuntimeError('指定檔名不唯一或不存在，請用視窗勾選。')
        config['selected_input_paths']=[str(matches[0])]
    atomic_write_text(SYSTEM/'config.local.json',json.dumps(config,ensure_ascii=False,indent=2))
    for item in reversed(services):item.close()
    services.clear()
    say('環境已就緒，模型不需下載；校稿服務會在需要時啟動。')
    return config

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--prepare-only',action='store_true');parser.add_argument('--once',action='store_true')
    parser.add_argument('--stable-minutes',type=float,default=3);parser.add_argument('--chunk-seconds',type=int,default=300)
    parser.add_argument('--only-name');parser.add_argument('--language',choices=['zh','en'],default='zh');parser.add_argument('--skip-cleanup',action='store_true')
    args=parser.parse_args();environment();services=[]
    from audio_review import check_lock
    try:
        with check_lock(SYSTEM/'work/bootstrap_lock'):
            if args.prepare_only or args.once:
                config=prepare(args,lambda message:print(message,flush=True),services)
                if args.prepare_only:return 0
                import run_transcribe
                sys.argv=['runner','-Once','-StableMinutes',str(args.stable_minutes)]
                return run_transcribe.main()
            import tkinter as tk
            from tkinter import ttk
            window=tk.Tk();window.title('影音轉文字朋友版');window.geometry('720x280')
            frame=ttk.Frame(window,padding=24);frame.pack(fill='both',expand=True)
            status=tk.StringVar(value='正在準備包內環境，請稍候。')
            ttk.Label(frame,text='影音轉文字｜完整離線朋友版',font=('Microsoft JhengHei',18)).pack(anchor='w',pady=12)
            ttk.Label(frame,textvariable=status,wraplength=650).pack(anchor='w',pady=12)
            events=queue.Queue();closing=threading.Event()
            def worker():
                try:events.put(('ready',prepare(args,lambda message:events.put(('status',message)),services)))
                except Exception as error:events.put(('error',str(error)))
            thread=threading.Thread(target=worker,daemon=True);thread.start()
            def close():closing.set();window.destroy()
            window.protocol('WM_DELETE_WINDOW',close)
            def poll():
                if closing.is_set():return
                while not events.empty():
                    kind,value=events.get_nowait()
                    if kind=='status':status.set(value)
                    elif kind=='error':status.set('環境準備失敗：'+value);ttk.Button(frame,text='關閉',command=close).pack(anchor='w');return
                    else:
                        frame.destroy()
                        from launch_menu import WizardMenu
                        menu=WizardMenu(window,value)
                        window.protocol('WM_DELETE_WINDOW',menu.cancel)
                        return
                window.after(100,poll)
            window.after(100,poll);window.mainloop();closing.set()
            thread.join(timeout=1)
            return 0
    finally:
        for service in reversed(services):service.close()

if __name__=='__main__':
    try:raise SystemExit(main())
    except Exception as error:
        if sys.stdout is not None:print('環境或轉錄未完成：'+str(error),flush=True)
        raise SystemExit(1)
