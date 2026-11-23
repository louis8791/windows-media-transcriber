"""One-time guarded rename of this project's two desktop folders."""
from pathlib import Path
import hashlib
import json
import shutil
from datetime import datetime, timezone, timedelta
from contextlib import contextmanager

ROOT = Path(__file__).resolve().parents[1]
DESKTOP = (Path.home() / 'OneDrive' / 'Desktop').resolve()
PAIRS = [(DESKTOP/'廣播音檔', DESKTOP/'音檔'), (DESKTOP/'廣播文字檔', DESKTOP/'文字檔')]
MEDIA = {'.mp3','.mp4','.m4a','.wav','.flac','.aac','.ogg','.wma','.mov','.mkv','.webm'}

@contextmanager
def protect_runtime():
    import ctypes
    from ctypes import wintypes
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.CreateMutexW.argtypes=[ctypes.c_void_p,wintypes.BOOL,wintypes.LPCWSTR];kernel.CreateMutexW.restype=wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes=[wintypes.HANDLE,wintypes.DWORD]
    kernel.ReleaseMutex.argtypes=[wintypes.HANDLE];kernel.CloseHandle.argtypes=[wintypes.HANDLE]
    held=[]
    try:
        for name in ['Local\\BroadcastTranscribeWindowsSingleRunner','Local\\BroadcastTranscribeProofreadOnly']:
            handle=kernel.CreateMutexW(None,False,name)
            if not handle:raise ctypes.WinError(ctypes.get_last_error())
            if kernel.WaitForSingleObject(handle,0) not in (0,128):
                kernel.CloseHandle(handle);raise ValueError('轉錄或校稿工作仍在執行，不能改名')
            held.append(handle)
        yield
    finally:
        for handle in reversed(held):kernel.ReleaseMutex(handle);kernel.CloseHandle(handle)

def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream,'sha256').hexdigest()

def inventory(folder):
    return {p.relative_to(folder).as_posix():{'sha256':digest(p),'size':p.stat().st_size,'mtime_ns':p.stat().st_mtime_ns}
            for p in folder.rglob('*') if p.is_file()}

def job_id(path, stat, identity_root=None, input_root=None):
    identity = Path(identity_root)/Path(path).relative_to(input_root) if identity_root else Path(path)
    ticks = stat.st_mtime_ns//100 + 621355968000000000
    material = f'{identity}|{stat.st_size}|{ticks}'
    return 'job_'+hashlib.sha256(material.encode('utf-8')).hexdigest()[:16]

def replace_path(value):
    if not isinstance(value,str):
        return value
    for old,new in PAIRS:
        for old_text,new_text in ((str(old),str(new)),(old.as_posix(),new.as_posix())):
            if value.casefold()==old_text.casefold() or value.casefold().startswith((old_text+'\\').casefold()) or value.casefold().startswith((old_text+'/').casefold()):
                return new_text+value[len(old_text):]
    return value

