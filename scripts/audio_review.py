"""Read-only transcript listening and Silero checks of uncovered audio."""
from __future__ import annotations
import argparse
import ctypes as c
import hashlib
import json
import os
from pathlib import Path
import subprocess
import re
from datetime import datetime, timezone
from contextlib import contextmanager
import uuid

from recheck_audio import parse_srt_strict
from merge_whisper_chunks import atomic_write_text

ROOT = Path(__file__).resolve().parents[1]
MEDIA = {'.mp3', '.wav', '.m4a', '.mp4', '.aac', '.flac', '.mov', '.mkv', '.ogg', '.wma', '.webm', '.avi', '.wmv', '.ts', '.mts', '.m2ts', '.3gp'}


def sha(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


REVIEW_DECISIONS = ('尚未核對', '確實漏句', '掌聲／音樂', '沒有漏句', '仍不確定')
PLAYBACK_CONTEXTS = {'僅所選段': 0, '前後0.4秒': 400, '前後3秒': 3000, '前後5秒': 5000}


def playback_bounds(row, duration_ms, context):
    if context not in PLAYBACK_CONTEXTS or type(duration_ms) is not int or duration_ms <= 0:
        raise ValueError('播放範圍或音訊長度無效。')
    padding = PLAYBACK_CONTEXTS[context]
    a, b = max(0, row['start_ms']-padding), min(duration_ms, row['end_ms']+padding)
    if not 0 <= a < b or row['start_ms'] >= duration_ms:
        raise ValueError('這一段超出原音長度，請確認選到對應的原音。')
    return a, b


def decision_file(hashes, row):
    if set(hashes) != {'media', 'transcript'} or any(not isinstance(value, str) or not re.fullmatch(r'[0-9a-f]{64}', value) for value in hashes.values()):
        raise ValueError('核對紀錄缺少可靠來源指紋。')
    a, b = row['start_ms'], row['end_ms']
    if row['kind'] != 'possible_omission' or type(a) is not int or type(b) is not int or not 0 <= a < b:
        raise ValueError('只可標記有效漏句疑點。')
    key = hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest()
    return ROOT/'work/audio_review/decisions'/key/f'{a}_{b}.json'


def read_decision(hashes, row):
    path = decision_file(hashes, row)
    if not path.exists():
        return {'decision': '尚未核對', 'note': ''}
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
        if (data['source_hashes'] != hashes or data['start_ms'] != row['start_ms'] or data['end_ms'] != row['end_ms']
                or data['decision'] not in REVIEW_DECISIONS or not isinstance(data['note'], str)):
            raise ValueError()
        return data
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise ValueError('人工核對紀錄無效，已保留原檔；請查看專案工作資料夾。') from error


def write_decision(hashes, row, decision, note):
    if decision not in REVIEW_DECISIONS or not isinstance(note, str) or len(note) > 2000:
        raise ValueError('核對選項無效，或備註超過2000字。')
    # Validate an existing record before updating; never replace unreadable human work.
    read_decision(hashes, row)
    data = {'schema_version': 1, 'source_hashes': hashes, 'start_ms': row['start_ms'], 'end_ms': row['end_ms'],
            'decision': decision, 'note': note, 'reviewed_by': 'Owner via GUI',
            'saved_at': datetime.now(timezone.utc).isoformat()}
    path = decision_file(hashes, row)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2))
    return data


def draft_file(hashes, row):
    path = decision_file(hashes, row)
    return path.parent.parent.parent/'drafts'/path.parent.name/path.name


def decision_digest(mark):
    return hashlib.sha256(json.dumps([mark['decision'], mark['note']], ensure_ascii=False).encode()).hexdigest()


def raw_draft(hashes, row):
    path = draft_file(hashes, row)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
        if (data['kind'] != 'unconfirmed_draft' or data['source_hashes'] != hashes
                or data['start_ms'] != row['start_ms'] or data['end_ms'] != row['end_ms']
                or data['decision'] not in REVIEW_DECISIONS or not isinstance(data['note'], str)
                or not re.fullmatch(r'[0-9a-f]{64}', data['base_digest'])):
            raise ValueError()
        return data
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise ValueError('草稿紀錄無效，已保留原檔；請先處理後再保存。') from error


def read_draft(hashes, row):
    data = raw_draft(hashes, row)
    if data and data['base_digest'] == decision_digest(read_decision(hashes, row)):
        return data
    return None


