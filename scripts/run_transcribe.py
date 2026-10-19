"""Desktop transcription runner: bounded Python orchestration, no PowerShell."""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from urllib.parse import urlparse
import uuid

from audio_review import sha, check_lock, native_runtime_files
from merge_whisper_chunks import atomic_write_text, merge_chunks
from normalize_text import convert_s2tw
from quality_report import build_report, review_text
from recheck_audio import parse_srt_strict
from process_guard import OwnedProcess, ProcessFailure, run_guarded, available_memory, memory_status

ROOT=Path(__file__).resolve().parents[1]
EXTENSIONS={'.wav','.mp3','.m4a','.mp4','.mov','.mkv','.aac','.flac','.ogg','.wma','.webm'}
OUTPUTS={'timeline':('.srt','.txt'),'txt':('.txt','.純文字.txt'),'srt':('.srt','.srt'),
         'cleaned':('.cleaned.txt','.cleaned.txt'),'review':('.審查報告.txt','.審查報告.txt')}
PERMANENT={'config','missing_tool','model_integrity','disk_space','memory','out_of_memory','permission','invalid_media','invalid_state','source_changed','empty_output'}


def write_json(path,data):atomic_write_text(Path(path),json.dumps(data,ensure_ascii=False,indent=2))
def read_json(path):return json.loads(Path(path).read_text(encoding='utf-8-sig'))
def suffixed(base,suffix):return Path(str(base)+suffix)
def snapshot(path):
    stat=Path(path).stat();return stat.st_size,stat.st_mtime_ns
def atomic_copy(source,target):
    target=Path(target);target.parent.mkdir(parents=True,exist_ok=True)
    handle,name=tempfile.mkstemp(prefix='.'+target.name+'.',suffix='.tmp',dir=target.parent)
    try:
        with os.fdopen(handle,'wb') as out,Path(source).open('rb') as stream:
            shutil.copyfileobj(stream,out);out.flush();os.fsync(out.fileno())
        os.replace(name,target)
    finally:Path(name).unlink(missing_ok=True)


