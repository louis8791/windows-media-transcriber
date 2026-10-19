"""Proofread an existing transcript; never invoke FFmpeg or Whisper."""
import argparse
import hashlib
import json
import re
from pathlib import Path
import sys
import time
import urllib.request

from clean_transcript import clean_text, load_terms
from normalize_text import atomic_write_text
from quality_report import parse_cues

ROOT = Path(__file__).resolve().parents[1]


def transcript_body(text):
    if '-->' not in text:
        return text
    cues = parse_cues(text)
    blocks = re.split(r'\r?\n\s*\r?\n', text.strip())
    if not cues or len(cues) != len(blocks):
        raise ValueError('時間碼稿格式無法辨識，請選擇正式 TXT／SRT 或純文字稿。')
    return '\n'.join(cue['text'] for cue in cues)


def proofread(config=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('source', nargs='?', type=Path)
    args = parser.parse_args()
    source = args.source or Path(input('請把要校稿的 TXT／SRT 拖進這個視窗，按 Enter：\n').strip().strip('"'))
    source = source.resolve()
    if not source.is_file() or source.suffix.lower() not in ('.txt', '.srt'):
        raise ValueError('請選擇一份已完成的 TXT／SRT 文字檔，不是音檔。')
    if any(token in source.stem for token in ('轉錄中', '轉錄進度', '審查報告', '校稿進度')):
        raise ValueError('不能把部分稿或狀態報告當成完整逐字稿校稿。')
    destination = source.with_name(source.stem + '.cleaned.txt')
    if destination.exists():
        raise FileExistsError('已有校稿檔，為保留既有工作，本次沒有覆寫：' + str(destination))
    config = config or json.loads((ROOT / 'config.local.json').read_text(encoding='utf-8-sig'))
    api = config['ollama_api_url']
    model = config['ollama_model']
    tags_url = api.rsplit('/api/', 1)[0] + '/api/tags'
    with urllib.request.urlopen(tags_url, timeout=5) as response:
        tags = json.load(response)
    selected = next((m for m in tags['models'] if m['name'] == model), None)
    if selected is None:
        raise RuntimeError('指定模型尚未安裝：' + model)
    manifest = json.loads((ROOT / 'config/model-manifest.json').read_text(encoding='utf-8-sig'))
    expected = next(m for m in manifest['models'] if m['role'] == 'transcript_cleanup')
    if expected['name'] != model or expected['model_digest'] != selected['digest']:
        raise RuntimeError('模型版本與本專案驗證紀錄不符，未開始校稿。')
    raw = source.read_bytes()
    text = transcript_body(raw.decode('utf-8-sig'))
    source_hash = hashlib.sha256(raw).hexdigest()
    cache_key = hashlib.sha256((selected['digest'] + ':' + source_hash).encode('ascii')).hexdigest()
    cache = ROOT / 'work/proofread_cache' / cache_key
    status_path = source.with_name(source.stem + '.校稿進度.txt')
    evidence_path = source.with_name(source.stem + '.校稿紀錄.json')
    started = time.monotonic()
    record = dict(model=model, model_digest=selected['digest'], source=str(source),
                  source_sha256=source_hash, destination=str(destination), status='running',
                  whisper_invoked=False, think=False, owner_content_review_required=True)

    def progress(done, total):
        record.update(completed_chunks=done, total_chunks=total)
        atomic_write_text(status_path, f'Qwen 單獨校稿中（不重轉音檔）\n模型：{model}\n檔案：{source.name}\n已完成：{done} / {total} 段\n原始時間碼稿保持不變。\n')

    try:
        result = clean_text(text, api, model, load_terms(ROOT / 'data/custom_terms.txt'),
                            int(config.get('ollama_chunk_chars', 900)), 600,
                            int(config.get('ollama_context_size', 8192)), cache, progress,
                            model_digest=selected['digest'])
        if hashlib.sha256(source.read_bytes()).hexdigest() != source_hash:
            raise RuntimeError('校稿期間原稿已變更，沒有交付混用版本的結果。')
        if destination.exists():
            raise FileExistsError('校稿期間已有其他程序產生結果，未覆寫：' + str(destination))
        atomic_write_text(destination, result)
        record.update(status='completed', output_sha256=hashlib.sha256(destination.read_bytes()).hexdigest())
        atomic_write_text(status_path, f'校稿完成：{model}\n結果：{destination.name}\n此為無時間碼校稿候選，請對照原稿／原音驗收。\n')
        print('校稿完成：' + str(destination), flush=True)
    except Exception as exc:
        record.update(status='failed', error=str(exc))
        atomic_write_text(status_path, f'校稿未完成：{exc}\n已完成校稿段落有保存，重開同一原稿可續跑；沒有覆寫原稿。\n')
        raise
    finally:
        record['elapsed_seconds'] = round(time.monotonic() - started, 2)
        atomic_write_text(evidence_path, json.dumps(record, ensure_ascii=False, indent=2))


def main():
    from run_transcribe import Pipeline
    pipeline=Pipeline(json.loads((ROOT/'config.local.json').read_text(encoding='utf-8-sig')))
    try:
        pipeline.ensure_cleanup()
        return proofread(pipeline.config)
    finally:
        pipeline.close()


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print('無法完成校稿：' + str(error), flush=True)
        sys.exit(1)