def write_draft(hashes, row, decision, note, base):
    if decision not in REVIEW_DECISIONS or not isinstance(note, str) or len(note) > 2000:
        raise ValueError('草稿選項無效，或備註超過2000字。')
    raw_draft(hashes, row)
    if decision_digest(read_decision(hashes, row)) != decision_digest(base):
        raise ValueError('正式核對已變更，請重新載入；原草稿保留。')
    data = {'kind': 'unconfirmed_draft', 'source_hashes': hashes, 'start_ms': row['start_ms'], 'end_ms': row['end_ms'],
            'decision': decision, 'note': note, 'base_digest': decision_digest(base),
            'saved_at': datetime.now(timezone.utc).isoformat()}
    path = draft_file(hashes, row)
    atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2))
    return data


def clear_draft(hashes, row):
    raw_draft(hashes, row)
    draft_file(hashes, row).unlink(missing_ok=True)


@contextmanager
def check_lock(folder):
    import msvcrt
    folder.mkdir(parents=True, exist_ok=True)
    with (folder/'writer.lock').open('a+b') as stream:
        if stream.seek(0, 2) == 0:
            stream.write(b'1');stream.flush()
        stream.seek(0)
        try:
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as error:
            raise ValueError('同一來源已有漏句檢查執行中，請等它完成或停止。') from error
        try:
            yield
        finally:
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)


def checkpoint_speech(path, hashes, signature, end, left, right):
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
        if (data['status'] != 'chunk_completed' or data['source_hashes'] != hashes or data['check_signature'] != signature
                or data['duration_ms'] != end or data['left_ms'] != left or data['right_ms'] != right):
            return None
        speech = data['speech']
        if not isinstance(speech, list) or any(not isinstance(item, list) or len(item) != 2
               or any(type(value) is not int for value in item) or not left <= item[0] < item[1] <= right for item in speech):
            return None
        return [tuple(item) for item in speech]
    except (OSError, ValueError, KeyError, TypeError):
        return None


def ffmpeg(config):
    path = Path(config.get('ffmpeg_path', config.get('ffmpeg_exe', '')))
    if not path.is_file():
        raise ValueError('找不到本專案的音訊工具，請檢查目前設定。')
    return str(path)


def run(command, **kwargs):
    result = subprocess.run(command, capture_output=True, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0), **kwargs)
    if result.returncode:
        raise ValueError('音訊工具失敗：' + result.stderr.decode('utf-8', errors='replace')[-1500:])
    return result.stdout


def duration(media, config):
    probe = str(Path(ffmpeg(config)).with_name('ffprobe.exe'))
    raw = run([probe, '-v', 'error', '-show_entries', 'format=duration', '-of', 'json', str(media)], timeout=30)
    import math
    seconds = float(json.loads(raw)['format']['duration'])
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError('音訊長度無效。')
    return round(seconds * 1000)


def uncovered(cues, end_ms, minimum=1000):
    end = 0
    gaps = []
    for cue in sorted(cues, key=lambda row: row['start_ms']):
        a, b = cue['start_ms'], cue['end_ms']
        if not 0 <= a < b <= end_ms + 500:
            raise ValueError('字幕時間超出音檔，請確認選到對應的原音與字幕。')
        a = min(a, end_ms)
        if a - end >= minimum:
            gaps.append((end, a))
        end = max(end, min(b, end_ms))
    if end_ms - end >= minimum:
        gaps.append((end, end_ms))
    return gaps


def speech_without_text(gaps, speech, minimum=320):
    """Intersect detected speech with exact gaps; never extend into an existing cue."""
    findings = []
    speech = sorted(speech)
    position = 0
    for a, b in sorted(gaps):
        intervals = []
        while position < len(speech) and speech[position][1] <= a:
            position += 1
        index = position
        while index < len(speech) and speech[index][0] < b:
            start, end = speech[index]
            index += 1
            start, end = max(a, start), min(b, end)
            if end <= start:
                continue
            if intervals and start <= intervals[-1][1] + 200:
                intervals[-1][1] = max(end, intervals[-1][1])
            else:
                intervals.append([start, end])
        for start, end in intervals:
            if end - start >= minimum:
                findings.append({'start_ms': start, 'end_ms': end,
                                 'text': '疑似有語音但沒有字幕，請聽原音核對。', 'kind': 'possible_omission'})
    return findings