class Pipeline:
    def __init__(self,config,*,force=False,retry=False,cancel=None,manual=False):
        self.config=config;self.force=force;self.retry=retry;self.cancel=cancel;self.manual=manual or force or retry
        self.workspace=Path(config['workspace_dir']).resolve()
        self.input=Path(config['desktop_input_dir']).resolve();self.output=Path(config['desktop_output_dir']).resolve()
        self.run_id=datetime.now().strftime('%Y%m%d_%H%M%S')+'_'+uuid.uuid4().hex[:8]
        self.started=set();self.announced=set();self.hash_cache={};self.cleanup_error=None;self.service=None;self.service_log=None
        self.validate_config()
        self.log_path=self.workspace/'logs'/('run_'+self.run_id+'.log')
        self.log_path.parent.mkdir(parents=True,exist_ok=True)

    def validate_config(self):
        if not self.workspace.is_relative_to(ROOT) or not self.input.is_dir():
            raise ProcessFailure('config','preflight','工作區需位於專案根目錄內，音檔資料夾需存在。')
        if self.output == self.input or self.output.is_relative_to(self.input):
            raise ProcessFailure('config','preflight','輸出資料夾不可放在音檔資料夾內。')
        self.outputs=self.config.get('desktop_outputs',['timeline'])
        if not isinstance(self.outputs,list) or any(token not in OUTPUTS for token in self.outputs):
            raise ProcessFailure('config','preflight','輸出格式設定無效。')
        self.chunk_seconds=int(self.config.get('whisper_chunk_seconds',300))
        if self.chunk_seconds < 30 or self.config.get('language','zh') not in ('zh','en','auto'):
            raise ProcessFailure('config','preflight','分段至少30秒，語言設定需有效。')
        for name,default in [('process_timeout_seconds',7200),('process_idle_seconds',900),('max_attempts',3),('retry_backoff_seconds',30),('disk_reserve_mb',256)]:
            value=self.config.get(name,default)
            if not isinstance(value,(int,float)) or not math.isfinite(value) or value<=0:
                raise ProcessFailure('config','preflight',f'{name}必須為有限正數。')
        if not 1 <= self.config.get('max_attempts',3) <= 5:
            raise ProcessFailure('config','preflight','自動嘗試次數需介於1至5次。')

    def log(self,message):
        print(message,flush=True)
        with self.log_path.open('a',encoding='utf-8') as stream:stream.write(str(message)+'\n')

    def check_cancel(self):
        if self.cancel is not None and Path(self.cancel).exists():
            raise ProcessFailure('cancelled','runner','已停止；完成分段保留。')

    def execute(self,command,stage,*,timeout=None,idle=None,progress=()):
        self.check_cancel();self.log(stage)
        return run_guarded(command,stage,cwd=ROOT,timeout=timeout or self.config.get('process_timeout_seconds',7200),
            idle_timeout=idle or self.config.get('process_idle_seconds',900),cancel=self.cancel,emit=self.log,progress_paths=progress)

    def fingerprint(self,path):
        key=(str(path),snapshot(path))
        if key not in self.hash_cache:self.hash_cache[key]=sha(path)
        return self.hash_cache[key]

    def base(self,media):return Path(media).resolve().relative_to(self.input).with_suffix('')
    def failure_path(self,media):
        settings={key:self.config.get(key) for key in ('language','whisper_chunk_seconds','model_path','vad_model_path','desktop_outputs','asr_recheck')}
        assets=[]
        for name in ('model_path','vad_model_path','whisper_cli_path','ffmpeg_path'):
            path=Path(self.config.get(name,''))
            try:assets.append((str(path),snapshot(path)))
            except OSError:assets.append((str(path),None))
        key=hashlib.sha256(json.dumps([self.fingerprint(media),settings,assets],sort_keys=True).encode()).hexdigest()
        return self.workspace/'logs/failures'/f'{key}.json'

    def status(self,path,media,status,detail='',**fields):
        record=read_json(path) if path.exists() else {'schema_version':3,'job_id':path.stem,'input_path':str(media),'status_history':[]}
        record['current_status']=status;record.update(fields)
        record['status_history'].append({'at':datetime.now(timezone.utc).isoformat(),'status':status,'detail':detail})
        write_json(path,record);self.log(f'{status}：{detail}')

    def receipt_path(self,fingerprint):return self.workspace/'logs/completed_media'/f'{fingerprint}.json'
    def receipt(self,media,evidence='pipeline_completed'):
        fingerprint=self.fingerprint(media);path=self.receipt_path(fingerprint)
        record=read_json(path) if path.exists() else {}
        if record and (record.get('schema_version')!=1 or record.get('media_sha256')!=fingerprint):
            raise ProcessFailure('invalid_state','completion','完成紀錄無效，原檔保留。')
        write_json(path,dict(schema_version=1,media_sha256=fingerprint,input_path=str(media),
            input_paths=list(dict.fromkeys(record.get('input_paths',[])+[str(media)])),
            completed_at=datetime.now(timezone.utc).isoformat(),evidence=evidence,output_base=str(self.base(media))))

    def delivered(self,media):
        base=self.output/self.base(media)
        if any(suffixed(base,ext).is_file() and suffixed(base,ext).stat().st_size for ext in ('.txt','.srt')):return True
        peers=[p for p in self.input.rglob('*') if p.is_file() and p.suffix.lower() in EXTENSIONS and p.stem==media.stem and p!=media]
        if peers:return False
        return any(p.stem==media.stem and p.suffix.lower() in ('.txt','.srt') and p.stat().st_size for p in self.output.rglob('*') if p.is_file())

    def completed(self,media):
        if self.force or self.retry:return False
        fingerprint=self.fingerprint(media);path=self.receipt_path(fingerprint)
        active=self.workspace/'work/transcribe'/fingerprint/'state.json'
        if active.exists() and read_json(active).get('status') in ('running','failed','cancelled'):return False
        if path.exists():
            record=read_json(path)
            if record.get('schema_version')!=1 or record.get('media_sha256')!=fingerprint or not record.get('completed_at') or not record.get('input_paths'):
                raise ProcessFailure('invalid_state','completion','完成紀錄無效，原檔保留。')
            if str(media) in record['input_paths']:return True
            if not any(Path(p).is_file() for p in record['input_paths']):self.receipt(media,'source_renamed');return True
        for prior in (self.workspace/'logs/completed_media').glob('*.json'):
            record=read_json(prior)
            if str(media) in record.get('input_paths',[]) and record.get('media_sha256')!=fingerprint:
                raise ProcessFailure('source_changed','completion','同路徑音檔已更換內容，請明確選擇重新轉錄。')
        if not path.exists() and self.delivered(media):self.receipt(media,'existing_delivered_transcript');return True
        return False

    def probe(self,media):
        exe=Path(self.config['ffmpeg_path']).with_name('ffprobe.exe')
        try:raw=self.execute([str(exe),'-v','error','-show_entries','format=duration','-of','json',str(media)],'probe',timeout=30,idle=30)
        except ProcessFailure as error:
            if error.reason=='process_exit':raise ProcessFailure('invalid_media','probe','音檔無法解碼：'+str(error)) from error
            raise
        try:
            seconds=float(json.loads(raw)['format']['duration'])
            if not math.isfinite(seconds) or seconds<=0:raise ValueError()
            return seconds
        except (ValueError,KeyError,TypeError) as error:raise ProcessFailure('invalid_media','probe','音檔長度無效。') from error

    def write_probe(self,folder):
        folder.mkdir(parents=True,exist_ok=True)
        handle,name=tempfile.mkstemp(prefix='.transcribe_probe_',dir=folder)
        try:
            with os.fdopen(handle,'wb') as out:out.write(b'probe');out.flush();os.fsync(out.fileno())
        finally:Path(name).unlink(missing_ok=True)

    def preflight(self,media,needs_asr):
        stage='preflight'
        try:
            with media.open('rb') as stream:stream.read(1)
            for folder in (self.workspace,self.output):self.write_probe(folder)
            reserve=self.config.get('disk_reserve_mb',256)*1024**2
            backup_size=sum(p.stat().st_size for folder in (self.output,self.workspace/'output_final',self.workspace/'whisper_raw')
                for p in (folder/self.base(media).parent).glob(self.base(media).name+'.*') if p.is_file()) if self.force or self.config.get('backup_before_delivery') else 0
            if shutil.disk_usage(self.workspace).free<backup_size+reserve or shutil.disk_usage(self.output).free<reserve:
                raise ProcessFailure('disk_space',stage,'工作區或輸出位置的可用空間不足。')
            if not needs_asr:return {'duration':None,'signature':None}
            paths=[Path(self.config[key]).resolve() for key in ('ffmpeg_path','whisper_cli_path','model_path','vad_model_path')]
            paths.append(paths[0].with_name('ffprobe.exe'))
            for path in paths:
                if not path.is_file() or not path.is_relative_to(ROOT):
                    raise ProcessFailure('missing_tool',stage,f'模型或工具缺少，或不在專案內：{path.name}')
            try:libraries=native_runtime_files(paths[1].parent)
            except ValueError as error:raise ProcessFailure('missing_tool',stage,str(error)) from error
            model,vad=paths[2:4]
            required=math.ceil(model.stat().st_size*1.5)+512*1024**2
            if available_memory()<required:raise ProcessFailure('memory',stage,f'可用記憶體不足；需至少約 {required/1024**3:.2f} GiB。')
            if memory_status().available<required:self.log('實體記憶體較緊，可能使用Windows分頁而變慢；仍保留逾時與停止保護。')
            manifest=read_json(ROOT/'config/model-manifest.json')
            hashes={}
            for role,path in [('speech_to_text',model),('voice_activity_detection',vad)]:
                expected=next((m['sha256'] for m in manifest['models'] if m['role']==role),None)
                actual=self.fingerprint(path)
                if actual!=expected:raise ProcessFailure('model_integrity',stage,f'{path.name}模型指紋不符。')
                hashes[role]=actual
            seconds=self.probe(media)
            required_disk=media.stat().st_size+math.ceil(seconds*32000*2.2)+backup_size+reserve
            if shutil.disk_usage(self.workspace).free<required_disk:
                raise ProcessFailure('disk_space',stage,f'暫存磁碟空間不足；需約 {required_disk/1024**2:.0f} MiB。')
            if shutil.disk_usage(self.output).free<reserve:
                raise ProcessFailure('disk_space',stage,'文字檔輸出位置的可用空間不足。')
            from opencc import OpenCC
            if OpenCC('s2tw').convert('汉字')!='漢字':raise ProcessFailure('config',stage,'OpenCC不可用。')
            terms=(ROOT/'data/custom_terms.txt').read_text(encoding='utf-8-sig') if (ROOT/'data/custom_terms.txt').exists() else ''
            hashes.update(whisper=self.fingerprint(paths[1]),libraries={path.name:self.fingerprint(path) for path in libraries},
                          ffmpeg=self.fingerprint(paths[0]),language=self.config.get('language','zh'),chunk_seconds=self.chunk_seconds,terms=terms,algorithm='python-asr-v3')
            signature=hashlib.sha256(json.dumps(hashes,sort_keys=True).encode()).hexdigest()
            self.log(f'開跑前檢查通過：暫存預留約 {required_disk/1024**2:.0f} MiB。')
            return {'duration':seconds,'signature':signature}
        except ProcessFailure:raise
        except OSError as error:raise ProcessFailure('permission',stage,f'來源或輸出位置無法讀寫：{error}') from error
        except (KeyError,ValueError,StopIteration) as error:raise ProcessFailure('config',stage,f'設定或模型清單無效：{error}') from error

    def backup(self,media):
        base=self.base(media);backup=self.workspace/'work/retranscribe_backups'/self.run_id
        for label,folder in [('desktop',self.output),('output_final',self.workspace/'output_final'),('whisper_raw',self.workspace/'whisper_raw')]:
            for suffix in ('.txt','.srt','.純文字.txt','.cleaned.txt','.審查報告.txt','.quality.json','.重查比對.txt','.recheck.json'):
                source=suffixed(folder/base,suffix)
                if source.is_file():atomic_copy(source,suffixed(backup/label/base,suffix))

    def ascii_path(self,path):
        text=Path(path).resolve().relative_to(ROOT).as_posix()
        try:text.encode('ascii')
        except UnicodeError as error:raise ProcessFailure('config','whisper','Whisper內部工作路徑必須為ASCII。') from error
        return text

    def transcribe(self,media,info,state_path,job,status_path,fresh):
        before=self.fingerprint(media);source_stat=snapshot(media)
        state=read_json(state_path) if state_path.exists() else {}
        generation=state.get('generation') if not fresh and state.get('signature')==info['signature'] else None
        if generation is None:generation=uuid.uuid4().hex
        if not isinstance(generation,str) or len(generation)!=32 or any(c not in '0123456789abcdef' for c in generation):
            raise ProcessFailure('invalid_state','checkpoint','分段輪次紀錄無效，原檔保留。')
        state={'generation':generation,'signature':info['signature'],'status':'running','source_sha256':before}
        write_json(state_path,state)
        chunks=job/generation/'chunks';chunks.mkdir(parents=True,exist_ok=True)
        wav=job/generation/'source.wav';marker=wav.with_suffix('.complete')
        valid_wav=False
        try:
            saved=read_json(marker)
            valid_wav=saved['source_sha256']==before and saved['wav_sha256']==sha(wav)
        except (OSError,ValueError,KeyError,TypeError):pass
        if not valid_wav:
            partial=wav.with_suffix('.partial.wav')
            self.status(status_path,media,'converting_audio','建立16kHz單聲道WAV')
            self.execute([self.config['ffmpeg_path'],'-nostdin','-v','warning','-y','-i',str(media),'-vn','-ac','1','-ar','16000','-c:a','pcm_s16le','-rf64','auto',str(partial)],'convert',progress=(partial,))
            os.replace(partial,wav);write_json(marker,{'source_sha256':before,'wav_sha256':sha(wav)})
        archive=self.workspace/'input_audio'/self.base(media)
        source_copy=suffixed(archive,media.suffix)
        if source_copy.exists() and sha(source_copy)!=before:source_copy=suffixed(archive,'_'+before[:12]+media.suffix)
        if not source_copy.exists():atomic_copy(media,source_copy)
        count=math.ceil(info['duration']/self.chunk_seconds);resumed=0
        for index in range(count):
            self.check_cancel();base=chunks/f'chunk_{index:05d}';complete=base.with_suffix('.complete')
            txt,srt=base.with_suffix('.txt'),base.with_suffix('.srt')
            valid=False
            try:
                saved=read_json(complete)
                valid=saved['signature']==info['signature'] and saved['txt_sha256']==sha(txt) and saved['srt_sha256']==sha(srt)
                if valid:parse_srt_strict(srt.read_text(encoding='utf-8-sig'))
            except (OSError,ValueError,KeyError,TypeError):valid=False
            if valid:resumed+=1;self.log(f'沿用已完成分段：{index+1}/{count}')
            else:
                complete.unlink(missing_ok=True)
                clip=base.with_suffix('.wav');start=index*self.chunk_seconds
                self.execute([self.config['ffmpeg_path'],'-nostdin','-v','warning','-y','-ss',str(start),'-t',str(min(self.chunk_seconds,info['duration']-start)),'-i',str(wav),'-c:a','copy',str(clip)],'split',progress=(clip,))
                args=['-m',self.ascii_path(self.config['model_path']),'-f',self.ascii_path(clip),'-l',self.config.get('language','zh'),'-mc','0','-tpi','0.20','--vad','-vm',self.ascii_path(self.config['vad_model_path']),'-otxt','-osrt','-of',self.ascii_path(base)]
                if self.config.get('language','zh')=='zh':
                    terms=(ROOT/'data/custom_terms.txt').read_text(encoding='utf-8-sig') if (ROOT/'data/custom_terms.txt').exists() else ''
                    terms='、'.join(line.strip() for line in terms.splitlines() if line.strip() and not line.strip().startswith('#'))
                    if terms:args+=['--prompt',terms]
                rsp=base.with_suffix('.rsp');atomic_write_text(rsp,'\n'.join(args)+'\n')
                self.status(status_path,media,'whisper_transcribing',f'{index+1}/{count}')
                self.execute([self.config['whisper_cli_path'],'@'+self.ascii_path(rsp)],'whisper',progress=(txt,srt))
                parse_srt_strict(srt.read_text(encoding='utf-8-sig'))
                if snapshot(media)!=source_stat:raise ProcessFailure('source_changed','checkpoint','轉錄期間原音變更，結果未發布。')
                write_json(complete,{'signature':info['signature'],'txt_sha256':sha(txt),'srt_sha256':sha(srt)})
            self.status(status_path,media,'whisper_chunk_completed',f'{index+1}/{count}',completed_chunks=index+1,total_chunks=count,resumed_chunks=resumed)
            partial_txt=job/'partial.txt';partial_srt=job/'partial.srt'
            merge_chunks(chunks,index+1,partial_txt,partial_srt,self.chunk_seconds)
            desktop=self.output/self.base(media)
            for token in self.outputs:
                if token in ('timeline','srt','txt'):
                    suffix='.轉錄中.純文字.txt' if token=='txt' else '.轉錄中.srt' if token=='srt' else '.轉錄中.txt'
                    text=(partial_txt if token=='txt' else partial_srt).read_text(encoding='utf-8-sig')
                    atomic_write_text(suffixed(desktop,suffix),convert_s2tw(text))
            atomic_write_text(suffixed(desktop,'.轉錄進度.txt'),f'轉錄尚未完成\n已完成分段：{index+1}/{count}\n中斷後可續跑。\n')
        if sha(media)!=before:raise ProcessFailure('source_changed','merge','原音變更，完整結果未發布。')
        staged_txt=job/'merged.txt';staged_srt=job/'merged.srt'
        merge_chunks(chunks,count,staged_txt,staged_srt,self.chunk_seconds)
        if not staged_txt.read_text(encoding='utf-8-sig').strip() or not parse_srt_strict(staged_srt.read_text(encoding='utf-8-sig')):
            raise ProcessFailure('empty_output','merge','沒有辨識到可用文字，保留工作資料，請聽校或換設定後重試。')
        for ext,path in [('.txt',staged_txt),('.srt',staged_srt)]:
            atomic_copy(path,suffixed(self.workspace/'whisper_raw'/self.base(media),ext))
            atomic_write_text(suffixed(self.workspace/'output_final'/self.base(media),ext),convert_s2tw(path.read_text(encoding='utf-8-sig')))
        return wav,state

    def ensure_cleanup(self):
        if self.cleanup_error:raise RuntimeError(self.cleanup_error)
        if self.config.get('isolated_ollama') and self.service is None:
            with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
            self.config['ollama_api_url']=f'http://127.0.0.1:{port}/api/generate'
        config=self.config;api=config.get('ollama_api_url','');parsed=urlparse(api)
        if parsed.hostname not in ('127.0.0.1','localhost','::1'):raise RuntimeError('校稿服務必須為本機loopback。')
        tags=api.rsplit('/api/',1)[0]+'/api/tags'
        def check():
            with urllib.request.urlopen(tags,timeout=3) as response:data=json.load(response)
            selected=next((m for m in data['models'] if m['name']==config['ollama_model']),None)
            expected=next(m for m in read_json(ROOT/'config/model-manifest.json')['models'] if m['role']=='transcript_cleanup')
            if selected is None or selected['name']!=expected['name'] or selected['digest']!=expected['model_digest']:
                raise RuntimeError('校稿模型未安裝或版本不符。')
        try:check();return
        except OSError:
            if self.service is not None:raise
            exe=Path(config.get('ollama_path',''))
            if not exe.is_file():raise RuntimeError('Ollama服務不可用。')
            env=dict(os.environ,OLLAMA_HOST=parsed.netloc,OLLAMA_MODELS=config['ollama_models_dir'])
            self.service_log=(self.workspace/'logs'/('ollama_'+self.run_id+'.log')).open('wb')
            self.service=OwnedProcess([str(exe),'serve'],cwd=ROOT,env=env,stdout=self.service_log,stderr=subprocess.STDOUT,stdin=subprocess.DEVNULL)
            until=time.monotonic()+30
            while time.monotonic()<until:
                self.check_cancel()
                try:check();return
                except OSError:time.sleep(.2)
            raise RuntimeError('Ollama啟動逾時。')

    def process_media(self,media):
        media=Path(media).resolve();self.base(media);fingerprint=self.fingerprint(media)
        job=self.workspace/'work/transcribe'/fingerprint;state_path=job/'state.json'
        status_path=self.workspace/'logs/status'/('job_py_'+fingerprint[:20]+'.json')
        final=self.workspace/'output_final'/self.base(media)
        txt,srt=suffixed(final,'.txt'),suffixed(final,'.srt')
        prior=read_json(state_path) if state_path.exists() else {}
        first=fingerprint not in self.started;fresh=self.force and first
        needs_asr=self.force or prior.get('status') in ('running','failed','cancelled') or not (txt.is_file() and srt.is_file() and txt.stat().st_size and srt.stat().st_size)
        archived=suffixed(self.workspace/'input_audio'/self.base(media),media.suffix)
        if not self.force and archived.exists() and sha(archived)!=fingerprint:
            raise ProcessFailure('source_changed','preflight','同名原音與舊稿來源不同，請明確選重新轉錄。')
        if needs_asr and self.config.get('reuse_only'):raise ProcessFailure('config','reuse','缺少可沿用完整稿，請選一般轉錄。')
        info=self.preflight(media,needs_asr)
        if first and (self.force or self.config.get('backup_before_delivery')):self.backup(media)
        self.started.add(fingerprint)
        self.status(status_path,media,'queued','Python轉錄工作；來源/工具指紋隔離')
        job.mkdir(parents=True,exist_ok=True)
        with check_lock(job/'writer'):
            try:
                wav,state=self.transcribe(media,info,state_path,job,status_path,fresh) if needs_asr else (None,prior)
                if not parse_srt_strict(srt.read_text(encoding='utf-8-sig')):
                    raise ProcessFailure('empty_output','quality','完整字幕沒有可用句子，未交付。')
                warnings=[];cleaned=suffixed(final,'.cleaned.txt')
                if fresh:cleaned.unlink(missing_ok=True)
                cleanup=str(self.config.get('use_ollama_cleanup','auto')).lower() not in ('false','off','disabled','never','0')
                if cleanup and (needs_asr or self.retry) and not cleaned.exists():
                    try:
                        self.ensure_cleanup()
                        self.execute([sys.executable,'-X','utf8',str(ROOT/'scripts/clean_transcript.py'),str(txt),str(cleaned),
                            '--model',self.config['ollama_model'],'--api-url',self.config['ollama_api_url'],'--max-chars',str(self.config.get('ollama_chunk_chars',900)),
                            '--context-size',str(self.config.get('ollama_context_size',8192)), '--cache-dir',str(job/'cleanup_cache')],'cleanup')
                    except ProcessFailure as error:
                        if error.reason=='cancelled':raise
                        self.cleanup_error=str(error);warnings.append('校稿未完成：'+str(error))
                    except Exception as error:self.cleanup_error=str(error);warnings.append('校稿未完成：'+str(error))
                quality=build_report(txt,srt,cleaned if cleaned.exists() else None)
                review=suffixed(final,'.審查報告.txt');write_json(suffixed(final,'.quality.json'),quality)
                atomic_write_text(review,review_text(quality));recheck_required=False
                if needs_asr and self.config.get('asr_recheck'):
                    recheck=job/'recheck';recheck.mkdir(exist_ok=True);cfg=recheck/'config.json';write_json(cfg,self.config)
                    try:
                        command=[sys.executable,'-X','utf8',str(ROOT/'scripts/recheck_audio.py'),'--config',str(cfg),'--wav',str(wav),'--srt',str(srt),'--output-dir',str(recheck),'--chunk-seconds',str(self.chunk_seconds)]
                        if cleanup and not self.cleanup_error:command+=['--qwen']
                        self.execute(command,'recheck')
                        atomic_copy(recheck/'report.json',suffixed(final,'.recheck.json'))
                        atomic_copy(recheck/'comparison.txt',suffixed(final,'.重查比對.txt'))
                        atomic_copy(recheck/'comparison.txt',suffixed(self.output/self.base(media),'.重查比對.txt'))
                        warnings.append('重查候選另存，仍須聽原音核對。');recheck_required=True
                    except ProcessFailure as error:
                        if error.reason=='cancelled':raise
                        warnings.append('音訊重查未完成：'+str(error));recheck_required=True
                if quality['content_review_status']=='blocked':warnings.append('機器審查未通過，請聽校。')
                if warnings:atomic_write_text(review,review_text(quality)+'\n'+'\n'.join(warnings)+'\n')
                desktop=self.output/self.base(media);tokens=set(self.outputs)
                if sha(media)!=fingerprint:raise ProcessFailure('source_changed','delivery','處理期間原音已改變，交付取消。')
                if quality['content_review_status']=='blocked' or recheck_required:tokens.add('review')
                for token,(source_suffix,target_suffix) in OUTPUTS.items():
                    source=suffixed(final,source_suffix);target=suffixed(desktop,target_suffix)
                    if token in tokens and source.exists():atomic_copy(source,target)
                    elif target.exists():target.unlink()
                for suffix in ('.轉錄中.txt','.轉錄中.純文字.txt','.轉錄中.srt','.轉錄進度.txt','.時間軸.txt','.轉錄中.時間軸.txt'):
                    suffixed(desktop,suffix).unlink(missing_ok=True)
                self.check_cancel()
                self.status(status_path,media,'completed_with_warning' if warnings else 'completed','；'.join(warnings) or '轉錄及交付完成')
                if state:state['status']='completed';write_json(state_path,state)
                self.receipt(media)
            except Exception as error:
                reason=error.reason if isinstance(error,ProcessFailure) else 'io_error' if isinstance(error,OSError) else 'invalid_state'
                if state_path.exists():
                    state=read_json(state_path);state['status']='cancelled' if reason=='cancelled' else 'failed';write_json(state_path,state)
                self.status(status_path,media,'interrupted' if reason=='cancelled' else 'failed',str(error),failure_reason=reason)
                raise

    def attempt(self,media):
        path=self.failure_path(media)
        record=read_json(path) if path.exists() else {'attempts':0,'isolated':False,'next_retry_at':0}
        if self.manual and str(media) not in getattr(self,'manual_reset',set()):
            self.manual_reset=getattr(self,'manual_reset',set())|{str(media)}
            record={'attempts':0,'isolated':False,'next_retry_at':0}
        if record['isolated'] or time.time()<record['next_retry_at']:
            announcement=(str(path),record['attempts'],record['isolated'])
            if announcement not in self.announced:
                self.announced.add(announcement)
                self.log(f'暫停自動重試：{media.name}；{record.get("reason", "等待重試間隔")}。明確勾選此檔再開始可手動重試。')
            return 'isolated' if record['isolated'] else 'waiting'
        try:
            if self.completed(media):self.log('已完成，略過：'+media.name);return 'completed'
            self.process_media(media)
            path.unlink(missing_ok=True);return 'completed'
        except Exception as error:
            reason=error.reason if isinstance(error,ProcessFailure) else 'io_error' if isinstance(error,OSError) else 'invalid_state'
            if reason=='cancelled':raise
            attempts=record['attempts']+1
            isolated=reason in PERMANENT or attempts>=int(self.config.get('max_attempts',3))
            record={'attempts':attempts,'isolated':isolated,'reason':reason,'stage':getattr(error,'stage','runner'),'error':str(error),
                    'next_retry_at':time.time()+self.config.get('retry_backoff_seconds',30)*2**(attempts-1),'input_path':str(media)}
            write_json(path,record)
            self.log(f'失敗 {attempts}/{int(self.config.get("max_attempts",3))}：{media.name}；{error}'+('；已隔離，不再自動重試。' if isolated else '；稍後有限重試。'))
            return 'isolated' if isolated else 'failed'

    def close(self):
        if self.service:self.service.close();self.service=None
        if self.service_log:self.service_log.close();self.service_log=None


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('-ConfigPath','--config',type=Path,default=ROOT/'config.local.json')
    parser.add_argument('-Once','--once',action='store_true')
    parser.add_argument('-Force','--force',action='store_true')
    parser.add_argument('-RetryCleanup','--retry-cleanup',action='store_true')
    parser.add_argument('-StableMinutes','--stable-minutes',type=float,default=3)
    parser.add_argument('-PollSeconds','--poll-seconds',type=float,default=5)
    parser.add_argument('--cancel',type=Path)
    args=parser.parse_args();pipeline=None
    try:
        config=read_json(args.config);selected={str(Path(p).resolve()) for p in config.get('selected_input_paths',[])}
        pipeline=Pipeline(config,force=args.force,retry=args.retry_cleanup,cancel=args.cancel,manual=bool(selected) or args.force or args.retry_cleanup)
        if any(not Path(p).is_file() or not Path(p).is_relative_to(pipeline.input) for p in selected):
            raise ProcessFailure('config','selection','勾選音檔已移動或超出音檔資料夾。')
        seen={};failed=False
        with check_lock(pipeline.workspace/'work/runner_lock'):
            while True:
                pipeline.check_cancel()
                files=sorted(p for p in pipeline.input.rglob('*') if p.is_file() and p.suffix.lower() in EXTENSIONS and (not selected or str(p.resolve()) in selected))
                for media in files:
                    stat=snapshot(media);prior=seen.get(media)
                    if prior is None or prior[0]!=stat:seen[media]=(stat,time.time())
                    stable=time.time()-max(seen[media][1],media.stat().st_mtime)
                    if stable<args.stable_minutes*60:continue
                    if getattr(pipeline,'finished',{}).get(str(media))==stat:continue
                    result=pipeline.attempt(media)
                    if result=='completed':
                        pipeline.finished=getattr(pipeline,'finished',{});pipeline.finished[str(media)]=stat
                    elif result!='waiting':failed=True
                if args.once:break
                until=time.monotonic()+max(.1,args.poll_seconds)
                while time.monotonic()<until:pipeline.check_cancel();time.sleep(.05)
        return 1 if failed else 0
    except ProcessFailure as error:
        print(str(error),flush=True);return 2 if error.reason=='cancelled' else 1
    except Exception as error:print('轉錄未完成：'+str(error),flush=True);return 1
    finally:
        if pipeline:pipeline.close()


if __name__=='__main__':raise SystemExit(main())
