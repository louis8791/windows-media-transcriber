import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'scripts'),str(ROOT)]
import run_transcribe as runner
from process_guard import run_guarded,ProcessFailure
from launch_menu import request_commands


class ProcessGuardTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=ROOT/'test_artifacts');self.root=Path(self.temp.name)
        self.worker=self.root/'worker.py'
        self.worker.write_text('''import sys,time,subprocess,os
from pathlib import Path
mode=sys.argv[1]
if mode=='tree':
    p=subprocess.Popen([sys.executable,__file__,'sleep'])
    Path(sys.argv[2]).write_text(str(p.pid))
if mode=='oom':
    print('out of memory',flush=True);sys.exit(12)
if mode=='heartbeat':
    for i in range(12):print('progress',flush=True);time.sleep(.04)
    sys.exit(0)
if mode=='spam':
    while True:print('still alive',flush=True);time.sleep(.02)
time.sleep(30)
''',encoding='utf-8')
    def tearDown(self):self.temp.cleanup()
    def execute(self,mode,**kwargs):
        return run_guarded([sys.executable,'-u',str(self.worker),mode,*kwargs.pop('args',[])],mode,cwd=ROOT,timeout=kwargs.pop('timeout',3),idle_timeout=kwargs.pop('idle_timeout',.4),emit=lambda _:None,**kwargs)
    def test_idle_hang_exits_and_kills_descendant(self):
        child=self.root/'child.txt'
        with self.assertRaises(ProcessFailure) as caught:self.execute('tree',args=[str(child)])
        self.assertEqual(caught.exception.reason,'stalled')
        import ctypes
        pid=int(child.read_text());k=ctypes.WinDLL('kernel32');k.OpenProcess.restype=ctypes.c_void_p
        handle=k.OpenProcess(0x1000,False,pid)
        if handle:
            k.GetExitCodeProcess.argtypes=[ctypes.c_void_p,ctypes.c_void_p]
            code=ctypes.c_ulong();self.assertTrue(k.GetExitCodeProcess(handle,ctypes.byref(code)))
            k.CloseHandle.argtypes=[ctypes.c_void_p];k.CloseHandle(handle)
            self.assertNotEqual(code.value,259)
    def test_healthy_slow_progress_is_allowed(self):self.execute('heartbeat',idle_timeout=.3)
    def test_total_timeout_stops_even_with_noisy_output(self):
        with self.assertRaises(ProcessFailure) as caught:self.execute('spam',timeout=.4,idle_timeout=.3)
        self.assertEqual(caught.exception.reason,'timeout')
    def test_cancel_is_distinct_and_oom_is_classified(self):
        cancel=self.root/'cancel';cancel.write_text('stop')
        with self.assertRaises(ProcessFailure) as caught:self.execute('sleep',cancel=cancel)
        self.assertEqual(caught.exception.reason,'cancelled')
        with self.assertRaises(ProcessFailure) as caught:self.execute('oom')
        self.assertEqual(caught.exception.reason,'out_of_memory')


class PipelineSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=ROOT/'test_artifacts');self.root=Path(self.temp.name)
        self.input=self.root/'input';self.output=self.root/'output';self.workspace=self.root/'workspace'
        for path in (self.input,self.output,self.workspace):path.mkdir()
        self.media=self.input/'A'/'same.wav';self.media.parent.mkdir();self.media.write_bytes(b'source')
        self.config=json.loads((ROOT/'config.local.json').read_text(encoding='utf-8-sig'))
        self.config.update(desktop_input_dir=str(self.input),desktop_output_dir=str(self.output),workspace_dir=str(self.workspace),use_ollama_cleanup='false',desktop_outputs=['timeline'],whisper_chunk_seconds=30,retry_backoff_seconds=.01)
        self.pipeline=runner.Pipeline(self.config)
    def tearDown(self):self.pipeline.close();self.temp.cleanup()
    def test_disk_full_rejected_before_asr_or_output_changes(self):
        with patch('run_transcribe.shutil.disk_usage',return_value=shutil._ntuple_diskusage(100,100,0)),patch.object(self.pipeline,'execute') as execute:
            with self.assertRaises(ProcessFailure) as caught:self.pipeline.preflight(self.media,True)
        self.assertEqual(caught.exception.reason,'disk_space');execute.assert_not_called()
        self.assertFalse((self.workspace/'input_audio').exists())
    def test_write_permission_failure_rejected(self):
        with patch.object(self.pipeline,'write_probe',side_effect=PermissionError('locked')),patch.object(self.pipeline,'execute') as execute:
            with self.assertRaises(ProcessFailure) as caught:self.pipeline.preflight(self.media,True)
        self.assertEqual(caught.exception.reason,'permission');execute.assert_not_called()
    def test_low_memory_and_missing_tool_rejected(self):
        with patch('run_transcribe.available_memory',return_value=0):
            with self.assertRaises(ProcessFailure) as caught:self.pipeline.preflight(self.media,True)
        self.assertEqual(caught.exception.reason,'memory')
        self.pipeline.config['ffmpeg_path']=str(self.root/'missing.exe')
        with self.assertRaises(ProcessFailure) as caught:self.pipeline.preflight(self.media,True)
        self.assertEqual(caught.exception.reason,'missing_tool')
    def test_model_integrity_and_invalid_duration_block(self):
        with patch('run_transcribe.available_memory',return_value=100*1024**3),patch.object(self.pipeline,'fingerprint',return_value='wrong'):
            with self.assertRaises(ProcessFailure) as caught:self.pipeline.preflight(self.media,True)
        self.assertEqual(caught.exception.reason,'model_integrity')
        for text in (b'{"format":{"duration":"NaN"}}',b'{"format":{"duration":"0"}}'):
            with patch.object(self.pipeline,'execute',return_value=text):
                with self.assertRaises(ProcessFailure) as caught:self.pipeline.probe(self.media)
            self.assertEqual(caught.exception.reason,'invalid_media')
    def test_finite_retry_persists_restart_and_manual_retry_resets(self):
        error=ProcessFailure('stalled','whisper','simulated hang')
        with patch('run_transcribe.time.time',return_value=100.0) as clock,patch.object(self.pipeline,'completed',return_value=False),patch.object(self.pipeline,'process_media',side_effect=error) as work:
            self.assertEqual(self.pipeline.attempt(self.media),'failed')
            self.assertEqual(self.pipeline.attempt(self.media),'waiting')
            clock.return_value=100.025;self.assertEqual(self.pipeline.attempt(self.media),'failed')
            clock.return_value=100.070;self.assertEqual(self.pipeline.attempt(self.media),'isolated')
            self.assertEqual(work.call_count,3)
        reopened=runner.Pipeline(self.config)
        with patch.object(reopened,'process_media') as work:self.assertEqual(reopened.attempt(self.media),'isolated');work.assert_not_called()
        reopened.close()
        manual=runner.Pipeline(self.config,manual=True)
        with patch.object(manual,'completed',return_value=False),patch.object(manual,'process_media') as work:
            self.assertEqual(manual.attempt(self.media),'completed');work.assert_called_once()
        manual.close()
    def test_permanent_failure_isolated_after_one_and_other_file_continues(self):
        second=self.input/'B'/'same.wav';second.parent.mkdir();second.write_bytes(b'other source')
        def work(media):
            if media==self.media:raise ProcessFailure('disk_space','preflight','full')
        with patch.object(self.pipeline,'completed',return_value=False),patch.object(self.pipeline,'process_media',side_effect=work) as process:
            self.assertEqual(self.pipeline.attempt(self.media),'isolated')
            self.assertEqual(self.pipeline.attempt(self.media),'isolated')
            self.assertEqual(self.pipeline.attempt(second),'completed')
            self.assertEqual(process.call_count,2)
    def test_cancel_does_not_consume_retry_budget(self):
        with patch.object(self.pipeline,'completed',return_value=False),patch.object(self.pipeline,'process_media',side_effect=ProcessFailure('cancelled','whisper','stop')):
            with self.assertRaises(ProcessFailure):self.pipeline.attempt(self.media)
        self.assertFalse(self.pipeline.failure_path(self.media).exists())
    def test_gui_commands_use_python_for_runner_and_proofreading(self):
        for request in [('runner',self.root/'session.json',['-Once']),('proofread',[self.output/'a.txt'])]:
            for command in request_commands(request):
                self.assertFalse(any('powershell' in str(p).lower() or str(p).endswith('.ps1') for p in command))
        self.assertIn('--cancel',request_commands(('runner',self.root/'session.json',[]))[0])

    def test_completed_receipt_survives_transcript_and_source_rename(self):
        self.pipeline.receipt(self.media)
        self.assertTrue(self.pipeline.completed(self.media))
        renamed=self.media.with_name('renamed.wav');self.media.rename(renamed)
        self.assertTrue(self.pipeline.completed(renamed))
        self.assertIn(str(renamed),runner.read_json(self.pipeline.receipt_path(self.pipeline.fingerprint(renamed)))['input_paths'])

    def test_replaced_source_does_not_borrow_same_name_completion(self):
        self.pipeline.receipt(self.media);self.media.write_bytes(b'replacement content')
        self.assertEqual(self.pipeline.attempt(self.media),'isolated')
        self.assertEqual(runner.read_json(self.pipeline.failure_path(self.media))['reason'],'source_changed')

    def test_monitor_retries_three_then_isolates_without_spamming_or_restarting(self):
        wrapper=self.root/'monitor.py';config=self.root/'monitor_config.json';cancel=self.root/'stop'
        data=dict(self.config,selected_input_paths=[]);config.write_text(json.dumps(data),encoding='utf-8')
        wrapper.write_text('''import sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
import run_transcribe as r
from process_guard import ProcessFailure
def fail(self,media):raise ProcessFailure('stalled','whisper','injected monitor hang')
r.Pipeline.process_media=fail
sys.argv=['runner','--config',sys.argv[2],'--stable-minutes','0','--poll-seconds','.02','--cancel',sys.argv[3]]
raise SystemExit(r.main())
''',encoding='utf-8')
        log_path=self.root/'monitor.log'
        with log_path.open('wb') as log:
            process=subprocess.Popen([sys.executable,'-X','utf8',str(wrapper),str(ROOT/'scripts'),str(config),str(cancel)],stdout=log,stderr=subprocess.STDOUT,creationflags=subprocess.CREATE_NO_WINDOW)
            try:
                deadline=time.monotonic()+5;path=self.pipeline.failure_path(self.media)
                while time.monotonic()<deadline:
                    if path.exists() and runner.read_json(path)['isolated']:break
                    time.sleep(.02)
                self.assertEqual(runner.read_json(path)['attempts'],3)
                time.sleep(.15);self.assertEqual(runner.read_json(path)['attempts'],3)
                cancel.write_text('cancel');process.wait(timeout=5);self.assertEqual(process.returncode,2)
            finally:
                if process.poll() is None:process.kill();process.wait()
        self.assertEqual(log_path.read_text(encoding='utf-8').count('失敗 '),3)

    def test_identical_copy_reuses_checkpoints_and_nested_names_stay_separate(self):
        info={'duration':40,'signature':'fixture_signature'}
        second=self.input/'B/same.wav';second.parent.mkdir();second.write_bytes(self.media.read_bytes())
        with patch.object(self.pipeline,'preflight',return_value=info),patch.object(self.pipeline,'execute',side_effect=self.fake_engine) as execute:
            self.assertEqual(self.pipeline.attempt(self.media),'completed')
            count=execute.call_count
            self.assertEqual(self.pipeline.attempt(second),'completed')
            self.assertEqual(execute.call_count,count)
        self.assertEqual((self.output/'A/same.txt').read_bytes(),(self.output/'B/same.txt').read_bytes())

    def test_optional_cleanup_failure_keeps_transcript_and_opens_circuit(self):
        self.pipeline.config['use_ollama_cleanup']='auto'
        with patch.object(self.pipeline,'preflight',return_value={'duration':40,'signature':'fixture'}),patch.object(self.pipeline,'execute',side_effect=self.fake_engine),patch.object(self.pipeline,'ensure_cleanup',side_effect=RuntimeError('service down')):
            self.assertEqual(self.pipeline.attempt(self.media),'completed')
        self.assertTrue((self.output/'A/same.txt').exists())
        self.assertFalse((self.workspace/'output_final/A/same.cleaned.txt').exists())
        status=next((self.workspace/'logs/status').glob('*.json'))
        self.assertEqual(runner.read_json(status)['current_status'],'completed_with_warning')
        with patch('run_transcribe.urllib.request.urlopen') as request:
            with self.assertRaisesRegex(RuntimeError,'service down'):runner.Pipeline.ensure_cleanup(self.pipeline)
            request.assert_not_called()

    def test_standalone_proofreading_uses_same_readiness_without_powershell(self):
        import proofread_only
        with patch('run_transcribe.Pipeline') as factory,patch('proofread_only.proofread',return_value=0) as proofread:
            self.assertEqual(proofread_only.main(),0)
            factory.return_value.ensure_cleanup.assert_called_once()
            factory.return_value.close.assert_called_once();proofread.assert_called_once_with(factory.return_value.config)

    def test_portable_cleanup_chooses_own_port_and_never_reuses_old_endpoint(self):
        import urllib.error
        expected=next(m for m in runner.read_json(ROOT/'config/model-manifest.json')['models'] if m['role']=='transcript_cleanup')
        self.pipeline.config.update(isolated_ollama=True,ollama_model=expected['name'])
        old=self.pipeline.config['ollama_api_url']
        class Response:
            def __enter__(self):return self
            def __exit__(self,*_):pass
            def read(self):return json.dumps({'models':[{'name':expected['name'],'digest':expected['model_digest']}]}).encode()
        with patch('run_transcribe.OwnedProcess'),patch('run_transcribe.urllib.request.urlopen',side_effect=[urllib.error.URLError('not running'),Response()]) as request:
            self.pipeline.ensure_cleanup()
        self.assertNotEqual(self.pipeline.config['ollama_api_url'],old)
        self.assertTrue(self.pipeline.config['ollama_api_url'].startswith('http://127.0.0.1:'))
        self.assertTrue(all(call.args[0]!=old.rsplit('/api/',1)[0]+'/api/tags' for call in request.call_args_list))

    def fake_engine(self,command,stage,**kwargs):
        if stage=='convert':Path(command[-1]).write_bytes(b'wav')
        if stage=='split':Path(command[-1]).write_bytes(b'clip')
        if stage=='whisper':
            rsp=ROOT/command[1][1:];args=rsp.read_text().splitlines();base=ROOT/args[args.index('-of')+1]
            base.with_suffix('.txt').write_text('Hello, friends.\n')
            base.with_suffix('.srt').write_text('1\n00:00:00,000 --> 00:00:01,000\nHello, friends.\n')
        return b''
    def test_second_chunk_hang_preserves_first_then_resume_and_delivery(self):
        calls=[]
        def hang(command,stage,**kwargs):
            if stage=='whisper':
                calls.append(command)
                if len(calls)==2:raise ProcessFailure('stalled',stage,'hang second chunk')
            return self.fake_engine(command,stage,**kwargs)
        info={'duration':40,'signature':'fixture_signature'}
        with patch.object(self.pipeline,'preflight',return_value=info),patch.object(self.pipeline,'execute',side_effect=hang):
            self.assertEqual(self.pipeline.attempt(self.media),'failed')
        complete=next((self.workspace/'work/transcribe').glob('*/*/chunks/chunk_00000.complete'))
        first=complete.read_bytes();mtime=complete.stat().st_mtime_ns
        self.assertFalse((self.output/'A/same.txt').exists());self.assertTrue((self.output/'A/same.轉錄中.txt').exists())
        self.assertFalse(self.pipeline.receipt_path(self.pipeline.fingerprint(self.media)).exists())
        time.sleep(.025)
        with patch.object(self.pipeline,'preflight',return_value=info),patch.object(self.pipeline,'execute',side_effect=self.fake_engine) as execute:
            self.assertEqual(self.pipeline.attempt(self.media),'completed')
        self.assertEqual(sum(call.args[1]=='whisper' for call in execute.call_args_list),1)
        self.assertEqual(complete.read_bytes(),first);self.assertEqual(complete.stat().st_mtime_ns,mtime)
        self.assertEqual((self.output/'A/same.txt').read_bytes(),(self.workspace/'output_final/A/same.srt').read_bytes())
        self.assertFalse((self.output/'A/same.轉錄中.txt').exists())
    def test_force_backup_before_failure_and_retry_refuses_asr(self):
        base=self.workspace/'output_final/A/same';base.parent.mkdir(parents=True)
        base.with_suffix('.txt').write_text('original')
        base.with_suffix('.srt').write_text('1\n00:00:00,000 --> 00:00:01,000\noriginal\n')
        force=runner.Pipeline(self.config,force=True)
        with patch.object(force,'preflight',return_value={'duration':40,'signature':'fixture'}),patch.object(force,'execute',side_effect=ProcessFailure('stalled','convert','hang')):
            self.assertEqual(force.attempt(self.media),'failed')
        self.assertEqual(next((self.workspace/'work/retranscribe_backups').glob('*/output_final/A/same.txt')).read_text(),'original')
        self.assertEqual(base.with_suffix('.txt').read_text(),'original');force.close()
        reuse=runner.Pipeline(dict(self.config,reuse_only=True),retry=True)
        with patch.object(reuse,'execute') as work:self.assertEqual(reuse.attempt(self.media),'isolated');work.assert_not_called()
        reuse.close()


if __name__=='__main__':unittest.main()
