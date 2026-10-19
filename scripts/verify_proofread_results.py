"""Read-only verification of the two owner-requested real proofreading runs."""
import difflib
import hashlib
import json
from pathlib import Path
from proofread_only import transcript_body
from normalize_text import atomic_write_text

ROOT = Path(__file__).resolve().parents[1]
config = json.loads((ROOT / 'config.local.json').read_text(encoding='utf-8-sig'))
folder = Path(config['desktop_output_dir'])
results = []
for name in (
    'synthetic_media_case_001',
    'synthetic_media_case_002',
):
    record = json.loads((folder / (name + '.校稿紀錄.json')).read_text(encoding='utf-8'))
    source = Path(record['source'])
    output = Path(record['destination'])
    before = transcript_body(source.read_text(encoding='utf-8-sig'))
    after = output.read_text(encoding='utf-8')
    assert record['status'] == 'completed'
    assert record['model'] == 'qwen3:8b'
    assert hashlib.sha256(source.read_bytes()).hexdigest() == record['source_sha256']
    assert hashlib.sha256(output.read_bytes()).hexdigest() == record['output_sha256']
    record.update(original_unchanged=True, input_chars=len(before), output_chars=len(after),
                  text_changed=before.strip() != after.strip(),
                  changed_blocks=sum(tag != 'equal' for tag, *_ in difflib.SequenceMatcher(None, before, after).get_opcodes()))
    results.append(record)
atomic_write_text(ROOT / 'test_artifacts/qwen8b_20260923/report.json', json.dumps(results, ensure_ascii=False, indent=2))
print(json.dumps(results, ensure_ascii=False, indent=2))
