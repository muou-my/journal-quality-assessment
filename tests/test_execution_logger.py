"""Offline execution-ledger tests; all outcomes and model results are synthetic."""
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from core.article_scoring import aggregate_article_scores
from core.data_processing import Article
from core.execution_logger import ExecutionLogger, ExecutionLogError
from core.pairwise_comparison import generate_comparison_plan
from core.task_manager import TaskManager


class ExecutionLoggerTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name)
        self.manager=TaskManager(self.root/'tasks')
        self.articles=[Article(f'10.1234/{i}','J',f'Title{i}','Abstract',2025,i+2) for i in range(3)]
        self.plan=generate_comparison_plan(self.articles,mode='single')
        for run,model,version,prompt in (('r1','modelA','v1','p1'),('r2','modelB','v2','p2')):
            self.manager.create_tasks(self.plan,self.articles,evaluation_run_id=run,
                model_name=model,model_version=version,prompt_version=prompt)
        self.task=self.manager.tasks('r1')[0]['payload']
        self.task_id=self.task['task_id']
        self.logger=ExecutionLogger(self.root/'tasks')

    def tearDown(self):
        self.logger.close()
        self.manager.close()
        self.temp.cleanup()

    def import_result(self,*,bad=False):
        task=self.task
        value={k:task[k] for k in ('task_id','evaluation_run_id','model_name','model_version','prompt_version')}
        value.update(schema_version='jqa.result.v1',articles=[dict(article_id=a['article_id'],
            originality=3,significance=2,rigour=4,evidence_note='Synthetic evidence',uncertainty='high') for a in task['articles']])
        if bad:
            del value['articles'][0]['rigour']
        path=self.root/'response.jsonl'
        path.write_text(json.dumps(value)+'\n',encoding='utf-8')
        return self.manager.import_results('r1',path)

    def test_export_never_implies_execution(self):
        self.manager.export_tasks('r1',self.root/'tasks_export.jsonl')
        summary=self.logger.summary('r1')
        self.assertEqual(summary['planned_comparison_count'],3)
        self.assertEqual(summary['executed_attempt_count'],0)
        self.assertEqual(summary['valid_article_rating_count'],0)
        self.assertEqual(summary['unexecuted_task_count'],3)

    def test_success_validation_and_existing_scoring_bridge(self):
        attempt=self.logger.start_attempt('r1',self.task_id,executor='offline_mock')
        self.logger.finish_attempt('r1',attempt,status='succeeded')
        self.assertEqual(self.logger.summary('r1')['valid_comparison_count'],0)
        self.import_result()
        summary=self.logger.summary('r1')
        self.assertEqual(summary['executed_comparison_count'],1)
        self.assertEqual(summary['executed_unique_comparison_count'],1)
        self.assertEqual(summary['valid_comparison_count'],1)
        self.assertEqual(summary['valid_article_rating_count'],2)
        self.assertEqual(summary['valid_logged_comparison_count'],1)
        report=aggregate_article_scores(self.root/'tasks','r1',self.articles,
            executed_event_ids=self.logger.executed_event_ids('r1'))
        self.assertEqual(report.run_statistics['executed_comparison_count'],1)
        self.assertEqual(report.run_statistics['valid_article_rating_count'],2)
        row=self.logger.recover_run('r1')[0]
        self.assertEqual((row['model_name'],row['model_version'],row['prompt_version']),('modelA','v1','p1'))
        self.assertIsNotNone(row['ended_at'])
        with self.assertRaises(ExecutionLogError):
            self.logger.start_attempt('r1',self.task_id)

    def test_failed_retry_then_valid_does_not_multiply_scores(self):
        first=self.logger.start_attempt('r1',self.task_id)
        try:
            raise RuntimeError('synthetic executor failure')
        except RuntimeError as error:
            self.logger.finish_attempt('r1',first,status='failed',exception=error)
        summary=self.logger.summary('r1')
        self.assertEqual(summary['valid_comparison_count'],0)
        self.assertEqual(summary['attempt_status_counts']['failed'],1)
        row=self.logger.recover_run('r1')[0]
        self.assertEqual(row['failure_reason'],'synthetic executor failure')
        self.assertIn('RuntimeError',row['exception_traceback'])
        second=self.logger.start_attempt('r1',self.task_id,retry_of=first)
        self.logger.finish_attempt('r1',second,status='succeeded')
        self.import_result()
        self.assertEqual(self.import_result()['idempotent'],1)
        summary=self.logger.summary('r1')
        self.assertEqual(summary['executed_attempt_count'],2)
        self.assertEqual(summary['executed_unique_comparison_count'],1)
        self.assertEqual(summary['retry_attempt_count'],1)
        self.assertEqual(summary['valid_article_rating_count'],2)
        self.assertEqual(len(self.logger.executed_event_ids('r1')),1)
        retries=sorted(self.logger.recover_run('r1'),key=lambda r:r['attempt_number'])
        self.assertEqual(retries[1]['retry_count'],1)
        self.assertEqual(retries[1]['retry_of'],first)

    def test_malformed_scores_never_count_valid(self):
        attempt=self.logger.start_attempt('r1',self.task_id)
        self.assertEqual(self.import_result(bad=True)['errors'],1)
        self.logger.finish_attempt('r1',attempt,status='failed',failure_reason='Result schema rejected')
        self.assertEqual(self.logger.summary('r1')['valid_article_rating_count'],0)
        self.assertEqual(self.logger.summary('r1')['valid_logged_comparison_count'],0)

    def test_restart_recovers_running_and_requires_explicit_interruption(self):
        attempt=self.logger.start_attempt('r1',self.task_id)
        self.logger.close()
        self.logger=ExecutionLogger(self.root/'tasks')
        rows=self.logger.recover_run('r1')
        self.assertEqual(rows[0]['attempt_id'],attempt)
        self.assertEqual(rows[0]['status'],'running')
        self.assertIsNone(rows[0]['ended_at'])
        with self.assertRaises(ExecutionLogError):
            self.logger.start_attempt('r1',self.task_id,retry_of=attempt)
        self.logger.finish_attempt('r1',attempt,status='interrupted',failure_reason='Operator confirmed former worker stopped')
        retry=self.logger.start_attempt('r1',self.task_id,retry_of=attempt)
        self.assertNotEqual(attempt,retry)
        self.assertEqual(self.logger.summary('r1')['retry_attempt_count'],1)

    def test_strict_run_isolation(self):
        attempt=self.logger.start_attempt('r1',self.task_id)
        with self.assertRaises(ExecutionLogError):
            self.logger.start_attempt('r2',self.task_id)
        with self.assertRaises(ExecutionLogError):
            self.logger.finish_attempt('r2',attempt,status='succeeded')
        other=self.manager.tasks('r2')[0]['payload']['task_id']
        second=self.logger.start_attempt('r2',other)
        self.logger.finish_attempt('r2',second,status='succeeded')
        self.assertEqual(len(self.logger.recover_run('r1')),1)
        self.assertEqual(len(self.logger.recover_run('r2')),1)
        self.assertEqual(self.logger.summary('r1')['model_name'],'modelA')
        self.assertEqual(self.logger.summary('r2')['model_name'],'modelB')
        with self.assertRaises(ExecutionLogError):
            self.logger.summary('unknown')

    def test_finish_idempotence_and_conflict_preservation(self):
        attempt=self.logger.start_attempt('r1',self.task_id)
        self.logger.finish_attempt('r1',attempt,status='failed',failure_reason='Synthetic error')
        before=self.logger.recover_run('r1')
        self.logger.finish_attempt('r1',attempt,status='failed',failure_reason='Synthetic error')
        self.assertEqual(self.logger.recover_run('r1'),before)
        with self.assertRaises(ExecutionLogError):
            self.logger.finish_attempt('r1',attempt,status='succeeded')
        self.assertEqual(self.logger.recover_run('r1'),before)

    def test_exports_and_original_files_preserved(self):
        attempt=self.logger.start_attempt('r1',self.task_id)
        self.logger.finish_attempt('r1',attempt,status='succeeded')
        self.import_result()
        dbpath=self.root/'tasks/tasks.sqlite3'
        response=self.root/'response.jsonl'
        original_db,original_response=dbpath.read_bytes(),response.read_bytes()
        self.logger.export_jsonl('r1',self.root/'logs.jsonl')
        self.logger.export_summary('r1',self.root/'summary.json')
        exported=json.loads((self.root/'logs.jsonl').read_text(encoding='utf-8'))
        self.assertEqual(exported['schema_version'],'jqa.execution.v1')
        self.assertEqual(exported['task_id'],self.task_id)
        self.assertEqual(json.loads((self.root/'summary.json').read_text())['executed_attempt_count'],1)
        for path in (dbpath,response,self.root/'logs.jsonl'):
            with self.assertRaises(FileExistsError):
                self.logger.export_jsonl('r1',path)
        self.assertEqual(dbpath.read_bytes(),original_db)
        self.assertEqual(response.read_bytes(),original_response)

    def test_missing_legacy_logs_not_fabricated(self):
        self.import_result()
        summary=self.logger.summary('r1')
        self.assertEqual(summary['executed_attempt_count'],0)
        self.assertEqual(summary['valid_comparison_count'],1)
        self.assertEqual(summary['validated_results_without_success_log_count'],1)
        self.assertFalse(summary['execution_log_covers_all_completed'])
        with self.assertRaises(ExecutionLogError):
            self.logger.executed_event_ids('r1')

    def test_timestamps_and_retry_lifecycle(self):
        with self.assertRaises(ExecutionLogError):
            self.logger.start_attempt('r1',self.task_id,started_at=datetime(2025,1,1))
        attempt=self.logger.start_attempt('r1',self.task_id,started_at='2025-01-01T12:00:00+08:00')
        with self.assertRaises(ExecutionLogError):
            self.logger.finish_attempt('r1',attempt,status='succeeded',ended_at='2025-01-01T03:00:00Z')
        with self.assertRaises(ExecutionLogError):
            self.logger.finish_attempt('r1',attempt,status='failed')
        self.logger.finish_attempt('r1',attempt,status='failed',failure_reason='Synthetic',ended_at='2025-01-01T04:01:00Z')
        with self.assertRaises(ExecutionLogError):
            self.logger.start_attempt('r1',self.task_id)
        with self.assertRaises(ExecutionLogError):
            self.logger.start_attempt('r1',self.task_id,retry_of=attempt,started_at='2025-01-01T04:00:00Z')
        self.assertEqual(len(self.logger.recover_run('r1')),1)

    def test_competing_connection_cannot_double_start(self):
        other=ExecutionLogger(self.root/'tasks')
        try:
            self.logger.start_attempt('r1',self.task_id)
            with self.assertRaises(ExecutionLogError):
                other.start_attempt('r1',self.task_id)
            self.assertEqual(other.summary('r1')['executed_attempt_count'],1)
        finally:
            other.close()

    def test_run_configuration_binding_cannot_change(self):
        self.logger.start_attempt('r1',self.task_id)
        raw=self.manager.db.execute('SELECT config FROM runs WHERE id=?',('r1',)).fetchone()[0]
        config=json.loads(raw); config['fixture_configuration_changed']=True
        with self.manager.db:
            self.manager.db.execute('UPDATE runs SET config=? WHERE id=?',(json.dumps(config),'r1'))
        with self.assertRaises(ExecutionLogError):
            self.logger.recover_run('r1')

    def test_log_directory_cannot_be_rebound_to_another_task_store(self):
        other_manager=TaskManager(self.root/'different_tasks')
        try:
            with self.assertRaises(ExecutionLogError):
                ExecutionLogger(self.root/'different_tasks',self.logger.directory)
        finally:
            other_manager.close()

    def test_changed_run_configuration_and_invalid_completed_rejected(self):
        self.logger.start_attempt('r1',self.task_id)
        self.import_result()
        with self.manager.db:
            self.manager.db.execute("UPDATE tasks SET result='{}' WHERE id=?",(self.task_id,))
        with self.assertRaises(ExecutionLogError):
            self.logger.summary('r1')


if __name__=='__main__':
    unittest.main()