def matching_media(transcript, config):
    """Only exact relative paths; renamed/ambiguous sources require explicit selection."""
    transcript = Path(transcript).resolve()
    roots = [Path(config['desktop_output_dir']).resolve()]
    if config.get('workspace_dir'):
        roots.append((Path(config['workspace_dir'])/'output_final').resolve())
    output = next((folder for folder in roots if transcript.is_relative_to(folder)), None)
    if output is None:
        return None
    base = transcript.relative_to(output).with_suffix('')
    candidates = [p for p in Path(config['desktop_input_dir']).rglob('*')
                  if p.is_file() and p.suffix.lower() in MEDIA
                  and p.relative_to(Path(config['desktop_input_dir'])).with_suffix('') == base]
    return candidates[0] if len(candidates) == 1 else None


def matching_subtitle(media, config):
    base = Path(media).resolve().relative_to(Path(config['desktop_input_dir']).resolve()).with_suffix('')
    for folder in (Path(config['workspace_dir'])/'output_final', Path(config['desktop_output_dir'])):
        for extension in ('.srt', '.txt'):
            path = folder / (str(base) + extension)
            if path.is_file():
                try:
                    if parse_srt_strict(path.read_text(encoding='utf-8-sig')):
                        return path
                except ValueError:
                    pass
    return None


def matching_timed_transcript(plain, config):
    """Require all text (including punctuation) and a unique source/timing match."""
    text = ' '.join(Path(plain).read_text(encoding='utf-8-sig').split())
    if not text:
        return None
    folders = [Path(config['workspace_dir'])/'output_final', Path(config['desktop_output_dir'])]
    matches = {}
    for folder in folders:
        for path in sorted(folder.rglob('*')):
            if not path.is_file() or path.suffix.lower() not in {'.srt', '.txt'}:
                continue
            try:
                cues = parse_srt_strict(path.read_text(encoding='utf-8-sig'))
            except (ValueError, OSError):
                continue
            if ' '.join(' '.join(row['text'].split()) for row in cues) != text:
                continue
            media = matching_media(path, config)
            key = (str(media.resolve()) if media else str(path.resolve()), json.dumps(cues, sort_keys=True))
            matches.setdefault(key, path)
    return next(iter(matches.values())) if len(matches) == 1 else None


def vad_assets(config):
    model = Path(config['vad_model_path']).resolve()
    manifest = json.loads((ROOT/'config/model-manifest.json').read_text(encoding='utf-8-sig'))
    expected = next(row['sha256'] for row in manifest['models'] if row['role'] == 'voice_activity_detection')
    if not model.is_relative_to(ROOT) or sha(model) != expected:
        raise ValueError('語音活動模型來源或hash不符，停止檢查。')
    folder = Path(config['whisper_cli_path']).parent.resolve()
    if not folder.is_relative_to(ROOT):
        raise ValueError('語音工具必須在本專案內。')
    return model, folder


def check_signature(config):
    model, folder = vad_assets(config)
    return {'algorithm': 'audio_review_context_v1', 'minimum_gap_ms': 1000, 'vad_threshold': .05,
            'runtime_hashes': {str(p.relative_to(ROOT)): sha(p) for p in
                               [model, *native_runtime_files(folder)]},
            'ffmpeg_sha256': sha(ffmpeg(config))}


def native_runtime_files(folder):
    folder=Path(folder)
    base=[folder/name for name in ('whisper.dll','ggml-base.dll','ggml.dll')]
    cpu=sorted(folder.glob('ggml-cpu*.dll'))
    if any(not path.is_file() for path in base) or not cpu:
        raise ValueError('Whisper執行元件缺少。')
    return base+cpu


def cached_report(path, before, signature):
    try:
        report = json.loads(path.read_text(encoding='utf-8'))
        if (report.get('status') != 'completed' or report.get('source_hashes') != before
                or report.get('check_signature') != signature or report.get('owner_content_review_required') is not True):
            return None
        end = report['duration_ms']
        if type(end) is not int or end <= 0 or type(report['gap_count']) is not int or not isinstance(report['findings'], list):
            return None
        for row in report['findings']:
            if (type(row['start_ms']) is not int or type(row['end_ms']) is not int
                    or not 0 <= row['start_ms'] < row['end_ms'] <= end
                    or row['kind'] != 'possible_omission' or not isinstance(row['text'], str)):
                return None
        return report
    except (OSError, ValueError, KeyError, TypeError):
        return None


