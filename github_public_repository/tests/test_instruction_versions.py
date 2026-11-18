"""Version/format safeguards only; NOT evidence that model bias is eliminated."""
import copy
import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from core.result_parser import parse_result, ResultError
from core.task_manager import TaskManager
from pilot.prepare_run import prepare_run
from pilot.versioning import resolve_versions, validate_identity


class InstructionUpgradeTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.root=Path(self.tmp.name)
        self.repo=Path(__file__).resolve().parents[1]
        self.csv=self.root/'input.csv'
        self.csv.write_text('doi,journal_name,title,abstract,year\n'+''.join(
            f'10.1234/{i},J,Synthetic{i},Synthetic abstract,2025\n' for i in range(10)),encoding='utf-8')
        self.config=json.loads((self.repo/'pilot/run_config.example.json').read_text(encoding='utf-8-sig'))
        self.config.update(evaluation_run_id='new-fixture',mode='single',input_provenance_note='Synthetic only')
        self.path=self.root/'config.json'

    def tearDown(self):
        self.tmp.cleanup()

    def prepare(self,name='new'):
        self.path.write_text(json.dumps(self.config),encoding='utf-8')
        return prepare_run(self.csv,self.path,self.root/name)

    def exported(self):
        return [json.loads(line) for line in (self.root/'new/exchange/tasks_initial.jsonl').read_text(encoding='utf-8').splitlines()]

    def result(self,task):
        value={k:task[k] for k in ('task_id','evaluation_run_id','model_name','model_version','prompt_version')}
        value.update(schema_version='jqa.result.v1',articles=[dict(article_id=a['article_id'],
            originality=3,significance=2,rigour=3,evidence_note='Available text supports this provisional judgment; method detail not reported.',uncertainty='high') for a in task['articles']])
        return value

    def test_legacy_content_is_byte_preserved(self):
        for old,generic,expected in (
            ('jqa.assessment.v1.1','assessment_prompt.md','d15d8beb51e8c1fec05406fcc25be29abe650a768e8be8fde90ae57118c02c1a'),
            ('jqa.rubric.v1','scoring_rubric.md','cc35b1e32664f83dfdcbd2cb3686670bad76e078dab5bbb3a23c1dbe4b178952')):
            data=(self.repo/f'references/versions/{old}.md').read_bytes()
            self.assertEqual(data,(self.repo/'references'/generic).read_bytes())
            self.assertEqual(hashlib.sha256(data).hexdigest(),expected)

    def test_every_task_embeds_selected_full_text_and_manifest_hashes(self):
        manifest=self.prepare()
        versions=resolve_versions(self.repo,'jqa.assessment.v1.2','jqa.rubric.v1.1')
        for task in self.exported():
            for key in ('prompt','rubric'):
                self.assertIn(versions[key]['text'],task['instructions'])
                self.assertEqual(manifest[key+'_sha256'],versions[key]['sha256'])
                frozen='assessment_prompt.md' if key=='prompt' else 'scoring_rubric.md'
                self.assertEqual(hashlib.sha256((self.root/'new/frozen'/frozen).read_bytes()).hexdigest(),manifest[key+'_sha256'])
            self.assertEqual(task['prompt_version'],'jqa.assessment.v1.2')
            self.assertEqual(task['rubric_version'],'jqa.rubric.v1.1')
            self.assertEqual(task['prompt_sha256'],manifest['prompt_sha256'])
            self.assertEqual(task['rubric_sha256'],manifest['rubric_sha256'])
            self.assertNotIn('model_identity_verification',task)
        self.assertEqual(manifest['actual_execution_attempts_at_preparation'],0)

    def test_equal_scores_and_insufficient_evidence_keep_existing_json(self):
        self.prepare()
        task=self.exported()[0]
        value=self.result(task)
        accepted=parse_result(json.dumps(value),task)
        self.assertEqual(accepted['articles'][0]['originality'],accepted['articles'][1]['originality'])
        self.assertEqual(accepted['articles'][0]['uncertainty'],'high')
        self.assertEqual(accepted['articles'][0]['rigour'],3)  # high uncertainty is not converted into a low score
        self.assertEqual(set(accepted),{'schema_version','task_id','evaluation_run_id','model_name','model_version','prompt_version','articles'})
        for invalid in (3.5,3.0,True,0,5,'3',None):
            bad=copy.deepcopy(value);bad['articles'][0]['rigour']=invalid
            with self.assertRaises(ResultError):
                parse_result(json.dumps(bad),task)
        value['rubric_version']='jqa.rubric.v1.1'
        with self.assertRaises(ResultError):
            parse_result(json.dumps(value),task)

    def test_new_safeguards_are_present_not_a_model_behaviour_claim(self):
        selected=resolve_versions(self.repo,'jqa.assessment.v1.2','jqa.rubric.v1.1')
        prompt=selected['prompt']['text'];rubric=selected['rubric']['text']
        for fragment in ('mechanically default','ONLY when textual evidence','exactly the same scores',
                         'No positional preference','NOT evidence of a flaw','NOT\n   automatically lower scores'):
            self.assertIn(fragment,prompt)
        for fragment in ('World-leading','Internationally excellent','Internationally recognised',
                         'Nationally recognised','discretized adaptation','not verbatim',
                         'Missing reporting detail','not a fourth quality dimension'):
            self.assertIn(fragment,rubric)

    def test_hash_tampering_and_incompatible_versions_rejected(self):
        with self.assertRaises(ValueError):
            resolve_versions(self.repo,'jqa.assessment.v1.2','jqa.rubric.v1')
        self.config['prompt_sha256']='0'*64
        with self.assertRaises(ValueError):
            self.prepare()
        repo=self.root/'tampered'
        shutil.copytree(self.repo/'references/versions',repo/'references/versions')
        shutil.copyfile(self.repo/'references/version_registry.json',repo/'references/version_registry.json')
        with (repo/'references/versions/jqa.rubric.v1.1.md').open('a',encoding='utf-8') as stream:
            stream.write('modified')
        with self.assertRaises(ValueError):
            resolve_versions(repo,'jqa.assessment.v1.2','jqa.rubric.v1.1')

    def test_old_frozen_bundle_unchanged_after_new_run(self):
        old=copy.deepcopy(self.config)
        selected=resolve_versions(self.repo,'jqa.assessment.v1.1','jqa.rubric.v1')
        self.config.update(evaluation_run_id='legacy-fixture',prompt_version='jqa.assessment.v1.1',rubric_version='jqa.rubric.v1',
            prompt_sha256=selected['prompt']['sha256'],rubric_sha256=selected['rubric']['sha256'])
        self.prepare('old')
        before={str(p.relative_to(self.root/'old')):hashlib.sha256(p.read_bytes()).hexdigest() for p in (self.root/'old').rglob('*') if p.is_file()}
        self.config=old
        self.prepare()
        after={str(p.relative_to(self.root/'old')):hashlib.sha256(p.read_bytes()).hexdigest() for p in (self.root/'old').rglob('*') if p.is_file()}
        self.assertEqual(before,after)
        with self.assertRaises(FileExistsError):
            self.prepare('old')

    def test_unknown_identity_record_does_not_claim_verification(self):
        manifest=self.prepare()
        record=manifest['model_identity_verification']
        self.assertIsNone(record['checked_at'])
        self.assertEqual(record['verification_scope'],'unverified')
        self.assertEqual(manifest['model_version'],'unknown')
        self.assertFalse(record['metadata_copy_is_identity_verification'])
        self.assertFalse(record['evidence_truth_automatically_verified'])

    def test_identity_evidence_recorded_but_not_exported(self):
        self.config.update(model_name='fixture-model',model_version='fixture-version')
        record=self.config['model_identity_verification']
        record.update(checked_at='2026-11-12T10:00:00+08:00',environment_visible_name='fixture-model',
            environment_visible_version='fixture-version',version_information_source='Synthetic environment record',
            verification_scope='name_and_version',evidence_basis='provider_execution_record',
            evidence_references=['LOCAL-SYNTHETIC-IDENTITY-RECORD'])
        manifest=self.prepare()
        self.assertEqual(manifest['model_identity_verification']['checked_at'],record['checked_at'])
        self.assertIn('LOCAL-SYNTHETIC-IDENTITY-RECORD',json.dumps(manifest))
        self.assertNotIn('LOCAL-SYNTHETIC-IDENTITY-RECORD',json.dumps(self.exported()))

    def test_new_run_requires_explicit_hashes_and_identity_record(self):
        original=copy.deepcopy(self.config)
        for field,value in (('prompt_sha256',None),('rubric_sha256',''),('model_identity_verification',None)):
            self.config=copy.deepcopy(original)
            self.config[field]=value
            with self.assertRaises(ValueError):
                self.prepare()
        self.config=copy.deepcopy(original)
        del self.config['rubric_sha256']
        with self.assertRaises(ValueError):
            self.prepare()

    def test_copying_task_metadata_cannot_verify_model_identity(self):
        self.config['model_identity_verification']['evidence_basis']='copied_task_metadata'
        with self.assertRaises(ValueError):
            validate_identity(self.config)
        self.config['model_identity_verification']['evidence_basis']='model_self_report'
        with self.assertRaises(ValueError):
            validate_identity(self.config)
        self.config['model_identity_verification']['evidence_basis']='unavailable'
        self.config['model_version']='asserted-but-unverified'
        with self.assertRaises(ValueError):
            validate_identity(self.config)

    def test_identity_scope_requires_evidence_time_and_matching_version(self):
        record=self.config['model_identity_verification']
        record['verification_scope']='name_and_version'
        with self.assertRaises(ValueError):
            validate_identity(self.config)
        record.update(checked_at='2026-11-12T10:00:00',environment_visible_name='X',
            environment_visible_version='V',version_information_source='Fixture',
            evidence_basis='environment_display',evidence_references=['fixture'])
        self.config.update(model_name='X',model_version='V')
        with self.assertRaises(ValueError):
            validate_identity(self.config)  # no timezone
        record['checked_at']='2026-11-12T10:00:00+08:00'
        self.config['model_version']='wrong'
        with self.assertRaises(ValueError):
            validate_identity(self.config)
        record['verification_scope']='name_only'
        self.config['model_version']='unknown'
        self.assertEqual(validate_identity(self.config)['verification_scope'],'name_only')


if __name__=='__main__':
    unittest.main()
