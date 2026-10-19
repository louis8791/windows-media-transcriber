"""Local-only bounded training experiment; never updates production config."""
from pathlib import Path
import json
import wave
import time
from whisper_learning import ROOT, validate, preflight, compare, save_new, sha

def train(dataset, model_path, output, max_steps=200):
    data = json.loads(Path(dataset).read_text(encoding='utf-8-sig'))
    check = preflight(data, model_path)
    if not check['ready']:
        raise ValueError('；'.join(check['reasons']))
    if not 1 <= max_steps <= 200:
        raise ValueError('第一輪最多200步，不得無上限續訓')
    output = Path(output).resolve()
    if output.exists() or not output.is_relative_to(ROOT):
        raise ValueError('實驗須寫入專案內新目錄')
    import numpy as np
    import torch
    from transformers import WhisperProcessor, WhisperForConditionalGeneration, Seq2SeqTrainer, Seq2SeqTrainingArguments, TrainerCallback
    from normalize_text import atomic_write_text
    if not torch.cuda.is_available():
        raise ValueError('尚無已可用的 PyTorch GPU 訓練環境；沒有啟動CPU長時間訓練')
    torch.manual_seed(42)
    processor = WhisperProcessor.from_pretrained(str(model_path), local_files_only=True)
    model = WhisperForConditionalGeneration.from_pretrained(str(model_path), local_files_only=True)
    model.config.use_cache = False
    rows = validate(data)
    snapshot = json.loads(json.dumps(data))
    prepared = {'train': [], 'validation': []}
    for row in rows:
        with wave.open(row['audio_path']) as audio:
            samples = np.frombuffer(audio.readframes(audio.getnframes()), dtype='<i2').astype(np.float32)/32768
        row['_features'] = processor.feature_extractor(samples, sampling_rate=16000).input_features[0]
        if row['split'] != 'test':
            processor.tokenizer.set_prefix_tokens(language={'en':'english','zh':'chinese'}[row['language']], task='transcribe')
            prepared[row['split']].append({'input_features': row['_features'], 'labels': processor.tokenizer(row['text']).input_ids})
    def collate(features):
        batch = processor.feature_extractor.pad([{'input_features': f['input_features']} for f in features], return_tensors='pt')
        labels = processor.tokenizer.pad([{'input_ids': f['labels']} for f in features], return_tensors='pt')
        ids = labels['input_ids'].masked_fill(labels.attention_mask.ne(1), -100)
        if (ids[:,0] == model.config.decoder_start_token_id).all(): ids = ids[:,1:]
        batch['labels'] = ids
        return batch
    model.to('cuda')
    def predictions():
        model.eval()
        result = {}
        with torch.no_grad():
            for row in rows:
                if row['split'] == 'test':
                    features = torch.as_tensor(row['_features'], device='cuda').unsqueeze(0)
                    ids = model.generate(features, language={'en':'english','zh':'chinese'}[row['language']], task='transcribe', max_new_tokens=225)
                    result[row['id']] = processor.batch_decode(ids, skip_special_tokens=True)[0]
        return result
    baseline = predictions()
    save_new(output/'run_manifest.json', {'base_model_files': {p.name:sha(p) for p in Path(model_path).iterdir() if p.is_file()}, 'base_model_path':str(model_path), 'max_steps':max_steps, 'step_boundary_time_budget_seconds':1800, 'seed':42, 'gpu':torch.cuda.get_device_name(0), 'torch_version':torch.__version__, 'production_promoted':False})
    save_new(output/'baseline.json', baseline)
    save_new(output/'dataset_snapshot.json', snapshot)
    arguments = Seq2SeqTrainingArguments(output_dir=str(output/'checkpoints'), max_steps=max_steps, per_device_train_batch_size=1, per_device_eval_batch_size=1, gradient_accumulation_steps=4, learning_rate=1e-5, gradient_checkpointing=True, eval_strategy='steps', eval_steps=20, save_steps=20, logging_steps=10, load_best_model_at_end=True, metric_for_best_model='eval_loss', report_to=[], push_to_hub=False, save_total_limit=2, seed=42)
    started = time.monotonic()
    class Budget(TrainerCallback):
        def on_step_end(self, args, state, control, **kwargs):
            if time.monotonic()-started >= 1800:control.should_training_stop=True
            return control
    trainer = Seq2SeqTrainer(model=model, args=arguments, train_dataset=prepared['train'], eval_dataset=prepared['validation'], data_collator=collate, callbacks=[Budget()])
    atomic_write_text(output/'status.json', json.dumps({'status':'running','production_promoted':False}))
    try:
        trainer.train()
        candidate = predictions()
        save_new(output/'candidate.json', candidate)
        save_new(output/'comparison.json', compare(data, baseline, candidate))
        trainer.save_model(str(output/'candidate_model'))
        processor.save_pretrained(str(output/'candidate_model'))
        atomic_write_text(output/'status.json', json.dumps({'status':'completed_candidate','production_promoted':False,'human_review_required':True}))
    except Exception as error:
        atomic_write_text(output/'status.json', json.dumps({'status':'failed','error':str(error),'production_promoted':False}))
        raise

if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--dataset', type=Path, required=True)
    parser.add_argument('--model', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--max-steps', type=int, default=200)
    args = parser.parse_args()
    train(args.dataset, args.model, args.output, args.max_steps)
