"""Offline CLI acceptance. Fixture scores are parser tests, never real assessments."""
import csv
import json
import tempfile
import unittest
from pathlib import Path
from scripts.jqa import main, select

ROOT=Path(__file__).resolve().parents[1]

class SkillCLITests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.r=Path(self.temp.name)
        self.csv=self.r/'input.csv'
        with self.csv.open('w',encoding='utf-8-sig',newline='') as f:
            w=csv.writer(f);w.writerow(['doi','journal_name','title','abstract','year'])
            for i in range(4):w.writerow([f'10.1234/cli{i}','测试期刊',f'中文标题{i}','中文摘要，保留逗号\n和换行。',2026])
        c=json.loads((ROOT/'pilot/run_config.example.json').read_text(encoding='utf-8-sig'))
        c.update(evaluation_run_id='offline-cli-test',mode='single',expected_articles=4,batch_size=20,input_provenance_note='Offline test fixture, not real research')
        self.config=self.r/'config.json';self.config.write_text(json.dumps(c),encoding='utf-8')
        self.run=self.r/'run'

    def tearDown(self):self.temp.cleanup()

    def call(self,*args):return main(list(map(str,args)))

    def test_lifecycle_and_partial_export(self):
        v=self.call('validate','--csv',self.csv,'--output',self.r/'v.json')
        self.assertEqual(v['data_quality']['valid_articles'],4)
        p=self.call('prepare','--csv',self.csv,'--config',self.config,'--output',self.run)
        self.assertEqual((p['planned'],p['K']),(6,3))
        dispatch=self.call('next','--run',self.run,'--output',self.r/'dispatch','--batch-size',2)
        self.assertEqual(dispatch['exported_count'],2)
        task=json.loads(Path(dispatch['task_file']).read_text(encoding='utf-8').splitlines()[0])
        self.assertIn('中文摘要，保留逗号\n和换行。',task['articles'][0]['abstract'])
        started=self.call('start','--run',self.run,'--task-id',task['task_id'])
        resumed=self.call('resume','--run',self.run,'--output',self.r/'resume')
        self.assertEqual(resumed['exported_count'],5)
        self.assertEqual(resumed['blocked'][0]['reason'],'running')
        result={k:task[k] for k in ('task_id','evaluation_run_id','model_name','model_version','prompt_version')}
        result.update(schema_version='jqa.result.v1',articles=[dict(article_id=a['article_id'],originality=2,significance=3,rigour=2,evidence_note='OFFLINE PARSER FIXTURE ONLY',uncertainty='high') for a in task['articles']])
        response=self.r/'response.jsonl';response.write_text(json.dumps(result,ensure_ascii=False)+'\n',encoding='utf-8')
        args=('import','--run',self.run,'--task-id',task['task_id'],'--attempt-id',started['attempt_id'],'--response',response)
        self.assertEqual(self.call(*args)['import_result']['completed'],1)
        self.assertEqual(self.call(*args)['import_result']['idempotent'],1)
        with self.assertRaises(ValueError):self.call('start','--run',self.run,'--task-id',task['task_id'])
        status=self.call('status','--run',self.run,'--output',self.r/'status.json')
        self.assertEqual(status['valid_comparison_count'],1)
        self.assertEqual(status['remaining_comparisons'],5)
        with self.assertRaises(ValueError):self.call('export','--run',self.run,'--output',self.r/'final')
        self.call('score','--run',self.run,'--output',self.r/'scores.json')
        self.call('export','--run',self.run,'--output',self.r/'partial','--allow-partial')
        with (self.r/'partial/articles.csv').open(encoding='utf-8-sig') as stream:
            rows=list(csv.DictReader(stream))
        self.assertEqual(len(rows),4)
        self.assertEqual(sum(row['llm_ref']=='' for row in rows),2)
        scores=json.loads((self.r/'scores.json').read_text(encoding='utf-8'))
        self.assertEqual(scores['status'],'partial_not_final')
        self.assertEqual(len(scores['diagnostics']['per_article_stability']),4)

    def test_failure_then_explicit_retry(self):
        self.call('prepare','--csv',self.csv,'--config',self.config,'--output',self.run)
        d=self.call('next','--run',self.run,'--output',self.r/'d','--batch-size',1)
        t=json.loads(Path(d['task_file']).read_text(encoding='utf-8').splitlines()[0])
        a=self.call('start','--run',self.run,'--task-id',t['task_id'])
        self.call('fail','--run',self.run,'--task-id',t['task_id'],'--attempt-id',a['attempt_id'],'--reason','Offline interrupted test','--interrupted')
        with self.assertRaises(ValueError):
            self.call('start','--run',self.run,'--task-id',t['task_id'],'--retry-of',a['attempt_id'])
        self.call('review-stop','--run',self.run,'--evidence','Offline interruption reviewed; explicit retry authorized')
        d=self.call('resume','--run',self.run,'--output',self.r/'d2','--batch-size',1)
        self.assertEqual(d['required_retry_of'][t['task_id']],a['attempt_id'])
        retry=self.call('start','--run',self.run,'--task-id',t['task_id'],'--retry-of',a['attempt_id'])
        self.assertNotEqual(a['attempt_id'],retry['attempt_id'])

    def test_frozen_corruption_stops_dispatch(self):
        self.call('prepare','--csv',self.csv,'--config',self.config,'--output',self.run)
        (self.run/'frozen/assessment_prompt.md').write_text('corrupted',encoding='utf-8')
        with self.assertRaises(ValueError):self.call('next','--run',self.run,'--output',self.r/'dispatch')

    def test_risk_gate_and_disabled_api(self):
        text=self.csv.read_text(encoding='utf-8-sig').replace('中文标题0','测试期刊会议报道')
        self.csv.write_text(text,encoding='utf-8-sig')
        self.call('prepare','--csv',self.csv,'--config',self.config,'--output',self.run)
        s=self.call('status','--run',self.run,'--output',self.r/'s.json')
        self.assertEqual(s['research_quality']['affected_unique_task_count'],3)
        d=self.call('next','--run',self.run,'--output',self.r/'d')
        task=json.loads(Path(d['task_file']).read_text(encoding='utf-8').splitlines()[0])
        with self.assertRaises(ValueError):self.call('start','--run',self.run,'--task-id',task['task_id'])
        with self.assertRaises(ValueError):self.call('api','--run',self.run)

if __name__=='__main__':unittest.main()
