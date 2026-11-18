"""Recoverable <=50-task file batches. Never invokes or simulates a model.

A batch is an execution-management boundary, not a single model prompt. The
Agent must evaluate one task at a time between explicit start/finish calls.
"""
from __future__ import annotations
import hashlib,json,sqlite3
from collections import Counter
from contextlib import contextmanager
from pathlib import Path
from core.execution_logger import ExecutionLogger
from core.result_parser import parse_result,ResultError,decode_json
from core.task_manager import TaskManager


class BatchExecutionError(ValueError):pass


def _sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()


def _new_json(path,value):
 path.parent.mkdir(parents=True,exist_ok=True)
 with path.open('x',encoding='utf-8') as stream:json.dump(value,stream,ensure_ascii=False,indent=2)


class BatchExecution:
 def __init__(self,run_directory):
  self.root=Path(run_directory).resolve()
  self.manifest=json.loads((self.root/'run_manifest.json').read_text(encoding='utf-8-sig'))
  self.run=self.manifest['evaluation_run_id']
  self.manager=TaskManager(self.root/'store')
  self.log=ExecutionLogger(self.root/'store')
  try:
   self._validate_frozen_instructions()
   path=self.root/'batches/batch_plan.json'
   if path.exists():self.plan=json.loads(path.read_text(encoding='utf-8'))
   else:
    with self._source() as db:
     tasks=db.execute('SELECT id,length(payload) FROM tasks WHERE run=? ORDER BY id',(self.run,)).fetchall()
    size=self.manifest.get('batch_size',50)
    if type(size) is not int or not 1<=size<=50:raise BatchExecutionError('batch_size must be 1..50')
    self.plan=dict(schema_version='jqa.batch_plan.v1',evaluation_run_id=self.run,
      run_manifest_sha256=_sha(self.root/'run_manifest.json'),selection_order='ascending task_id',
      batch_size=size,total_tasks=len(tasks),batches=[dict(batch_number=i//size+1,
        task_ids=[t[0] for t in tasks[i:i+size]],
        serialized_task_characters=sum(t[1]+1 for t in tasks[i:i+size]),
        approximate_tokens_characters_div4=(sum(t[1]+1 for t in tasks[i:i+size])+3)//4)
       for i in range(0,len(tasks),size)],
      warning='Character/4 is only a rough size estimate, not an actual tokenizer/context-limit measurement. Dispatch tasks individually.')
    _new_json(path,self.plan)
   if self.plan['evaluation_run_id']!=self.run or self.plan['run_manifest_sha256']!=_sha(self.root/'run_manifest.json'):
    raise BatchExecutionError('Batch plan belongs to a different or changed run')
   identifiers=[t for b in self.plan['batches'] for t in b['task_ids']]
   if len(identifiers)!=len(set(identifiers)) or any(not 1<=len(b['task_ids'])<=50 for b in self.plan['batches']):
    raise BatchExecutionError('Invalid or duplicate batch membership')
   with self._source() as db:
    actual=[r[0] for r in db.execute('SELECT id FROM tasks WHERE run=? ORDER BY id',(self.run,))]
   if identifiers!=actual or len(actual)!=self.manifest['planned_comparisons']:
    raise BatchExecutionError('Batch membership does not exactly match frozen tasks')
   self.task_numbers={identifier:i for i,identifier in enumerate(identifiers,1)}
  except Exception:
   self.close();raise

 @contextmanager
 def _source(self):
  db=sqlite3.connect((self.root/'store/tasks.sqlite3').as_uri()+'?mode=ro',uri=True)
  try:
   db.execute('BEGIN')
   yield db
  finally:db.close()

 def _response_folder(self,task_id):
  # Compact stable slots avoid Windows MAX_PATH problems in deep run/temp roots.
  return self.root/'responses'/f't{self.task_numbers[task_id]:05}'

 def _invalid_output_recorded(self,task_id,attempt_id):
  row=self.log.db.execute('SELECT attempt_number FROM attempts WHERE evaluation_run_id=? AND task_id=? AND attempt_id=?',
    (self.run,task_id,attempt_id)).fetchone()
  return bool(row and any(self._response_folder(task_id).glob(f'a{row[0]:03}.validation_error_*.json')))

 def close(self):
  self.log.close();self.manager.close()

 def __enter__(self):return self
 def __exit__(self,*args):self.close()

 def _validate_frozen_instructions(self):
  self.instructions={}
  for kind,name in [('prompt','assessment_prompt.md'),('rubric','scoring_rubric.md')]:
   path=self.root/'frozen'/name
   if _sha(path)!=self.manifest[kind+'_sha256']:raise BatchExecutionError('Frozen instruction hash mismatch')
   self.instructions[kind]=path.read_bytes().decode('utf-8-sig')

 def _batch(self,number):
  if type(number) is not int or not 1<=number<=len(self.plan['batches']):raise BatchExecutionError('Unknown batch')
  return self.plan['batches'][number-1]

 def _task(self,number,task_id):
  if task_id not in self._batch(number)['task_ids']:raise BatchExecutionError('Task is outside this batch')
  with self._source() as db:
   row=db.execute('SELECT payload,status,result FROM tasks WHERE run=? AND id=?',(self.run,task_id)).fetchone()
  if row is None:raise BatchExecutionError('Task missing from selected run')
  task=json.loads(row[0])
  for key in ('evaluation_run_id','model_name','model_version','prompt_version','rubric_version','prompt_sha256','rubric_sha256'):
   if task.get(key)!=self.manifest.get(key):raise BatchExecutionError('Task version/identity mismatch')
  if any(text not in task['instructions'] for text in self.instructions.values()):raise BatchExecutionError('Task lacks complete frozen instructions')
  return task,row[1],row[2]

 def _history(self):return self.log.recover_run(self.run)

 def export_batch(self,number,*,snapshot=1):
  """Export only unresolved, safely dispatchable tasks. Does NOT start any work.

  Running attempts or successful-but-unimported outputs block dispatch until
  explicitly reconciled. Completed tasks are skipped. Failed/interrupted attempts
  require explicit retry_of when start_task is later called.
  """
  if type(snapshot) is not int or snapshot<1:raise BatchExecutionError('snapshot must be positive')
  latest={}
  for row in self._history():
   if row['task_id'] not in latest or row['attempt_number']>latest[row['task_id']]['attempt_number']:
    latest[row['task_id']]=row
  selected=[];retries={}
  for identifier in self._batch(number)['task_ids']:
   task,status,_=self._task(number,identifier)
   previous=latest.get(identifier)
   if status=='completed':continue
   if previous and (previous['status']=='running' or
       (previous['status']=='succeeded' and not self._invalid_output_recorded(identifier,previous['attempt_id']))):
    raise BatchExecutionError('Reconcile running or successful-but-unimported attempt before redispatch')
   if previous:retries[identifier]=previous['attempt_id']
   selected.append(task)
  directory=self.root/f'batches/batch_{number:03}'
  path=directory/f'tasks_{snapshot:03}.jsonl'
  directory.mkdir(parents=True,exist_ok=True)
  with path.open('x',encoding='utf-8',newline='\n') as stream:
   for task in selected:stream.write(json.dumps(task,ensure_ascii=False)+'\n')
  manifest=dict(evaluation_run_id=self.run,batch_number=number,task_ids=[t['task_id'] for t in selected],
    exported_count=len(selected),task_file_sha256=_sha(path),required_retry_of=retries,
    execution_starts_created=0,instruction='This export does not start execution. Evaluate one task at a time; never replay a completed task.')
  _new_json(directory/f'dispatch_manifest_{snapshot:03}.json',manifest)
  return dict(task_file=str(path),**manifest)

 def start_task(self,number,task_id,*,retry_of=None):
  """Call only when the Agent actually starts this single task."""
  task,status,_=self._task(number,task_id)
  if status=='completed':raise BatchExecutionError('Completed task must not be evaluated again')
  previous=sorted((a for a in self._history() if a['task_id']==task_id),key=lambda a:a['attempt_number'])
  if previous and previous[-1]['status']=='succeeded' and not self._invalid_output_recorded(task_id,previous[-1]['attempt_id']):
   raise BatchExecutionError('Import or reconcile the existing successful output before any retry')
  attempt=self.log.start_attempt(self.run,task_id,executor='codex-agent',retry_of=retry_of)
  return dict(attempt_id=attempt,task=task)

 def _owned_attempt(self,number,task_id,attempt_id):
  self._task(number,task_id)
  rows=[a for a in self._history() if a['attempt_id']==attempt_id and a['task_id']==task_id]
  if len(rows)!=1:raise BatchExecutionError('Attempt/task/batch mismatch')
  return rows[0]

 def complete_task(self,number,task_id,attempt_id,response_path):
  """Archive exact raw bytes, finish actual execution, validate and import ONE task.

  Invalid output raises and stops the caller. No automatic evaluation/retry occurs.
  Equivalent terminal/import replays are idempotent; conflicting raw bytes cannot
  overwrite a previous response for the attempt.
  """
  previous=self._owned_attempt(number,task_id,attempt_id)
  if previous['status'] not in ('running','succeeded'):raise BatchExecutionError('Attempt already failed/interrupted')
  task,_,_=self._task(number,task_id)
  raw=Path(response_path).read_bytes()
  path=self._response_folder(task_id)/f"a{previous['attempt_number']:03}.jsonl"
  path.parent.mkdir(parents=True,exist_ok=True)
  if path.exists():
   if path.read_bytes()!=raw:raise BatchExecutionError('Conflicting response bytes; original preserved')
  else:
   with path.open('xb') as stream:stream.write(raw)
  self.log.finish_attempt(self.run,attempt_id,status='succeeded')
  parsed=None
  try:
   text=raw.decode('utf-8-sig')
   if len(text.splitlines())!=1:raise ResultError('Exactly one JSONL task result required')
   parsed=decode_json(text)
   if not isinstance(parsed,dict) or parsed.get('task_id')!=task_id or parsed.get('evaluation_run_id')!=self.run:
    raise ResultError('Response task/run mismatch; not imported into another task')
   parse_result(text,task)
  except (ResultError,UnicodeError) as exc:
   if isinstance(parsed,dict) and parsed.get('task_id')==task_id and parsed.get('evaluation_run_id')==self.run:
    self.manager.import_results(self.run,path)  # Preserve existing parser's error/status audit for this task only.
   _new_json(path.with_suffix('.validation_error_'+str(len(list(path.parent.glob('*.validation_error_*.json')))+1)+'.json'),
     dict(task_id=task_id,attempt_id=attempt_id,error=str(exc),response_sha256=_sha(path)))
   raise BatchExecutionError('Invalid raw output archived; stop batch and review before retry') from exc
  result=self.manager.import_results(self.run,path)
  if result['errors'] or result['conflicts'] or result['completed']+result['idempotent']!=1:
   raise BatchExecutionError('Import rejected; stop batch')
  return dict(import_result=result,raw_response=str(path),raw_response_sha256=_sha(path))

 def fail_task(self,number,task_id,attempt_id,*,reason,exception=None,interrupted=False):
  self._owned_attempt(number,task_id,attempt_id)
  return self.log.finish_attempt(self.run,attempt_id,status='interrupted' if interrupted else 'failed',
    failure_reason=reason,exception=exception)

 def batch_status(self,number):
  ids=set(self._batch(number)['task_ids']);statuses=Counter();valid=0
  for identifier in sorted(ids):
   task,status,result=self._task(number,identifier);statuses[status]+=1
   if status=='completed':parse_result(result,task);valid+=1
  attempts=[a for a in self._history() if a['task_id'] in ids]
  observed={a['task_id'] for a in attempts}
  return dict(evaluation_run_id=self.run,batch_number=number,planned_comparisons=len(ids),
    executed_attempts=len(attempts),executed_unique_comparisons=len(observed),
    retries=len(attempts)-len(observed),attempt_status_counts=dict(Counter(a['status'] for a in attempts)),
    valid_comparisons=valid,valid_article_ratings=2*valid,remaining_tasks=len(ids)-valid,task_status_counts=dict(statuses))

 def checkpoint(self,number,*,snapshot=1):
  if type(snapshot) is not int or snapshot<1:raise BatchExecutionError('snapshot must be positive')
  status=self.batch_status(number)
  directory=self.root/f'batches/batch_{number:03}'
  _new_json(directory/f'checkpoint_{snapshot:03}.json',status)
  rows=[a for a in self._history() if a['task_id'] in self._batch(number)['task_ids']]
  path=directory/f'execution_log_{snapshot:03}.jsonl'
  with path.open('x',encoding='utf-8') as stream:
   for row in rows:stream.write(json.dumps(row,ensure_ascii=False)+'\n')
  return status
