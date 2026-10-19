"""Build the fully offline x64 friend ZIP from explicit, hash-checked assets."""
from pathlib import Path
import hashlib,json,shutil,subprocess,sys,time,zipfile

ROOT=Path(__file__).resolve().parents[1]
SOURCES=['run_transcribe.py','process_guard.py','audio_review.py','audio_review_panel.py','launch_menu.py',
         'proofread_only.py','clean_transcript.py','normalize_text.py','merge_whisper_chunks.py','quality_report.py','recheck_audio.py']
def sha(path):
    with Path(path).open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()
def copy(source,target):
    target=Path(target);target.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(source,target)
def git(*args):return subprocess.check_output(['git',*args],cwd=ROOT,text=True,creationflags=subprocess.CREATE_NO_WINDOW).strip()

def build():
    stamp=time.strftime('%Y%m%d_%H%M%S');output=ROOT/'work/output_packages'/('friend_offline_'+stamp)
    package=output/'影音轉文字朋友版';system=package/'system';python=system/'runtime/python'
    python.mkdir(parents=True)
    for name in ('音檔','文字檔'):(package/name).mkdir()
    base=Path(sys.base_prefix)
    print('準備包內Python、Tcl/Tk與OpenCC。',flush=True)
    for name in ('python.exe','pythonw.exe','python3.dll','python312.dll','vcruntime140.dll','vcruntime140_1.dll','LICENSE.txt'):
        copy(base/name,python/name)
    ignore=shutil.ignore_patterns('__pycache__','*.pyc','*.pyo','*.ps1','site-packages','test','tests','idlelib','ensurepip','venv')
    for name in ('Lib','DLLs','tcl'):shutil.copytree(base/name,python/name,ignore=ignore)
    import opencc
    shutil.copytree(Path(opencc.__path__[0]),python/'Lib/site-packages/opencc',ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
    (python/'python312._pth').write_text('.\nLib\nDLLs\nLib/site-packages\n../../scripts\n',encoding='ascii')
    for name in SOURCES:copy(ROOT/'scripts'/name,system/'scripts'/name)
    copy(ROOT/'packaging/friend_start.py',system/'scripts/friend_start.py')
    for name in ('start_friend.bat','朋友版使用說明.txt'):
        copy(ROOT/'packaging'/name,package/('開始轉錄.bat' if name.endswith('.bat') else name))
    whisper=ROOT/'tools/whisper.cpp/build-friend-cpu/bin/Release'
    for path in [whisper/'whisper-cli.exe',whisper/'whisper.dll',*whisper.glob('ggml*.dll')]:copy(path,system/'tools/whisper'/path.name)
    ffmpeg=ROOT/'tools/ffmpeg/bin'
    for name in ('ffmpeg.exe','ffprobe.exe'):copy(ffmpeg/name,system/'tools/ffmpeg'/name)
    ollama=Path.home()/'AppData/Local/Programs/Ollama'
    copy(ollama/'ollama.exe',system/'tools/ollama/ollama.exe')
    for path in (ollama/'lib/ollama').iterdir():
        if path.is_file() and path.suffix.lower() in ('.exe','.dll'):copy(path,system/'tools/ollama/lib/ollama'/path.name)
    windows=Path(__import__('os').environ['SystemRoot'])/'System32'
    for name in ('msvcp140.dll','msvcp140_1.dll','msvcp140_2.dll','vcruntime140.dll','vcruntime140_1.dll','concrt140.dll'):
        if (windows/name).is_file():
            copy(windows/name,system/'tools/ollama'/name)
            copy(windows/name,system/'tools/ollama/lib/ollama'/name)
    manifest=json.loads((ROOT/'config/model-manifest.json').read_text(encoding='utf-8-sig'))
    print('複製並核對Whisper與語音活動模型。',flush=True)
    for model in manifest['models']:
        if model['role'] in ('speech_to_text','voice_activity_detection'):
            source=ROOT/model['local_path'];assert sha(source)==model['sha256'];copy(source,system/model['local_path'])
    qwen=next(m for m in manifest['models'] if m['role']=='transcript_cleanup');assert qwen['name']=='qwen3:8b'
    model_path=Path('manifests/registry.ollama.ai/library/qwen3/8b');source=ROOT/'models/ollama'/model_path
    assert sha(source)==qwen['model_digest'];copy(source,system/'models/ollama'/model_path)
    spec=json.loads(source.read_text());print('複製並核對Qwen3 8B。',flush=True)
    for blob in [spec['config'],*spec['layers']]:
        name=blob['digest'].replace(':','-');source=ROOT/'models/ollama/blobs'/name
        assert sha(source)==blob['digest'].split(':')[1];copy(source,system/'models/ollama/blobs'/name)
        if blob.get('mediaType','').endswith('.license'):copy(source,system/'licenses/Qwen3-8B-LICENSE.txt')
    for source,name in [(ROOT/'tools/whisper.cpp/LICENSE','Whisper-LICENSE.txt'),(ROOT/'work/downloads/Ollama-LICENSE.txt','Ollama-LICENSE.txt'),
                        (ROOT/'tools/ffmpeg/LICENSE','FFmpeg-LICENSE.txt')]:
        if source.is_file():copy(source,system/'licenses'/name)
    copy(base/'LICENSE.txt',system/'licenses/Python-Tcl-Tk-LICENSE.txt')
    wheel=ROOT/'work/downloads/opencc_python_reimplemented-0.1.7-py2.py3-none-any.whl'
    with zipfile.ZipFile(wheel) as archive:
        for name in archive.namelist():
            if 'LICENSE' in name.upper():
                target=system/'licenses/OpenCC-LICENSE.txt';target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(archive.read(name));break
    (system/'config').mkdir(exist_ok=True)
    (system/'config/model-manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
    files={p.relative_to(package).as_posix():sha(p) for p in package.rglob('*') if p.is_file()}
    evidence=dict(schema_version=2,offline=True,version_git_head=git('rev-parse','HEAD'),source_dirty=bool(git('status','--porcelain')),
        built_at=stamp,platform='Windows x64 CPU',python_version=sys.version.split()[0],qwen=qwen['name'],
        source_hashes={name:sha(ROOT/'scripts'/name) for name in SOURCES},bootstrap_sha256=sha(ROOT/'packaging/friend_start.py'),files=files)
    (system/'package-manifest.json').write_text(json.dumps(evidence,ensure_ascii=False,indent=2),encoding='utf-8')
    target=output/('影音轉文字朋友版_完整離線環境_8B_'+stamp+'.zip')
    print('建立完整離線ZIP（含模型，需等待）。',flush=True)
    with zipfile.ZipFile(target,'w',zipfile.ZIP_DEFLATED,compresslevel=1,allowZip64=True) as archive:
        for path in package.rglob('*'):archive.write(path,path.relative_to(output))
    receipt={'zip':str(target),'bytes':target.stat().st_size,'sha256':sha(target),'offline':True}
    (output/'build.json').write_text(json.dumps(receipt,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(receipt,ensure_ascii=False),flush=True);return target
if __name__=='__main__':build()
