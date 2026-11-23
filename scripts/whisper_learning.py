"""Reviewed audio/text dataset and held-out evaluation for Whisper experiments."""
from __future__ import annotations
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import subprocess
import wave

ROOT = Path(__file__).resolve().parents[1]

def sha(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()

def save_new(path, data):
    path = Path(path).resolve()
    if not path.is_relative_to(ROOT):
        raise ValueError('教材與實驗必須放在正式專案內')
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('x', encoding='utf-8') as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2)

def validate(data, complete=True, allow_weak_training=False):
    records = data.get('records', [])
    if not records:
        raise ValueError('尚無教材')
    ids, groups, speakers, languages = set(), {}, {}, set()
    for row in records:
        if row['id'] in ids:
            raise ValueError('教材 id 重複')
        ids.add(row['id'])
        weak = (allow_weak_training and data.get('weak_supervision_authorized') is True
                and row.get('split') == 'train' and row.get('annotation_type') == 'owner_authorized_asr_candidate'
                and row.get('target_authorized_by') == 'Owner' and row.get('target_authorized_at'))
        if not weak and (row.get('approved') is not True or not row.get('verified_by', '').strip() or not row.get('verified_at', '').strip()):
            raise ValueError('教材未經人工聽校，不可訓練')
        if not row.get('text', '').strip() or re.search(r'待確認|unclear|原文不清|/no_think', row['text'], re.I):
            raise ValueError('教材仍有不確定內容')
        if row.get('language') not in {'en', 'zh'} or row.get('split') not in {'train', 'validation', 'test'}:
            raise ValueError('語言或資料分組無效')
        languages.add(row['language'])
        source, clip = Path(row['source_path']), Path(row['audio_path'])
        if not clip.resolve().is_relative_to(ROOT) or sha(source) != row['source_sha256'] or sha(clip) != row['audio_sha256']:
            raise ValueError('來源或片段 hash 改變')
        with wave.open(str(clip)) as audio:
            if audio.getframerate() != 16000 or audio.getnchannels() != 1 or audio.getsampwidth() != 2:
                raise ValueError('片段須是 16kHz mono PCM16 WAV')
            seconds = audio.getnframes() / 16000
        if not 0 < seconds <= 30 or not 0 <= row['start_ms'] < row['end_ms'] or row['end_ms'] - row['start_ms'] > 30000:
            raise ValueError('教材片段須不超過30秒')
        if abs(seconds * 1000 - (row['end_ms'] - row['start_ms'])) > 100:
            raise ValueError('教材片段與時間範圍不符')
        group = groups.setdefault(row['source_sha256'], row['split'])
        if group != row['split']:
            raise ValueError('同一錄音不可跨訓練、驗證與考試，避免洩漏')
        if row.get('speaker_id'):
            previous = speakers.setdefault(row['speaker_id'], row['split'])
            if previous != row['split']:
                raise ValueError('同一講者不可跨訓練、驗證與考試')
    if complete and {r['split'] for r in records} != {'train', 'validation', 'test'}:
        raise ValueError('缺少獨立錄音的 train／validation／test 教材')
    if complete:
        for language in languages:
            if not any(r['split'] == 'test' and r['language'] == language for r in records):
                raise ValueError('每種教材語言都須有未參與訓練的考試音檔')
    return records

