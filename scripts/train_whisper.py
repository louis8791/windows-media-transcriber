"""Local-only bounded training experiment; never updates production config."""
from pathlib import Path
import json
import wave
import time
from whisper_learning import ROOT, validate, preflight, compare, save_new, sha, score_predictions, acceptable_score, ValidationProgress

def train(dataset, model_path, output, max_steps=200, lora=False, allow_weak_training=False, learning_rate=1e-5):
    data = json.loads(Path(dataset).read_text(encoding='utf-8-sig'))
    check = preflight(data, model_path, allow_weak_training)
    if not check['ready']:
        raise ValueError('；'.join(check['reasons']))
    if not 1 <= max_steps <= 200:
        raise ValueError('第一輪最多200步，不得無上限續訓')
    if max_steps < 20 or max_steps % 20:
        raise ValueError('訓練步數須為20的倍數，以便保存並驗證checkpoint')
    if not 0 < learning_rate <= 1e-3:
        raise ValueError('學習率須大於0且不超過0.001')
    output = Path(output).resolve()
    if output.exists() or not output.is_relative_to(ROOT):
        raise ValueError('實驗須寫入專案內新目錄')
    import numpy as np
    import torch
    from transformers import WhisperProcessor, WhisperForConditionalGeneration, Seq2SeqTrainer, Seq2SeqTrainingArguments, TrainerCallback
    from merge_whisper_chunks import atomic_write_text
    if not torch.cuda.is_available():
        raise ValueError('尚無已可用的 PyTorch GPU 訓練環境；沒有啟動CPU長時間訓練')
    torch.manual_seed(42)
    processor = WhisperProcessor.from_pretrained(str(model_path), local_files_only=True)
    model = WhisperForConditionalGeneration.from_pretrained(str(model_path), local_files_only=True,
            dtype=torch.float16 if lora else torch.float32, attn_implementation='eager')
    model.config.use_cache = False
    rows = validate(data, allow_weak_training=allow_weak_training)
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
    def predictions(phase, split='test'):
        was_training = model.training
        model.eval()
        result = {}
        with torch.no_grad():
            test_rows = [row for row in rows if row['split'] == split]
            for index, row in enumerate(test_rows, 1):
                if row['split'] == split:
                    features = torch.as_tensor(row['_features'], device='cuda', dtype=model.dtype).unsqueeze(0)
                    ids = model.generate(features, language={'en':'english','zh':'chinese'}[row['language']], task='transcribe', max_new_tokens=225, use_cache=True)
                    result[row['id']] = processor.batch_decode(ids, skip_special_tokens=True)[0]
                    if index % 10 == 0 or index == len(test_rows):
                        print(f'{phase}: {index}/{len(test_rows)}', flush=True)
        model.train(was_training)
        return result
    validation_rows = [row for row in rows if row['split'] == 'validation']
    validation_baseline = predictions('validation_baseline', 'validation')
    baseline_score = score_predictions(validation_rows, validation_baseline)
    progress = ValidationProgress(baseline_score)
    if lora:
        from peft import LoraConfig, get_peft_model
        model = get_peft_model(model, LoraConfig(r=8, lora_alpha=16, target_modules=['q_proj','v_proj'], lora_dropout=0.05, bias='none'))
        model.enable_input_require_grads()
    save_new(output/'run_manifest.json', {'base_model_files': {p.name:sha(p) for p in Path(model_path).iterdir() if p.is_file()}, 'base_model_path':str(model_path), 'training_method':'lora' if lora else 'full', 'learning_rate':learning_rate, 'trainable_parameters':sum(p.numel() for p in model.parameters() if p.requires_grad), 'max_steps':max_steps, 'step_boundary_time_budget_seconds':1800, 'seed':42, 'gpu':torch.cuda.get_device_name(0), 'torch_version':torch.__version__, 'checkpoint_selection':'generated_validation_error_rate', 'test_inference_after_checkpoint_selection':True, 'production_promoted':False})
    save_new(output/'validation_baseline.json', {'predictions':validation_baseline, 'score':baseline_score})
    save_new(output/'dataset_snapshot.json', snapshot)
    arguments = Seq2SeqTrainingArguments(output_dir=str(output/'checkpoints'), max_steps=max_steps, per_device_train_batch_size=1, per_device_eval_batch_size=1, gradient_accumulation_steps=4, learning_rate=learning_rate, gradient_checkpointing=True, gradient_checkpointing_kwargs={'use_reentrant':False}, fp16=lora, remove_unused_columns=False, label_names=['labels'], eval_strategy='steps', eval_steps=20, save_steps=20, logging_steps=10, load_best_model_at_end=True, metric_for_best_model='eval_generated_error_rate', greater_is_better=False, report_to=[], push_to_hub=False, save_total_limit=2, seed=42)
    started = time.monotonic()
    class Budget(TrainerCallback):
        def on_step_end(self, args, state, control, **kwargs):
            if time.monotonic()-started >= 1800:
                progress.reason = 'training_time_budget'
                control.should_training_stop=True
                control.should_evaluate=True
                control.should_save=True
            return control
        def on_evaluate(self, args, state, control, metrics=None, **kwargs):
            predicted = predictions(f'validation_step_{state.global_step}', 'validation')
            score = score_predictions(validation_rows, predicted)
            metrics['eval_generated_error_rate'] = score['error_rate']
            reason = progress.observe(score)
            save_new(output/f'validation_step_{state.global_step}.json', {'step':state.global_step, 'predictions':predicted, 'score':score, 'teacher_forced_loss':metrics.get('eval_loss'), 'stop_reason':reason})
            print(json.dumps({'step':state.global_step, 'validation_errors':score['errors'], 'baseline_errors':baseline_score['errors'], 'stop_reason':reason}), flush=True)
            if reason or time.monotonic()-started >= 1800:
                progress.reason = reason or 'training_time_budget'
                control.should_training_stop=True
                control.should_save=True
            return control
    trainer = Seq2SeqTrainer(model=model, args=arguments, train_dataset=prepared['train'], eval_dataset=prepared['validation'], data_collator=collate, callbacks=[Budget()])
    atomic_write_text(output/'status.json', json.dumps({'status':'running','production_promoted':False}))
    try:
        trainer.train()
        save_new(output/'training_summary.json', {'optimizer_steps':trainer.state.global_step, 'best_checkpoint':trainer.state.best_model_checkpoint, 'best_validation_error_rate':trainer.state.best_metric, 'stop_reason':progress.reason or 'max_steps', 'training_and_validation_seconds':time.monotonic()-started})
        selected_validation = predictions('selected_validation', 'validation')
        selected_score = score_predictions(validation_rows, selected_validation)
        validation_passed = acceptable_score(baseline_score, selected_score)
        save_new(output/'selected_validation.json', {'predictions':selected_validation, 'score':selected_score, 'accepted':validation_passed})
        # The final test is first decoded after checkpoint selection has ended.
        if lora:
            with model.disable_adapter():
                baseline = predictions('baseline')
        else:
            original = WhisperForConditionalGeneration.from_pretrained(str(model_path), local_files_only=True, dtype=model.dtype, attn_implementation='eager').to('cuda')
            trained = model
            model = original
            baseline = predictions('baseline')
            model = trained
            del original
            torch.cuda.empty_cache()
        save_new(output/'baseline.json', baseline)
        candidate = predictions('candidate')
        save_new(output/'candidate.json', candidate)
        comparison = compare(data, baseline, candidate, allow_weak_training)
        comparison['validation_accepted'] = validation_passed
        if not validation_passed:
            comparison['status'] = 'not_accepted'
        save_new(output/'comparison.json', comparison)
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
    parser.add_argument('--lora', action='store_true')
    parser.add_argument('--allow-weak-training', action='store_true')
    parser.add_argument('--learning-rate', type=float, default=1e-5)
    args = parser.parse_args()
    train(args.dataset, args.model, args.output, args.max_steps, args.lora, args.allow_weak_training, args.learning_rate)
