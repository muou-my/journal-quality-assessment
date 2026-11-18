"""Offline batching mechanics; synthetic fixtures never become real-run scores."""
import csv,json,tempfile,unittest
from pathlib import Path
from pilot.prepare_run import prepare_run
from pilot.batch_execution import BatchExecution,BatchExecutionError
from core.data_processing import load_csv


class BatchExecutionTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
  self.repo=Path(__file__).resolve().parents[1]
  self.config=json.loads((self.repo/'pilot/run_config.example.json').read_text(encoding='utf-8-sig'))
  self.config.update(evaluation_run_id='offline-batch-fixture',input_provenance_note='SYNTHETIC OFFLINE TEST ONLY',mode='multi')

 def tearDown(self):self.tmp.cleanup()

 def prepared(self,n=10):
  path=self.root/'input.csv'
  with path.open('w',encoding='utf-8',newline='') as stream:
   writer=csv.writer(stream);writer.writerow(['doi','journal_name','title','abstract','year'])
   for i in range(n):writer.writerow([f'10.1234/{i}', 'A' if i<n//2 else 'B',f'Synthetic{i}','Synthetic abstract',2025])
  self.config.update(expected_articles=n,batch_size=50)
  cfg=self.root/'config.json';cfg.write_text(json.dumps(self.config),encoding='utf-8')
  manifest=prepare_run(path,cfg,self.root/'run')
  return manifest,self.root/'run'

 def response(self,task,*,score=3,wrong_run=False):
  value={k:task[k] for k in ('task_id','evaluation_run_id','model_name','model_version','prompt_version')}
  value.update(schema_version='jqa.result.v1',articles=[dict(article_id=a['article_id'],originality=score,significance=3,rigour=3,evidence_note='Synthetic unit-test fixture; not a real judgment',uncertainty='high') for a in task['articles']])
  if wrong_run:value['evaluation_run_id']='another-run'
  path=self.root/f'response_{len(list(self.root.glob("response_*.jsonl")))}.jsonl'
  path.write_text(json.dumps(value)+'\n',encoding='utf-8')
  return path

 def test_100_papers_4950_unique_tasks_99_batches_and_no_execution(self):
  manifest,root=self.prepared(100)
  self.assertEqual(manifest['K'],99);self.assertEqual(manifest['planned_comparisons'],4950)
  plan=json.loads((root/'frozen/comparison_plan.json').read_text())
  self.assertEqual(set(plan['article_comparison_counts'].values()),{99})
  with BatchExecution(root) as batch:
   self.assertEqual(len(batch.plan['batches']),99)
   self.assertEqual({len(b['task_ids']) for b in batch.plan['batches']},{50})
   self.assertEqual(len({t for b in batch.plan['batches'] for t in b['task_ids']}),4950)
   export=batch.export_batch(1)
   self.assertEqual(export['exported_count'],50)
   self.assertEqual(batch.batch_status(1)['executed_attempts'],0)
   with self.assertRaises(BatchExecutionError):batch.start_task(1,batch.plan['batches'][1]['task_ids'][0])
   self.assertEqual(batch.log.summary(batch.run)['valid_article_rating_count'],0)

 def test_10_paper_compatibility_completed_tasks_skipped_and_replay_idempotent(self):
  manifest,root=self.prepared()
  self.assertEqual(manifest['planned_comparisons'],45)
  with BatchExecution(root) as batch:
   identifier=batch.plan['batches'][0]['task_ids'][0]
   started=batch.start_task(1,identifier);response=self.response(started['task'])
   result=batch.complete_task(1,identifier,started['attempt_id'],response)
   self.assertEqual(result['import_result']['completed'],1)
   self.assertEqual(batch.complete_task(1,identifier,started['attempt_id'],response)['import_result']['idempotent'],1)
   self.assertEqual(batch.export_batch(1)['exported_count'],44)
   self.assertEqual(batch.batch_status(1)['valid_article_ratings'],2)
   self.assertEqual(batch.batch_status(1)['executed_attempts'],1)
   with self.assertRaises(BatchExecutionError):batch.start_task(1,identifier)
   with self.assertRaises(FileExistsError):batch.export_batch(1)

 def test_restart_running_block_explicit_interruption_and_retry(self):
  _,root=self.prepared()
  with BatchExecution(root) as batch:
   identifier=batch.plan['batches'][0]['task_ids'][0]
   started=batch.start_task(1,identifier)
  with BatchExecution(root) as batch:
   with self.assertRaises(BatchExecutionError):batch.export_batch(1)
   batch.fail_task(1,identifier,started['attempt_id'],reason='Synthetic worker interruption',interrupted=True)
   dispatch=batch.export_batch(1)
   self.assertEqual(dispatch['required_retry_of'][identifier],started['attempt_id'])
   retry=batch.start_task(1,identifier,retry_of=started['attempt_id'])
   batch.fail_task(1,identifier,retry['attempt_id'],reason='Synthetic failure')
   status=batch.checkpoint(1)
   self.assertEqual(status['executed_attempts'],2);self.assertEqual(status['retries'],1)
   self.assertEqual(status['valid_comparisons'],0)

 def test_invalid_score_stops_archives_and_only_explicit_retry_can_succeed(self):
  _,root=self.prepared()
  with BatchExecution(root) as batch:
   identifier=batch.plan['batches'][0]['task_ids'][0]
   started=batch.start_task(1,identifier);bad=self.response(started['task'],score=9)
   original=bad.read_bytes()
   with self.assertRaises(BatchExecutionError):batch.complete_task(1,identifier,started['attempt_id'],bad)
   self.assertEqual(batch.batch_status(1)['valid_comparisons'],0)
   self.assertEqual(bad.read_bytes(),original)
   retry=batch.start_task(1,identifier,retry_of=started['attempt_id'])
   good=self.response(retry['task'])
   batch.complete_task(1,identifier,retry['attempt_id'],good)
   self.assertEqual(batch.batch_status(1)['valid_comparisons'],1)
   self.assertEqual(batch.batch_status(1)['retries'],1)
   with self.assertRaises(BatchExecutionError):batch.complete_task(1,identifier,retry['attempt_id'],bad)

 def test_wrong_run_response_cannot_complete_a_task(self):
  _,root=self.prepared()
  with BatchExecution(root) as batch:
   identifier=batch.plan['batches'][0]['task_ids'][0]
   started=batch.start_task(1,identifier)
   with self.assertRaises(BatchExecutionError):batch.complete_task(1,identifier,started['attempt_id'],self.response(started['task'],wrong_run=True))
   self.assertEqual(batch.batch_status(1)['valid_comparisons'],0)
   self.assertEqual(batch.batch_status(1)['remaining_tasks'],45)

 def test_successful_unimported_attempt_not_blindly_redispatched(self):
  _,root=self.prepared()
  with BatchExecution(root) as batch:
   identifier=batch.plan['batches'][0]['task_ids'][0]
   started=batch.start_task(1,identifier)
   batch.log.finish_attempt(batch.run,started['attempt_id'],status='succeeded')
   with self.assertRaises(BatchExecutionError):batch.export_batch(1)
   with self.assertRaises(BatchExecutionError):batch.start_task(1,identifier,retry_of=started['attempt_id'])
   batch.complete_task(1,identifier,started['attempt_id'],self.response(started['task']))
   self.assertEqual(batch.batch_status(1)['valid_comparisons'],1)

 def test_invalid_size_and_existing_run_are_rejected(self):
  _,root=self.prepared()
  cfg=self.root/'config.json';values=json.loads(cfg.read_text());values['batch_size']=51
  cfg.write_text(json.dumps(values))
  with self.assertRaises(ValueError):prepare_run(self.root/'input.csv',cfg,self.root/'too_large_batch')
  values['batch_size']=50;cfg.write_text(json.dumps(values))
  with self.assertRaises(FileExistsError):prepare_run(self.root/'input.csv',cfg,root)


if __name__=='__main__':unittest.main()
