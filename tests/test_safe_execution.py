"""Synthetic offline storage/recovery checks; never use real-run judgments."""
import json
import os
import unittest
from unittest.mock import patch

from tests import test_batch_execution as fixtures
from pilot.safe_execution import SafeBatchExecution, atomic_bytes
from pilot.batch_execution import BatchExecutionError


class SafeExecutionTests(unittest.TestCase):
    setUp=fixtures.BatchExecutionTests.setUp
    prepared=fixtures.BatchExecutionTests.prepared
    response=fixtures.BatchExecutionTests.response

    def tearDown(self):
        self.doCleanups()  # Windows requires SQLite handles closed before rmtree.
        self.tmp.cleanup()

    def started(self):
        _,root=self.prepared()
        b=SafeBatchExecution(root)
        self.addCleanup(b.close)
        task=b.plan['batches'][0]['task_ids'][0]
        return b,task,b.start_task(1,task)

    def text(self,task):
        data=json.loads(self.response(task).read_text(encoding='utf-8'))
        for a in data['articles']:
            a['evidence_note']='中文证据：α β，😀，𠮷；引号"、反斜杠\\、换行\n均需保留。'
        return json.dumps(data,ensure_ascii=False)+'\n'

    def test_utf8_chinese_nonascii_and_special_characters(self):
        b,t,a=self.started();text=self.text(a['task']);p=self.root/'中文.jsonl'
        b.save_response(1,t,a['attempt_id'],p,text)
        self.assertEqual(p.read_bytes(),text.encode('utf-8'))
        result=b.complete_task(1,t,a['attempt_id'],p)
        self.assertEqual(result['import_result']['completed'],1)
        self.assertEqual(b.batch_status(1)['valid_article_ratings'],2)

    def test_atomic_commit_failure_no_empty_destination_and_immediate_stop(self):
        b,t,a=self.started();p=self.root/'raw.jsonl';text=self.text(a['task'])
        with patch('pilot.safe_execution.os.link',side_effect=PermissionError('offline denied')):
            with self.assertRaises(BatchExecutionError):b.save_response(1,t,a['attempt_id'],p,text)
        self.assertFalse(p.exists())
        self.assertEqual(next(self.root.glob('.jqa-*.tmp')).read_bytes(),text.encode('utf-8'))
        self.assertEqual(b.batch_status(1)['valid_comparisons'],0)
        self.assertEqual(b._history()[0]['status'],'running')
        other=b.plan['batches'][0]['task_ids'][1]
        with self.assertRaises(BatchExecutionError):b.start_task(1,other)
        self.assertTrue(b.safety_status()['open_halts'][0]['generated_response'])

    def test_empty_response_not_model_failure_or_success(self):
        b,t,a=self.started();p=self.root/'empty.jsonl';p.write_bytes(b'')
        with self.assertRaises(BatchExecutionError):b.complete_task(1,t,a['attempt_id'],p)
        self.assertEqual(b._history()[0]['status'],'running')
        self.assertEqual(b.batch_status(1)['valid_comparisons'],0)
        self.assertFalse((b._response_folder(t)/'a001.jsonl').exists())

    def test_invalid_score_stops_other_tasks_persistently(self):
        b,t,a=self.started();p=self.response(a['task'],score=9)
        with self.assertRaises(BatchExecutionError):b.complete_task(1,t,a['attempt_id'],p)
        self.assertEqual(b._history()[0]['status'],'failed')
        self.assertTrue((b._response_folder(t)/'a001.jsonl').is_file())
        with SafeBatchExecution(b.root) as restarted:
            with self.assertRaises(BatchExecutionError):restarted.start_task(1,b.plan['batches'][0]['task_ids'][1])
            restarted.review_stop(evidence='Offline schema error reviewed; explicit retry only')
            retry=restarted.start_task(1,t,retry_of=a['attempt_id'])
            restarted.complete_task(1,t,retry['attempt_id'],self.response(retry['task']))
            self.assertEqual(restarted.batch_status(1)['valid_comparisons'],1)

    def test_generated_but_save_failed_recover_original_without_new_attempt(self):
        b,t,a=self.started();p=self.root/'raw.jsonl';text=self.text(a['task'])
        with patch('pilot.safe_execution.os.link',side_effect=OSError('offline disk error')):
            with self.assertRaises(BatchExecutionError):b.save_response(1,t,a['attempt_id'],p,text)
        with SafeBatchExecution(b.root) as restarted:
            with self.assertRaises(BatchExecutionError):restarted.review_stop(evidence='Cannot clear running work')
            temp=next(self.root.glob('.jqa-*.tmp'))
            # Restore the actual same bytes, not regenerated ratings.
            restarted.save_response(1,t,a['attempt_id'],p,temp.read_text(encoding='utf-8'))
            restarted.complete_task(1,t,a['attempt_id'],p)
            restarted.review_stop(evidence='Original fsynced temporary response recovered and imported')
            self.assertEqual(restarted.batch_status(1)['executed_attempts'],1)
            with self.assertRaises(BatchExecutionError):restarted.start_task(1,t)
            other=restarted.plan['batches'][0]['task_ids'][1]
            restarted.start_task(1,other)

    def test_no_overwrite_and_idempotent_import(self):
        b,t,a=self.started();p=self.root/'raw.jsonl';text=self.text(a['task'])
        b.save_response(1,t,a['attempt_id'],p,text)
        b.complete_task(1,t,a['attempt_id'],p)
        self.assertEqual(b.complete_task(1,t,a['attempt_id'],p)['import_result']['idempotent'],1)
        with self.assertRaises(FileExistsError):atomic_bytes(p,b'changed')
        self.assertEqual(p.read_bytes(),text.encode('utf-8'))

    def test_global_unimported_success_gate_even_without_halt(self):
        b,t,a=self.started();b.log.finish_attempt(b.run,a['attempt_id'],status='succeeded')
        with self.assertRaises(BatchExecutionError):b.start_task(1,b.plan['batches'][0]['task_ids'][1])
        with self.assertRaises(BatchExecutionError):b.review_stop(evidence='Unreviewed lost response')
        b.review_stop(evidence='Offline audit establishes loss, explicit reevaluation authorized',retryable_attempts=[a['attempt_id']])
        b.start_task(1,t,retry_of=a['attempt_id'])

    def test_invalid_utf8_input_and_malformed_json(self):
        b,t,a=self.started();p=self.root/'bad.jsonl';p.write_bytes(b'\xff')
        with self.assertRaises(BatchExecutionError):b.complete_task(1,t,a['attempt_id'],p)
        self.assertEqual(b._history()[0]['status'],'running')
        p2=self.root/'invalid.jsonl';p2.write_text('{broken}\n',encoding='utf-8')
        with self.assertRaises(BatchExecutionError):b.complete_task(1,t,a['attempt_id'],p2)
        self.assertEqual(b._history()[0]['status'],'failed')

    def test_malformed_generated_text_preserved_but_not_imported(self):
        b,t,a=self.started();p=self.root/'malformed.jsonl';text='{中文坏JSON}\n'
        with self.assertRaises(BatchExecutionError):b.save_response(1,t,a['attempt_id'],p,text)
        self.assertEqual(p.read_bytes(),text.encode('utf-8'))
        self.assertEqual(b.batch_status(1)['valid_comparisons'],0)

    def test_partial_temp_write_and_archive_failure_are_not_success(self):
        b,t,a=self.started();p=self.response(a['task'])
        with patch('pilot.safe_execution.os.fsync',side_effect=OSError('offline flush failure')):
            with self.assertRaises(BatchExecutionError):b.complete_task(1,t,a['attempt_id'],p)
        self.assertFalse((b._response_folder(t)/'a001.jsonl').exists())
        self.assertEqual(b._history()[0]['status'],'running')
        self.assertEqual(b.batch_status(1)['valid_comparisons'],0)

    def test_legacy_empty_archive_can_recover_original_without_overwrite(self):
        b,t,a=self.started();folder=b._response_folder(t);folder.mkdir(parents=True)
        original=folder/'a001.jsonl';original.write_bytes(b'')
        b.log.finish_attempt(b.run,a['attempt_id'],status='succeeded')
        b.halt('historical_empty_archive',t,a['attempt_id'])
        response=self.response(a['task'])
        b.complete_task(1,t,a['attempt_id'],response)
        self.assertEqual(original.read_bytes(),b'')
        self.assertEqual(len(list(folder.glob('*.recovered_*.jsonl'))),1)
        self.assertEqual(b.complete_task(1,t,a['attempt_id'],response)['import_result']['idempotent'],1)
        b.review_stop(evidence='Original actual response recovered, validated and imported, not re-evaluated')
        self.assertEqual(b.batch_status(1)['executed_attempts'],1)


if __name__=='__main__':unittest.main()
