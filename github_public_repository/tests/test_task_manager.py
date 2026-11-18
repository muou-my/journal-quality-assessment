import copy
import json
import tempfile
import unittest
from pathlib import Path
from core.data_processing import Article
from core.pairwise_comparison import generate_comparison_plan
from core.task_manager import TaskManager
from core.result_parser import parse_result, ResultError


class TaskTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.manager = TaskManager(self.root / 'store')
        self.articles = [Article(f'10.1234/{i}', 'SECRET JOURNAL', f'Title {i}', 'Abstract', 2025, i+2) for i in range(3)]
        self.plan = generate_comparison_plan(self.articles, mode='single')
        self.manager.create_tasks(self.plan, self.articles, evaluation_run_id='run1', model_name='model1')

    def tearDown(self):
        self.manager.close()
        self.tmp.cleanup()

    def result(self, task=None):
        task = task or self.manager.tasks('run1')[0]['payload']
        result = {k: task[k] for k in ('task_id','evaluation_run_id','model_name','model_version','prompt_version')}
        result.update(schema_version='jqa.result.v1', articles=[dict(article_id=a['article_id'], originality=3, significance=2, rigour=4, evidence_note='Text evidence', uncertainty='medium') for a in task['articles']])
        return result

    def write(self, values):
        path = self.root / 'results.jsonl'
        path.write_text('\n'.join(v if isinstance(v,str) else json.dumps(v) for v in values), encoding='utf-8')
        return path

    def test_roundtrip_resume_and_idempotence_conflict(self):
        export = self.root / 'tasks.jsonl'
        self.assertEqual(len(self.manager.export_tasks('run1', export)['task_ids']), 3)
        self.assertNotIn('10.1234', export.read_text(encoding='utf-8'))
        self.assertNotIn('SECRET JOURNAL', export.read_text(encoding='utf-8'))
        self.assertEqual(self.manager.export_tasks('run1', export)['task_ids'], [])
        result = self.result()
        self.assertEqual(self.manager.import_results('run1', self.write([result]))['completed'], 1)
        self.assertEqual(self.manager.import_results('run1', self.write([result]))['idempotent'], 1)
        changed = copy.deepcopy(result)
        changed['articles'][0]['originality'] = 1
        self.assertEqual(self.manager.import_results('run1', self.write([changed]))['conflicts'], 1)
        self.assertEqual(self.manager.results('run1')[0], {**result, 'articles': sorted(result['articles'],key=lambda a:a['article_id'])})
        self.manager.close()
        self.manager = TaskManager(self.root / 'store')
        self.assertEqual(len(self.manager.export_tasks('run1', export, resume=True)['task_ids']), 2)
        self.assertEqual(len(self.manager.results('run1')), 1)

    def test_invalid_results(self):
        expected = self.manager.tasks('run1')[0]['payload']
        cases = ['{bad', '{}', '{"x":1,"x":2}']
        for field, value in [('originality',0), ('rigour',True), ('significance',2.5), ('article_id','wrong'), ('evidence_note',' '), ('uncertainty','unknown')]:
            obj = self.result(); obj['articles'][0][field] = value; cases.append(json.dumps(obj))
        obj = self.result(); del obj['articles'][0]['rigour']; cases.append(json.dumps(obj))
        for field in ('task_id','evaluation_run_id','model_name','model_version','prompt_version'):
            obj = self.result(); obj[field] = 'wrong'; cases.append(json.dumps(obj))
        for raw in cases:
            with self.subTest(raw=raw), self.assertRaises(ResultError):
                parse_result(raw, expected)
        report = self.manager.import_results('run1', self.write(['{bad', cases[3]]))
        self.assertEqual(report['errors'], 2)
        self.assertTrue(self.manager.errors('run1'))
        self.assertIn('failed', [t['status'] for t in self.manager.tasks('run1')])

    def test_model_isolation_and_run_reuse(self):
        self.manager.create_tasks(self.plan, self.articles, evaluation_run_id='run2', model_name='model2', agent='chatgpt')
        tasks2 = self.manager.tasks('run2')
        self.assertNotEqual(tasks2[0]['payload']['task_id'], self.manager.tasks('run1')[0]['payload']['task_id'])
        result = self.result(tasks2[0]['payload'])
        self.assertEqual(self.manager.import_results('run1',self.write([result]))['errors'],1)
        self.assertEqual(self.manager.import_results('run2',self.write([result]))['completed'],1)
        self.assertEqual(self.manager.results('run1'),[])
        with self.assertRaises(ValueError):
            self.manager.create_tasks(self.plan,self.articles,evaluation_run_id='run1',model_name='changed')

    def test_duplicate_batch_quarantined_and_recovery(self):
        result = self.result()
        self.assertEqual(self.manager.import_results('run1',self.write([result,result]))['conflicts'],2)
        self.assertEqual(self.manager.results('run1'),[])
        self.assertEqual(self.manager.import_results('run1',self.write([result]))['completed'],1)


if __name__ == '__main__':
    unittest.main()

