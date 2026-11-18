"""Offline scoring integration tests using real task imports and synthetic scores."""
import copy
import json
import tempfile
import unittest
from pathlib import Path

from core.article_scoring import aggregate_article_scores, ScoringError
from core.journal_scoring import aggregate_journal_scores
from core.data_processing import Article
from core.pairwise_comparison import generate_comparison_plan
from core.task_manager import TaskManager, digest


class ScoringTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.manager = TaskManager(self.root/'store')
        self.articles = [Article(f'10.1234/{i}', 'A' if i<2 else 'B', f'Title{i}', 'Abstract', 2025,i+2) for i in range(4)]
        self.plan = generate_comparison_plan(self.articles,mode='multi')
        self.create('run1')

    def tearDown(self):
        self.manager.close()
        self.temp.cleanup()

    def create(self, run, **metadata):
        self.manager.create_tasks(self.plan,self.articles,evaluation_run_id=run,
                                  model_name=metadata.get('model_name','mock'),
                                  model_version=metadata.get('model_version','v1'),
                                  prompt_version=metadata.get('prompt_version','p1'))

    def task_result(self, run, pair, scores):
        tokens = {'article-'+digest([run,self.articles[i].doi])[:24]:score for i,score in zip(pair,scores)}
        task = next(t['payload'] for t in self.manager.tasks(run)
                    if {a['article_id'] for a in t['payload']['articles']}==set(tokens))
        result = {k:task[k] for k in ('task_id','evaluation_run_id','model_name','model_version','prompt_version')}
        result.update(schema_version='jqa.result.v1',articles=[dict(article_id=token,
            originality=s[0],significance=s[1],rigour=s[2],evidence_note='Synthetic evidence',uncertainty='medium')
            for token,s in tokens.items()])
        return result

    def import_values(self, run, values):
        path = self.root/'results.jsonl'
        path.write_text('\n'.join(json.dumps(v) for v in values),encoding='utf-8')
        return self.manager.import_results(run,path)

    def report(self, run='run1', **kwargs):
        return aggregate_article_scores(self.root/'store',run,self.articles,**kwargs)

    def test_dimensions_composite_and_std(self):
        self.import_values('run1',[
            self.task_result('run1',(0,1),[(4,3,2),(1,1,1)]),
            self.task_result('run1',(0,2),[(2,1,4),(3,3,3)])])
        report = self.report()
        paper = next(a for a in report.articles if a['doi']=='10.1234/0')
        self.assertEqual(paper['valid_evaluation_count'],2)
        self.assertEqual((paper['originality_mean'],paper['significance_mean'],paper['rigour_mean']),(3,2,3))
        self.assertAlmostEqual(paper['llm_ref'],8/3)
        self.assertAlmostEqual(paper['score_std'],1/3)
        self.assertEqual(len(paper['sources']),2)
        self.assertIn('result_sha256',paper['sources'][0])

    def test_equal_article_weights_and_partial_coverage(self):
        self.import_values('run1',[
            self.task_result('run1',(0,1),[(4,4,4),(1,1,1)]),
            self.task_result('run1',(0,2),[(2,2,2),(3,3,3)])])
        article_report=self.report()
        journal={j['journal_name']:j for j in aggregate_journal_scores(article_report).journals}
        self.assertEqual(journal['A']['journal_score'],2)  # (3+1)/2, not (4+2+1)/3
        self.assertEqual(journal['A']['article_score_std'],1)
        self.assertEqual(journal['A']['coverage'],1)
        self.assertFalse(journal['A']['partial_sample'])
        self.assertEqual(journal['B']['journal_score'],3)
        self.assertEqual(journal['B']['coverage'],0.5)
        self.assertEqual(journal['B']['total_article_count'],2)
        self.assertEqual(journal['B']['scored_article_count'],1)
        self.assertTrue(journal['B']['partial_sample'])
        unscored=next(a for a in article_report.articles if a['doi']=='10.1234/3')
        self.assertIsNone(unscored['llm_ref'])
        self.assertIsNone(unscored['score_std'])

    def test_all_valid_complete_and_idempotent_import(self):
        results=[]
        for pair in ((0,1),(0,2),(0,3),(1,2),(1,3),(2,3)):
            results.append(self.task_result('run1',pair,[(3,3,3),(3,3,3)]))
        self.import_values('run1',results)
        before=self.report()
        self.assertEqual(self.import_values('run1',results)['idempotent'],6)
        self.assertEqual(before,self.report())
        for journal in aggregate_journal_scores(before).journals:
            self.assertEqual(journal['coverage'],1)
            self.assertEqual(journal['journal_score'],3)
            self.assertEqual(journal['article_score_std'],0)
        self.assertEqual(before.run_statistics['valid_comparison_count'],6)
        self.assertEqual(before.run_statistics['valid_article_rating_count'],12)
        self.assertEqual(before.run_statistics['planned_comparison_count'],6)
        self.assertIsNone(before.run_statistics['executed_comparison_count'])

    def test_failures_and_no_valid_scores(self):
        bad=self.task_result('run1',(0,1),[(1,1,1),(1,1,1)])
        del bad['articles'][0]['rigour']
        self.import_values('run1',[bad])
        report=self.report()
        self.assertEqual(report.run_statistics['task_status_counts']['failed'],1)
        self.assertEqual(report.run_statistics['valid_article_rating_count'],0)
        for journal in aggregate_journal_scores(report).journals:
            self.assertEqual(journal['coverage'],0)
            self.assertIsNone(journal['journal_score'])
            self.assertIsNone(journal['article_score_std'])
            self.assertEqual(journal['evaluation_status'],'unscored')

    def test_model_run_and_prompt_isolation(self):
        for run,metadata in [('run2',{'model_name':'other'}),('run3',{'model_version':'v2'}),('run4',{'prompt_version':'p2'})]:
            self.create(run,**metadata)
            self.import_values(run,[self.task_result(run,(0,1),[(4,4,4),(4,4,4)])])
            report=self.report(run)
            self.assertEqual(report.run_statistics['scored_article_count'],2)
            self.assertEqual(report.articles[0]['llm_ref'],4)
        self.assertEqual(self.report().run_statistics['scored_article_count'],0)
        self.import_values('run1',[self.task_result('run1',(0,1),[(1,1,1),(1,1,1)])])
        mixed=copy.deepcopy(self.report())
        mixed.articles[0]['sources'][0]['model_version']='v2'
        with self.assertRaises(ScoringError):
            aggregate_journal_scores(mixed)
        with self.assertRaises(ScoringError):
            aggregate_journal_scores([self.report(),self.report('run2')])

    def test_execution_evidence_and_readonly_store(self):
        self.import_values('run1',[self.task_result('run1',(0,1),[(2,2,2),(2,2,2)])])
        events=[e.event_id for e in self.plan.events]
        before=(self.root/'store/tasks.sqlite3').read_bytes()
        report=self.report(executed_event_ids=events)
        self.assertEqual(report.run_statistics['executed_comparison_count'],6)
        self.assertEqual(report.run_statistics['valid_comparison_count'],1)
        self.assertEqual(report.run_statistics['valid_article_rating_count'],2)
        self.assertEqual({a['executed_comparison_count'] for a in report.articles},{3})
        self.assertEqual((self.root/'store/tasks.sqlite3').read_bytes(),before)
        with self.assertRaises(ScoringError):
            self.report(executed_event_ids=[])
        with self.assertRaises(ScoringError):
            self.report(executed_event_ids=events+events)

    def test_zero_task_run_retains_roster_and_no_zero_scores(self):
        empty_plan=generate_comparison_plan(self.articles,mode='multi',budget=0)
        self.manager.create_tasks(empty_plan,self.articles,evaluation_run_id='empty',model_name='mock')
        report=self.report('empty')
        self.assertEqual(report.run_statistics['planned_comparison_count'],0)
        self.assertEqual(report.run_statistics['total_article_count'],4)
        self.assertTrue(all(a['llm_ref'] is None for a in report.articles))
        self.assertEqual(len(aggregate_journal_scores(report).journals),2)

    def test_duplicate_event_is_rejected_not_double_counted(self):
        rows=self.manager.db.execute('SELECT id,private FROM tasks WHERE run=? ORDER BY id',('run1',)).fetchall()
        first=json.loads(rows[0][1]); second=json.loads(rows[1][1])
        second['event_id']=first['event_id']
        with self.manager.db:
            self.manager.db.execute('UPDATE tasks SET private=? WHERE id=?',(json.dumps(second),rows[1][0]))
        with self.assertRaises(ScoringError):
            self.report()

    def test_corrupt_completed_rejected_and_roster_checked(self):
        self.import_values('run1',[self.task_result('run1',(0,1),[(2,2,2),(2,2,2)])])
        with self.assertRaises(ScoringError):
            aggregate_article_scores(self.root/'store','run1',self.articles[:2])
        with self.assertRaises(ScoringError):
            self.report('missing')
        with self.manager.db:
            self.manager.db.execute("UPDATE tasks SET result='{}' WHERE status='completed'")
        with self.assertRaises(ScoringError):
            self.report()


if __name__=='__main__':
    unittest.main()

