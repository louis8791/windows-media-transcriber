import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch, MagicMock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from clean_transcript import clean_text, generate, OutputTruncatedError
from proofread_only import transcript_body


class ProofreadTests(unittest.TestCase):
    def test_verified_model_cache_survives_private_port_change(self):
        with tempfile.TemporaryDirectory(dir=ROOT/'work') as folder:
            args=('Dear friends thank you for joining us today.', 'http://127.0.0.1:1001/api/generate','qwen3:8b',[],900,1,8192,Path(folder))
            with patch('clean_transcript.generate',return_value=args[0]) as generate_call:
                first=clean_text(*args,model_digest='verified-model-a')
                changed=(args[0],'http://127.0.0.1:1002/api/generate',*args[2:])
                self.assertEqual(clean_text(*changed,model_digest='verified-model-a'),first)
                self.assertEqual(generate_call.call_count,1)
                clean_text(*changed,model_digest='verified-model-b')
                self.assertEqual(generate_call.call_count,2)

    def test_timeline_and_plain(self):
        self.assertEqual(transcript_body('1\n00:00:01,000 --> 00:00:02,000\n您好\n'), '您好')
        self.assertEqual(transcript_body('純文字\n'), '純文字\n')
        with self.assertRaises(ValueError):
            transcript_body('1\n00:00:01,000 --> 00:00:02,000\n您好\n\n壞掉區塊')

    def test_adaptive_split_and_cache(self):
        def fake(api, model, prompt, source, timeout, context):
            if len(source) > 150:
                raise OutputTruncatedError('length')
            return source
        with tempfile.TemporaryDirectory(dir=ROOT / 'work') as folder:
            args = ('甲' * 300, 'local', 'qwen3:8b', [], 900, 1, 8192, Path(folder))
            with patch('clean_transcript.generate', side_effect=fake) as call:
                result = clean_text(*args)
                self.assertEqual(call.call_count, 2)  # Identical second half reuses the first half.
            self.assertEqual(result.replace('\n', ''), '甲' * 300)
            with patch('clean_transcript.generate') as call:
                self.assertEqual(clean_text(*args), result)
                call.assert_not_called()

    def test_resume_after_network_failure(self):
        with tempfile.TemporaryDirectory(dir=ROOT / 'work') as folder:
            args = ('甲' * 130 + '\n' + '乙' * 130, 'local', 'qwen3:8b', [], 140, 1, 8192, Path(folder))
            with patch('clean_transcript.generate', side_effect=['甲' * 130, RuntimeError('offline')]):
                with self.assertRaises(RuntimeError):
                    clean_text(*args)
            with patch('clean_transcript.generate', return_value='乙' * 130) as call:
                self.assertIn('乙', clean_text(*args))
                self.assertEqual(call.call_count, 1)

    def test_qwen3_thinking_disabled(self):
        response = MagicMock()
        response.__enter__.return_value.read.return_value = b'{"response":"OK","done_reason":"stop"}'
        with patch('clean_transcript.urllib.request.urlopen', return_value=response) as call:
            generate('http://localhost/api/generate', 'qwen3:8b', 'prompt', 'source', 1, 8192)
            body = json.loads(call.call_args.args[0].data)
            self.assertIs(body['think'], False)
            self.assertEqual(body['options']['num_ctx'], 8192)


if __name__ == '__main__':
    unittest.main()