def draft(source, srt, output, config):
    from recheck_audio import parse_srt_strict
    source, output = Path(source).resolve(), Path(output).resolve()
    if output.exists() or not output.is_relative_to(ROOT):
        raise ValueError('請選專案內尚不存在的教材目錄')
    cues = parse_srt_strict(Path(srt).read_text(encoding='utf-8-sig'))
    source_hash = sha(source)
    output.mkdir(parents=True)
    records, excluded = [], []
    for i, cue in enumerate(cues):
        if cue['end_ms'] - cue['start_ms'] > 30000:
            excluded.append({'cue': i + 1, 'reason': '超過30秒，須人工對齊再切段'})
            continue
        clip = output / f'clip_{i:05d}.wav'
        subprocess.run([config['ffmpeg_path'], '-hide_banner', '-loglevel', 'error', '-n', '-ss', str(cue['start_ms']/1000), '-t', str((cue['end_ms']-cue['start_ms'])/1000), '-i', str(source), '-ar', '16000', '-ac', '1', '-c:a', 'pcm_s16le', str(clip)], check=True, timeout=120, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        records.append({'id': f'{source_hash[:12]}_{i:05d}', 'source_path': str(source), 'source_sha256': source_hash, 'audio_path': str(clip), 'audio_sha256': sha(clip), 'start_ms': cue['start_ms'], 'end_ms': cue['end_ms'], 'text': cue['text'], 'language': config.get('language', 'en'), 'split': 'train', 'approved': False, 'verified_by': '', 'verified_at': ''})
    data = {'schema_version': 1, 'records': records, 'excluded': excluded, 'candidate_only': True}
    save_new(output/'dataset.json', data)
    if sha(source) != source_hash:
        raise ValueError('來源已改變')
    return output/'dataset.json'

def tokens(text):
    return re.findall(r"[a-z0-9]+(?:'[a-z]+)*|[\u3400-\u9fff]", text.casefold())

def errors(reference, hypothesis):
    a, b = tokens(reference), tokens(hypothesis)
    previous = list(range(len(b)+1))
    for i, word in enumerate(a, 1):
        current = [i]
        for j, other in enumerate(b, 1):
            current.append(min(previous[j]+1, current[j-1]+1, previous[j-1]+(word != other)))
        previous = current
    return previous[-1], len(a)

def score_predictions(records, predictions):
    """Score one frozen split, including omissions and speaker regressions."""
    from inspect_whisper_error_changes import edits
    if set(predictions) != {r['id'] for r in records}:
        raise ValueError('評分結果 id 必須完整對應指定教材')
    totals = {'errors': 0, 'reference_tokens': 0, 'substitutions': 0, 'deletions': 0, 'insertions': 0}
    speakers = {}
    languages = {}
    for row in records:
        count, length = errors(row['text'], predictions[row['id']])
        if not length:
            raise ValueError('答案沒有可評分文字')
        totals['errors'] += count
        totals['reference_tokens'] += length
        for key, value in edits(row['text'], predictions[row['id']]).items():
            totals[key] += value
        for groups, ident in ((speakers, row.get('speaker_id', 'unspecified')), (languages, row['language'])):
            group = groups.setdefault(ident, {'errors': 0, 'reference_tokens': 0})
            group['errors'] += count
            group['reference_tokens'] += length
    totals['error_rate'] = totals['errors']/totals['reference_tokens']
    return {**totals, 'speakers': speakers, 'languages': languages}

def acceptable_score(baseline, candidate):
    return (candidate['errors'] < baseline['errors']
            and candidate['deletions'] <= baseline['deletions']
            and candidate['insertions'] <= baseline['insertions']
            and all(candidate[group][key]['errors'] <= value['errors']
                    for group in ('speakers', 'languages') for key, value in baseline[group].items()))

class ValidationProgress:
    """Stop after two regressions or three checks without beating the best WER."""
    def __init__(self, baseline):
        self.baseline = baseline
        self.best = baseline['error_rate']
        self.regressions = self.stale = 0
        self.reason = None

    def observe(self, candidate):
        self.regressions = self.regressions + 1 if candidate['errors'] > self.baseline['errors'] else 0
        if candidate['error_rate'] < self.best:
            self.best = candidate['error_rate']
            self.stale = 0
        else:
            self.stale += 1
        if self.regressions >= 2:
            self.reason = 'two_consecutive_validation_regressions'
        elif self.stale >= 3:
            self.reason = 'three_checks_without_validation_improvement'
        return self.reason

def compare(data, baseline, candidate, allow_weak_training=False):
    records = [r for r in validate(data, allow_weak_training=allow_weak_training) if r['split'] == 'test']
    expected = {r['id'] for r in records}
    if set(baseline) != expected or set(candidate) != expected:
        raise ValueError('考試結果 id 必須完整對應同一組未見教材')
    rows = []
    for row in records:
        old, n = errors(row['text'], baseline[row['id']])
        new, _ = errors(row['text'], candidate[row['id']])
        if not n:
            raise ValueError('考試答案沒有可評分文字')
        rows.append({'id': row['id'], 'language': row['language'], 'baseline_errors': old, 'candidate_errors': new, 'reference_tokens': n})
    total = sum(r['reference_tokens'] for r in rows)
    old = sum(r['baseline_errors'] for r in rows)
    new = sum(r['candidate_errors'] for r in rows)
    old_score = score_predictions(records, baseline)
    new_score = score_predictions(records, candidate)
    passed = acceptable_score(old_score, new_score)
    return {'status': 'candidate_improved' if passed else 'not_accepted', 'baseline_error_rate': old/total, 'candidate_error_rate': new/total, 'baseline_score': old_score, 'candidate_score': new_score, 'normalization': 'casefold English words and individual Chinese characters; punctuation ignored', 'rows': rows, 'human_names_numbers_negations_review_required': True, 'production_promoted': False}

def preflight(data, model_path, allow_weak_training=False):
    reasons = []
    try:
        rows = validate(data, allow_weak_training=allow_weak_training)
    except (ValueError, KeyError, OSError, wave.Error) as error:
        rows = []; reasons.append(str(error))
    model = Path(model_path).resolve()
    if not model.is_relative_to(ROOT) or not (model/'config.json').is_file() or not any(model.glob('*.safetensors')):
        reasons.append('缺少專案內 Hugging Face 訓練權重；現有 ggml 推論檔不能直接微調')
    else:
        identity = json.loads((model/'config.json').read_text(encoding='utf-8'))
        if identity.get('model_type') != 'whisper' or identity.get('d_model') != 1280 or identity.get('num_mel_bins') != 128 or identity.get('decoder_layers') != 32:
            reasons.append('本輪基準為 Whisper large-v3，不可默默換小模型訓練')
    if importlib.util.find_spec('torch') is not None:
        import torch
        if not torch.cuda.is_available():
            reasons.append('PyTorch GPU 訓練環境尚不可用；顯示卡存在不代表能訓練')
    for name in ('torch', 'transformers', 'accelerate', 'numpy'):
        if importlib.util.find_spec(name) is None:
            reasons.append('缺少訓練依賴：'+name)
    return {'ready': not reasons, 'reasons': reasons, 'approved_records': sum(r.get('approved') is True for r in rows),
            'weak_training_records': sum(r.get('annotation_type') == 'owner_authorized_asr_candidate' for r in rows),
            'training_started': False, 'production_model_changed': False}

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['draft', 'validate', 'preflight', 'compare'])
    parser.add_argument('--dataset', type=Path)
    parser.add_argument('--source', type=Path)
    parser.add_argument('--srt', type=Path)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--model', type=Path, default=ROOT/'models/whisper_training/base')
    parser.add_argument('--baseline', type=Path)
    parser.add_argument('--candidate', type=Path)
    args = parser.parse_args()
    if args.action == 'draft':
        result = str(draft(args.source, args.srt, args.output, json.loads((ROOT/'config.local.json').read_text(encoding='utf-8-sig'))))
    else:
        data = json.loads(args.dataset.read_text(encoding='utf-8-sig'))
        if args.action == 'preflight': result = preflight(data, args.model)
        elif args.action == 'compare': result = compare(data, json.loads(args.baseline.read_text()), json.loads(args.candidate.read_text()))
        else: result = {'approved_records': len(validate(data))}
        if args.output: save_new(args.output, result)
    print(json.dumps(result, ensure_ascii=False, indent=2))

if __name__ == '__main__':
    main()
