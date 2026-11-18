"""Preparation-only tests; no real papers or models are used."""
import csv
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from pilot.prepare_run import prepare_run


class PilotPreparationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name)
        self.repository=Path(__file__).resolve().parents[1]
        self.csv=self.root/'input.csv'
        with self.csv.open('w',newline='',encoding='utf-8') as stream:
            writer=csv.writer(stream)
            writer.writerow(['doi','journal_name','title','abstract','year'])
            for i in range(10):
                writer.writerow([f'10.1234/p{i}','A' if i<5 else 'B',f'Synthetic{i}','Synthetic abstract',2025])
        self.config=json.loads((self.repository/'pilot/run_config.example.json').read_text(encoding='utf-8-sig'))
        self.config.update(evaluation_run_id='offline-prep-test',input_provenance_note='Synthetic offline test only',model_identity_note='Test fixture; actual identity unknown')
        self.path=self.root/'config.json'
        self.path.write_text(json.dumps(self.config),encoding='utf-8')

    def tearDown(self):
        self.temp.cleanup()

    def test_frozen_versions_export_and_zero_execution(self):
        before=self.csv.read_bytes()
        out=self.root/'prepared'
        manifest=prepare_run(self.csv,self.path,out)
        self.assertEqual(manifest['rubric_version'],'jqa.rubric.v1.1')
        self.assertEqual(manifest['prompt_version'],'jqa.assessment.v1.2')
        for name,digest in manifest['frozen_sha256'].items():
            self.assertEqual(hashlib.sha256((out/name).read_bytes()).hexdigest(),digest)
        tasks=[json.loads(line) for line in (out/'exchange/tasks_initial.jsonl').read_text(encoding='utf-8').splitlines()]
        self.assertEqual(len(tasks),45)
        self.assertTrue(all(t['prompt_version']=='jqa.assessment.v1.2' for t in tasks))
        self.assertTrue(all('jqa.rubric.v1.1' in t['instructions'] for t in tasks))
        summary=json.loads((out/'reports/preparation_execution_summary.json').read_text(encoding='utf-8'))
        self.assertEqual(summary['executed_attempt_count'],0)
        self.assertEqual(summary['valid_article_rating_count'],0)
        self.assertEqual(self.csv.read_bytes(),before)
        with self.assertRaises(FileExistsError):
            prepare_run(self.csv,self.path,out)

    def test_empty_real_template_cannot_be_evaluated(self):
        with self.assertRaises(ValueError):
            prepare_run(self.repository/'pilot/real_articles_10_template.csv',self.path,self.root/'invalid')
        self.assertFalse((self.root/'invalid').exists())

    def test_version_and_configuration_placeholders_block_preparation(self):
        self.config['prompt_version']='obsolete'
        self.path.write_text(json.dumps(self.config),encoding='utf-8')
        with self.assertRaises(ValueError):
            prepare_run(self.csv,self.path,self.root/'bad_version')
        self.config['prompt_version']='jqa.assessment.v1.2'
        self.config['evaluation_run_id']='REPLACE_WITH_UNIQUE_RUN_ID'
        self.path.write_text(json.dumps(self.config),encoding='utf-8')
        with self.assertRaises(ValueError):
            prepare_run(self.csv,self.path,self.root/'bad_config')


if __name__=='__main__':
    unittest.main()
