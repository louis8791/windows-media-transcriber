"""Small first-download regression: resume, ignored ranges, corrupt bytes."""
import hashlib
import importlib.util
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import tempfile
import threading
import unittest
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from audio_review import native_runtime_files

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('friend_start', ROOT / 'packaging/friend_start.py')
friend = importlib.util.module_from_spec(spec)
spec.loader.exec_module(friend)


class DownloadTests(unittest.TestCase):
    def test_portable_cpu_variants_accepted_and_all_hash_inputs_retained(self):
        with tempfile.TemporaryDirectory(dir=ROOT/'test_artifacts') as temp:
            folder=Path(temp)
            names=['whisper.dll','ggml.dll','ggml-base.dll','ggml-cpu-x64.dll','ggml-cpu-haswell.dll']
            for name in names:(folder/name).write_bytes(b'runtime fixture')
            self.assertEqual({p.name for p in native_runtime_files(folder)},set(names))
            (folder/'ggml-cpu-x64.dll').unlink();(folder/'ggml-cpu-haswell.dll').unlink()
            with self.assertRaises(ValueError):native_runtime_files(folder)
    def test_resume_full_response_and_bad_hash(self):
        payload = b'verified-model-fixture' * 500
        expected = hashlib.sha256(payload).hexdigest()

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                offset = int(self.headers.get('Range', 'bytes=0-')[6:-1]) if self.path == '/range' else 0
                self.send_response(206 if offset else 200)
                if offset:
                    self.send_header('Content-Range', f'bytes {offset}-{len(payload)-1}/{len(payload)}')
                self.send_header('Content-Length', str(len(payload)-offset))
                self.end_headers()
                self.wfile.write(payload[offset:])

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory(dir=ROOT / 'test_artifacts') as temp:
                target = Path(temp) / 'model.bin'
                partial = target.with_suffix('.bin.partial')
                for route in ('range', 'full'):
                    partial.write_bytes(payload[:100])
                    friend.download(f'http://127.0.0.1:{server.server_port}/{route}', target, expected)
                    self.assertEqual(target.read_bytes(), payload)
                    self.assertFalse(partial.exists())
                    target.unlink()
                with self.assertRaises(RuntimeError):
                    friend.download(f'http://127.0.0.1:{server.server_port}/full', target, '0' * 64)
                self.assertFalse(target.exists())
                self.assertFalse(partial.exists())
        finally:
            server.shutdown()
            server.server_close()
            thread.join()
