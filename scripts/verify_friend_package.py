"""Acceptance from the actual offline ZIP, with no model/runtime seeding."""
from pathlib import Path
import argparse,hashlib,json,os,shutil,subprocess,time,zipfile

ROOT=Path(__file__).resolve().parents[1]
def sha(path):
    with Path(path).open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()
def main():
    parser=argparse.ArgumentParser();parser.add_argument('zip',type=Path);parser.add_argument('--resume-case',type=Path);args=parser.parse_args()
    case=args.resume_case.resolve() if args.resume_case else ROOT/'work/acceptance'/time.strftime('friend_offline_%Y%m%d_%H%M%S')
    assert case.is_relative_to(ROOT/'work/acceptance')
    case.mkdir(parents=True,exist_ok=bool(args.resume_case))
    print('核對实际ZIP及解壓內容，沒有補入任何模型或工具。',flush=True)
    with zipfile.ZipFile(args.zip) as archive:
        names=archive.namelist()
        for name in names:
            parts=Path(name).parts
            assert not any(p in ('.git','.venv','__pycache__','logs','userhome','temp') for p in parts),name
            assert not name.endswith(('.pyc','.ps1','.wav','.mp3','.mp4','config.local.json')),name
            assert not Path(name).is_absolute() and '..' not in parts,name
        if not args.resume_case:
            assert archive.testzip() is None;archive.extractall(case)
    package=case/'影音轉文字朋友版';system=package/'system'
    manifest=json.loads((system/'package-manifest.json').read_text(encoding='utf-8'))
    assert manifest['offline'] and manifest['qwen']=='qwen3:8b'
    for name,expected in manifest['files'].items():assert sha(package/name)==expected,name
    for name,expected in manifest['source_hashes'].items():assert sha(ROOT/'scripts'/name)==expected,name
    env=dict(os.environ,PATH=os.environ['SystemRoot']+'\\System32;'+os.environ['SystemRoot'],
        PYTHONUTF8='1',PYTHONIOENCODING='utf-8',BROADCAST_TRANSCRIBE_NO_PAUSE='1')
    for key in ('PYTHONPATH','PYTHONHOME','OLLAMA_HOST','OLLAMA_MODELS','TCL_LIBRARY','TK_LIBRARY'):env.pop(key,None)
    shell=str(Path(os.environ['SystemRoot'])/'System32/cmd.exe');launcher=str(package/'開始轉錄.bat')
    def launch(name,*arguments,timeout=1800):
        with (case/(name+'.log')).open('wb') as log:
            result=subprocess.run([shell,'/d','/c',launcher,*arguments],cwd=package,env=env,stdout=log,stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NO_WINDOW,timeout=timeout)
        assert result.returncode==0,(case/(name+'.log')).read_text(encoding='utf-8',errors='replace')[-3000:]
    launch('prepare','--prepare-only',timeout=180)
    prepared=json.loads((system/'config.local.json').read_text(encoding='utf-8'))
    assert Path(prepared['desktop_input_dir'])==package/'音檔'
    assert Path(prepared['desktop_output_dir'])==package/'文字檔'
    fixture=ROOT/'test_artifacts/audio_review_20261013/opening.wav'
    source=package/'音檔/朋友測試.wav'
    if not source.exists():
        subprocess.run([str(system/'tools/ffmpeg/ffmpeg.exe'),'-nostdin','-v','error','-n','-i',str(fixture),'-t','40','-c:a','pcm_s16le',str(source)],
            check=True,creationflags=subprocess.CREATE_NO_WINDOW,timeout=30)
    if args.resume_case and (case/'transcribe.log').exists():
        (case/'transcribe_memory_block.log').write_bytes((case/'transcribe.log').read_bytes())
    print('解壓包真Whisper兩段辨識與Qwen3 8B校稿。',flush=True)
    launch('transcribe','--once','--stable-minutes','0','--chunk-seconds','30','--language','en','--only-name','朋友測試.wav')
    text=package/'文字檔/朋友測試.txt';srt=system/'output_final/朋友測試.srt';cleaned=package/'文字檔/朋友測試.cleaned.txt'
    assert text.read_bytes()==srt.read_bytes();assert cleaned.is_file() and cleaned.stat().st_size
    assert not list((package/'文字檔').glob('*轉錄中*'))
    checkpoints=list((system/'work/transcribe').glob('*/*/chunks/chunk_*.complete'));assert len(checkpoints)==2
    before={str(p):p.stat().st_mtime_ns for p in checkpoints}
    launch('skip','--once','--stable-minutes','0','--language','en','--only-name','朋友測試.wav')
    assert all(Path(p).stat().st_mtime_ns==mtime for p,mtime in before.items())
    report=dict(zip=str(args.zip.resolve()),zip_sha256=sha(args.zip),zip_bytes=args.zip.stat().st_size,extracted=str(package),
        seeded_models_or_tools=False,sanitized_path=True,offline_prepare_passed=True,actual_two_chunk_asr=True,actual_qwen3_8b_cleanup=True,
        timeline_equals_srt=True,completed_skip_preserves_checkpoints=True,powershell_executed=False,source_hashes=manifest['source_hashes'],
        human_content_accepted=False,friend_clean_machine_tested=False)
    (case/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    (ROOT/'work/acceptance/friend_offline_latest.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(report,ensure_ascii=False),flush=True)
if __name__=='__main__':main()