def main():
    config_path=ROOT/'config.local.json'
    config=json.loads(config_path.read_text(encoding='utf-8-sig'))
    for key,(old,new) in zip(('desktop_input_dir','desktop_output_dir'),PAIRS):
        if old.parent!=DESKTOP or new.parent!=DESKTOP or old.resolve()!=old or new.resolve()!=new:
            raise ValueError('資料夾必須是明確指定的桌面直接子目錄')
        if not old.is_dir() or new.exists() or Path(config[key]).resolve()!=old:
            raise ValueError(f'來源、目標或設定不符，停止：{old} → {new}')
    backup=ROOT/'work/folder_rename'/datetime.now(timezone(timedelta(hours=8))).strftime('%Y%m%d_%H%M%S')
    backup.mkdir(parents=True,exist_ok=False)
    before={old.name:inventory(old) for old,_ in PAIRS}
    jobs=[]
    for path in PAIRS[0][0].rglob('*'):
        if path.is_file() and path.suffix.lower() in MEDIA:
            ident=job_id(path,path.stat())
            jobs.append({'relative':path.relative_to(PAIRS[0][0]).as_posix(),'old_job_id':ident,'existing_checkpoint':(ROOT/'whisper_raw/jobs'/ident).exists()})
    plan={'pairs':[{'old':str(old),'new':str(new)} for old,new in PAIRS],'before':before,'jobs':jobs,'backups':[],'changed_live_datasets':[],'changed_receipts':[],'changed_statuses':[]}
    manifest=backup/'migration.json'
    def record():manifest.write_text(json.dumps(plan,ensure_ascii=False,indent=2),encoding='utf-8')
    def write(path,content):
        target=backup/'files'/path.relative_to(ROOT)
        target.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(path,target)
        plan['backups'].append(str(path.relative_to(ROOT)));record()
        path.write_bytes(content)
    moved=[]
    record()
    try:
        for old,new in PAIRS:
            old.rename(new);moved.append((old,new))
        config['desktop_input_dir']=str(PAIRS[0][1]);config['desktop_output_dir']=str(PAIRS[1][1])
        config.setdefault('job_identity_input_root',str(PAIRS[0][0]))
        write(config_path,json.dumps(config,ensure_ascii=False,indent=2).encode('utf-8'))
        for path in (ROOT/'work/whisper_learning').rglob('dataset.json'):
            data=json.loads(path.read_text(encoding='utf-8-sig'));changed=False
            for row in data.get('records',[]):
                for key in ('source_path','audio_path'):
                    original=row.get(key);replacement=replace_path(original)
                    if replacement!=original:
                        row.setdefault('original_'+key,original);row[key]=replacement;changed=True
                        expected=row['source_sha256' if key=='source_path' else 'audio_sha256']
                        if digest(replacement)!=expected:raise ValueError('教材來源指紋不符')
            if changed:
                write(path,json.dumps(data,ensure_ascii=False,indent=2).encode('utf-8'));plan['changed_live_datasets'].append(str(path.relative_to(ROOT)))
        for path in (ROOT/'logs/completed_media').glob('*.json'):
            data=json.loads(path.read_text(encoding='utf-8-sig'));old_paths=list(data.get('input_paths',[]));new_paths=[replace_path(p) for p in old_paths]
            if new_paths!=old_paths:
                data['input_paths']=list(dict.fromkeys(old_paths+new_paths));data['input_path']=replace_path(data.get('input_path'))
                for value in new_paths:
                    if Path(value).is_file() and digest(value)!=data['media_sha256']:raise ValueError('完成紀錄音檔指紋不符')
                write(path,json.dumps(data,ensure_ascii=False,indent=2).encode('utf-8'));plan['changed_receipts'].append(str(path.relative_to(ROOT)))
        for path in (ROOT/'logs/status').glob('*.json'):
            data=json.loads(path.read_text(encoding='utf-8-sig'));changed=False
            for key,value in list(data.items()):
                if isinstance(value,str) and replace_path(value)!=value:
                    data.setdefault('original_'+key,value);data[key]=replace_path(value);changed=True
            if changed:
                write(path,json.dumps(data,ensure_ascii=False,indent=2).encode('utf-8'));plan['changed_statuses'].append(str(path.relative_to(ROOT)))
        for old,new in PAIRS:
            if before[old.name]!=inventory(new):raise ValueError('改名前後檔案內容、數量或修改時間不同')
        for row in jobs:
            new=PAIRS[0][1]/row['relative']
            row['new_job_id']=job_id(new,new.stat(),config['job_identity_input_root'],PAIRS[0][1])
            if row['old_job_id']!=row['new_job_id']:raise ValueError('續跑工作編號改變')
        plan['status']='completed';plan['files_unchanged']=True;record()
    except Exception:
        for relative in reversed(plan['backups']):shutil.copy2(backup/'files'/relative,ROOT/relative)
        for old,new in reversed(moved):
            if new.exists() and not old.exists():new.rename(old)
        plan['status']='rolled_back';record();raise
    print(json.dumps({'status':plan['status'],'backup':str(backup),'input_files':len(before[PAIRS[0][0].name]),'output_files':len(before[PAIRS[1][0].name]),'datasets':len(plan['changed_live_datasets']),'receipts':len(plan['changed_receipts']),'statuses':len(plan['changed_statuses']),'preserved_job_ids':len(jobs),'existing_checkpoint_jobs':sum(row['existing_checkpoint'] for row in jobs)},ensure_ascii=False))

if __name__=='__main__':
    with protect_runtime():main()