def make_clip(media, start_ms, end_ms, speed, config, target):
    if speed not in (1.0, 0.75, 0.5) or not 0 <= start_ms < end_ms:
        raise ValueError('播放範圍或速度無效。')
    target = Path(target).resolve()
    if not target.is_relative_to(ROOT/'work/audio_review') or target.exists():
        raise ValueError('播放暫存位置無效。')
    target.parent.mkdir(parents=True, exist_ok=True)
    run([ffmpeg(config), '-nostdin', '-v', 'error', '-n', '-ss', str(start_ms/1000),
         '-t', str((end_ms-start_ms)/1000), '-i', str(media), '-vn', '-af', f'atempo={speed}',
         '-ac', '1', '-ar', '16000', '-c:a', 'pcm_s16le', str(target)], timeout=120)
    return target


class ContextParams(c.Structure):
    _fields_ = [('n_threads', c.c_int), ('use_gpu', c.c_bool), ('gpu_device', c.c_int)]


class Silero:
    def __init__(self, config):
        model, folder = vad_assets(config)
        self.dll_dir = os.add_dll_directory(str(folder))
        backend=c.CDLL(str(folder/'ggml.dll'))
        backend.ggml_backend_load_all_from_path.argtypes=[c.c_char_p]
        backend.ggml_backend_load_all_from_path.restype=None
        backend.ggml_backend_load_all_from_path(folder.relative_to(ROOT).as_posix().encode('ascii'))
        self.dll = d = c.CDLL(str(folder/'whisper.dll'))
        d.whisper_vad_default_context_params.restype = ContextParams
        d.whisper_vad_init_from_file_with_params.argtypes = [c.c_char_p, ContextParams]
        d.whisper_vad_init_from_file_with_params.restype = c.c_void_p
        d.whisper_vad_detect_speech.argtypes = [c.c_void_p, c.POINTER(c.c_float), c.c_int]
        d.whisper_vad_detect_speech.restype = c.c_bool
        d.whisper_vad_n_probs.argtypes = [c.c_void_p]
        d.whisper_vad_n_probs.restype = c.c_int
        d.whisper_vad_probs.argtypes = [c.c_void_p]
        d.whisper_vad_probs.restype = c.POINTER(c.c_float)
        d.whisper_vad_free.argtypes = [c.c_void_p]
        params = d.whisper_vad_default_context_params()
        params.use_gpu, params.n_threads = False, 4
        # Native paths are ASCII relative to ROOT; the child worker sets its cwd there.
        self.ctx = d.whisper_vad_init_from_file_with_params(model.relative_to(ROOT).as_posix().encode('ascii'), params)
        if not self.ctx:
            raise ValueError('語音活動模型載入失敗。')

    def detect(self, pcm, offset):
        samples = (c.c_float * (len(pcm)//4)).from_buffer_copy(pcm)
        if not samples or not self.dll.whisper_vad_detect_speech(self.ctx, samples, len(samples)):
            raise ValueError('語音活動檢查失敗。')
        n = self.dll.whisper_vad_n_probs(self.ctx)
        probs = self.dll.whisper_vad_probs(self.ctx)
        # This pinned Silero runtime emits one probability per 512 samples at 16kHz.
        if not probs or n != (len(samples) + 511)//512:
            raise ValueError('語音活動模型的時間格數不符，停止檢查。')
        return [(offset+i*32, offset+min((i+1)*32, round(len(samples)/16)))
                for i in range(n) if probs[i] >= 0.05]

    def close(self):
        if self.ctx:
            self.dll.whisper_vad_free(self.ctx)
            self.ctx = None
        self.dll_dir.close()


def check(media, transcript, config, output, force=False):
    output = Path(output).resolve()
    if not output.is_relative_to(ROOT/'work/audio_review') or output.exists():
        raise ValueError('檢查報告位置無效。')
    print('正在核對來源與工具，確認能否沿用上次結果。', flush=True)
    before = {'media': sha(media), 'transcript': sha(transcript)}
    signature = check_signature(config)
    key = hashlib.sha256(json.dumps([before, signature], sort_keys=True).encode()).hexdigest()
    folder = ROOT/'work/audio_review/checkpoints'/key
    with check_lock(folder):
        return check_locked(media, transcript, config, output, force, before, signature, folder, key)


def check_locked(media, transcript, config, output, force, before, signature, folder, key):
    state_path = folder/'state.json'
    try:
        state = json.loads(state_path.read_text(encoding='utf-8'))
        if not isinstance(state, dict) or not re.fullmatch(r'[0-9a-f]{32}', state.get('generation', '')):
            state = {}
    except (OSError, ValueError, TypeError):
        state = {}
    cache = ROOT/'work/audio_review/cache'/f'{key}.json'
    active = state.get('status') == 'running' and state.get('source_hashes') == before and state.get('check_signature') == signature
    previous = None if force or active else cached_report(cache, before, signature)
    if previous is not None:
        if before != {'media': sha(media), 'transcript': sha(transcript)}:
            raise ValueError('核對期間來源改變，請重新載入。')
        report = dict(previous, media=str(Path(media).resolve()), transcript=str(Path(transcript).resolve()), reused=True)
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open('x', encoding='utf-8') as stream:
            json.dump(report, stream, ensure_ascii=False, indent=2)
        print('來源未變，已沿用先前完成的檢查結果。', flush=True)
        return report
    cues = parse_srt_strict(Path(transcript).read_text(encoding='utf-8-sig'))
    end = duration(media, config)
    gaps = uncovered(cues, end)
    generation = state['generation'] if active and not force and state.get('duration_ms') == end else uuid.uuid4().hex
    state = {'status': 'running', 'generation': generation, 'source_hashes': before, 'check_signature': signature, 'duration_ms': end}
    atomic_write_text(state_path, json.dumps(state, ensure_ascii=False))
    chunk_dir = folder/generation
    chunk_dir.mkdir(exist_ok=True)
    speech = []
    resumed, checked = 0, 0
    vad = None
    source_stats = tuple((Path(path).stat().st_size, Path(path).stat().st_mtime_ns) for path in (media, transcript))
    try:
        # Detect with surrounding audio, rather than resetting the model at each gap.
        # The applause-to-speech regression is missed when the gap is decoded alone.
        for start in range(0, end, 300000):
            right = min(end, start+300000)
            if not any(a < right and b > start for a, b in gaps):
                continue
            left = max(0, start-10000)
            checkpoint = chunk_dir/f'{start:012d}.json'
            saved = checkpoint_speech(checkpoint, before, signature, end, left, right)
            if saved is not None:
                speech.extend(saved);resumed += 1
                print(f'檢查音訊接續：已完成 {left/1000:.1f}–{right/1000:.1f} 秒，略過。', flush=True)
                continue
            print(f'檢查音訊區段：{left/1000:.1f}–{right/1000:.1f} 秒', flush=True)
            if vad is None:
                vad = Silero(config)
            pcm = run([ffmpeg(config), '-nostdin', '-v', 'error', '-ss', str(left/1000), '-i', str(media),
                       '-t', str((right-left)/1000), '-vn', '-ac', '1', '-ar', '16000', '-f', 'f32le', 'pipe:1'], timeout=120)
            detected = vad.detect(pcm, left)
            if source_stats != tuple((Path(path).stat().st_size, Path(path).stat().st_mtime_ns) for path in (media, transcript)):
                raise ValueError('檢查期間來源改變；請重新載入。')
            atomic_write_text(checkpoint, json.dumps({'status': 'chunk_completed', 'source_hashes': before,
                'check_signature': signature, 'duration_ms': end, 'left_ms': left, 'right_ms': right, 'speech': detected}))
            speech.extend(detected);checked += 1
    finally:
        if vad is not None:
            vad.close()
    if before != {'media': sha(media), 'transcript': sha(transcript)}:
        raise ValueError('檢查期間來源改變，結果不發布；請重新載入。')
    report = {'schema_version': 2, 'status': 'completed', 'reused': False, 'resumed_chunks': resumed, 'checked_chunks': checked, 'check_signature': signature, 'media': str(Path(media).resolve()),
              'transcript': str(Path(transcript).resolve()), 'source_hashes': before, 'duration_ms': end,
              'gap_count': len(gaps), 'minimum_gap_ms': 1000, 'vad_threshold': 0.05,
              'findings': speech_without_text(gaps, speech), 'owner_content_review_required': True,
              'limitations': '僅檢查至少1秒的無字幕區段；有字幕但少字、錯字，以及VAD漏偵測的語音仍須聽校。'}
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open('x', encoding='utf-8') as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
    cache.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(cache, json.dumps(report, ensure_ascii=False, indent=2))
    state['status'] = 'completed'
    atomic_write_text(state_path, json.dumps(state, ensure_ascii=False))
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('media', type=Path)
    parser.add_argument('transcript', type=Path)
    parser.add_argument('output', type=Path)
    parser.add_argument('--force', action='store_true')
    args = parser.parse_args()
    config = json.loads((ROOT/'config.local.json').read_text(encoding='utf-8-sig'))
    try:
        result = check(args.media, args.transcript, config, args.output, args.force)
        print(f'檢查完成：{len(result["findings"])}處待聽校。', flush=True)
    except Exception as error:
        print(f'檢查失敗：{error}', flush=True)
        raise SystemExit(1)
