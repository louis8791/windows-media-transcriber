"""Exact word edit decomposition, using the experiment's frozen tokenizer."""
import json
from whisper_learning import ROOT,tokens,errors

def edits(reference,hypothesis):
 a,b=tokens(reference),tokens(hypothesis);m=[[0]*(len(b)+1) for _ in range(len(a)+1)]
 for i in range(len(a)+1):m[i][0]=i
 for j in range(len(b)+1):m[0][j]=j
 for i in range(1,len(a)+1):
  for j in range(1,len(b)+1):m[i][j]=min(m[i-1][j]+1,m[i][j-1]+1,m[i-1][j-1]+(a[i-1]!=b[j-1]))
 counts={'substitutions':0,'deletions':0,'insertions':0};i,j=len(a),len(b)
 while i or j:
  if i and j and m[i][j]==m[i-1][j-1]+(a[i-1]!=b[j-1]):
   counts['substitutions']+=int(a[i-1]!=b[j-1]);i-=1;j-=1
  elif i and m[i][j]==m[i-1][j]+1:counts['deletions']+=1;i-=1
  else:counts['insertions']+=1;j-=1
 assert sum(counts.values())==errors(reference,hypothesis)[0]
 return counts

def main():
 run=ROOT/'work/whisper_learning/retrain_lr_run_20261004'
 data=json.loads((run/'dataset_snapshot.json').read_text(encoding='utf-8'));baseline=json.loads((run/'baseline.json').read_text());candidate=json.loads((run/'candidate.json').read_text())
 speakers={};totals={phase:{'substitutions':0,'deletions':0,'insertions':0} for phase in ('baseline','candidate')};changes=[]
 for row in data['records']:
  if row['split']!='test':continue
  old,n=errors(row['text'],baseline[row['id']]);new,_=errors(row['text'],candidate[row['id']])
  group=speakers.setdefault(row['speaker_id'],{'baseline_errors':0,'candidate_errors':0,'reference_tokens':0})
  group['baseline_errors']+=old;group['candidate_errors']+=new;group['reference_tokens']+=n
  for phase,predictions in (('baseline',baseline),('candidate',candidate)):
   for key,value in edits(row['text'],predictions[row['id']]).items():totals[phase][key]+=value
  if old!=new:changes.append({'id':row['id'],'baseline_errors':old,'candidate_errors':new,'reference':row['text'],'baseline':baseline[row['id']],'candidate':candidate[row['id']]})
 result={'speakers':speakers,'edit_types':totals,'changed_error_count_clips':len(changes),'changes':sorted(changes,key=lambda row:row['candidate_errors']-row['baseline_errors'],reverse=True),'every_speaker_non_regression':all(r['candidate_errors']<=r['baseline_errors'] for r in speakers.values())}
 (run/'error_analysis.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
 print(json.dumps({k:v for k,v in result.items() if k!='changes'},ensure_ascii=False),flush=True)
if __name__=='__main__':main()
