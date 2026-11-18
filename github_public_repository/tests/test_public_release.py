"""Public-only portable workflows; synthetic data and Mock transport only."""
import argparse
import contextlib
import io
import json
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.public_cli import main,prepare,doctor,paid_disclosure,relocate
from scripts.install import install
from scripts.release_tools import scan
from core.model_executor import ModelExecutor
from core.mock_provider import SyntheticOnlyProvider

ROOT=Path(__file__).resolve().parents[1]


class PublicReleaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.base=Path(self.tmp.name)/'用户 space';self.base.mkdir()
    def tearDown(self):self.tmp.cleanup()
    def prep(self,name='run',provider='deepseek',mode='multi'):
        return main(['prepare','--csv',str(ROOT/f'assets/synthetic_{mode}.csv'),'--mode',mode,
                     '--provider',provider,'--output',str(self.base/name),'--run-id',name])

    def test_no_key_diagnosis_and_single_multi_user_csv_prepare(self):
        self.assertTrue(doctor(self.base/'诊断')['passed'])
        a=self.prep(provider='agent',mode='single');b=self.prep(name='other')
        self.assertEqual(a['planned'],6);self.assertEqual(b['planned'],6)
        self.assertNotEqual(a['evaluation_run_id'],b['evaluation_run_id'])

    def test_demo_runs_all_and_exports_final_tables_without_network(self):
        result=main(['demo','--output',str(self.base/'演示')])
        self.assertEqual(result['network_requests'],0)
        export=Path(result['export'])
        scores=json.loads((export/'scores.json').read_text(encoding='utf-8'))
        self.assertEqual(scores['status'],'complete')
        self.assertEqual(len(scores['articles']['articles']),4)
        self.assertEqual(len(scores['journals']['journals']),2)
        self.assertTrue((export/'articles.csv').is_file())
        self.assertIn('SYNTHETIC',(export/'articles.csv').read_text(encoding='utf-8-sig'))

    def test_paid_disclosure_gate_precedes_credentials_and_http(self):
        self.prep()
        args=argparse.Namespace(run=self.base/'run',allow_paid=False,confirm_send=True)
        buffer=io.StringIO()
        with contextlib.redirect_stderr(buffer):
            with self.assertRaises(ValueError):paid_disclosure(args)
        self.assertIn('transmitted',buffer.getvalue());self.assertIn('estimate',buffer.getvalue())
        args.allow_paid=True;args.confirm_send=False
        with patch('scripts.public_cli.sys.stdin.isatty',return_value=False),contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(ValueError):paid_disclosure(args)
        self.assertFalse((self.base/'run/api/requests').exists())

    def test_explicit_cny_conversion_and_no_guessed_exchange_rate(self):
        args=['prepare','--csv',str(ROOT/'assets/synthetic_multi.csv'),'--mode','multi','--provider','deepseek','--output',str(self.base/'money'),'--run-id','money','--budget-cny','10']
        with self.assertRaises(ValueError):main(args)
        result=main(args+['--usd-to-cny','10','--fx-date','2026-01-01'])
        cfg=json.loads((Path(result['run_directory'])/'frozen/api_config.json').read_text(encoding='utf-8'))
        self.assertEqual(cfg['max_cost_usd'],'1.000000')

    def test_install_idempotent_uninstall_and_protect_unmanaged(self):
        target=self.base/'project/.agents/skills/journal-quality-assessment'
        self.assertTrue(install(target)['installed'])
        self.assertTrue(install(target)['unchanged'])
        data=self.base/'research.txt';data.write_text('do not delete',encoding='utf-8')
        self.assertTrue(install(target,uninstall=True)['uninstalled']);self.assertTrue(data.exists())
        target.mkdir();(target/'user.txt').write_text('owned',encoding='utf-8')
        with self.assertRaises(ValueError):install(target)

    def test_upgrade_backs_up_old_code(self):
        target=self.base/'versions/journal-quality-assessment';install(target)
        marker=target/'.jqa-install.json';value=json.loads(marker.read_text(encoding='utf-8'))
        # Emulate a clean earlier installation missing one newly-added file.
        name=next(n for n in value['files'] if n=='README_EN.md')
        del value['files'][name];marker.write_text(json.dumps(value),encoding='utf-8')
        result=install(target,upgrade=True)
        self.assertTrue(Path(result['previous_code_backup']).is_dir())

    def test_relocate_rebinds_only_physical_paths_and_resumes(self):
        self.prep()
        run=self.base/'run'
        manifest=json.loads((run/'run_manifest.json').read_text(encoding='utf-8'))
        self.assertTrue(all('\\' not in n for n in manifest['frozen_sha256']))
        self.assertTrue(all('\\' not in n for n in manifest['core_sha256']))
        with ModelExecutor(run,SyntheticOnlyProvider()) as ex:ex.run(max_tasks=1)
        old=sqlite3.connect(run/'store/tasks.sqlite3');before=old.execute("select id,result from tasks where status='completed'").fetchall();old.close()
        moved=self.base/'新目录/run';moved.parent.mkdir();shutil.copytree(run,moved)
        relocate(moved,'run-api')
        with ModelExecutor(moved,SyntheticOnlyProvider()) as ex:
            result=ex.run();self.assertEqual(result['valid_comparisons'],6)
        new=sqlite3.connect(moved/'store/tasks.sqlite3');after=dict(new.execute("select id,result from tasks where status='completed'"));new.close()
        self.assertTrue(all(after[k]==v for k,v in before))

    def test_cleanup_only_explicit_managed_child(self):
        result=self.prep(provider='agent')
        with self.assertRaises(ValueError):main(['clean','--run',str(self.base),'--data-root',str(self.base),'--confirm-run-id','run'])
        main(['clean','--run',result['run_directory'],'--data-root',str(self.base),'--confirm-run-id','run'])
        self.assertFalse((self.base/'run').exists())

    def test_sensitive_scan_and_mock_refuses_real_titles(self):
        self.assertTrue(scan(ROOT)['passed'],scan(ROOT)['issues'])
        secret=self.base/'leak';secret.mkdir();(secret/'key.json').write_text(json.dumps(dict(api_key='not-a-public-fixture')),encoding='utf-8')
        self.assertFalse(scan(secret)['passed'])
        with self.assertRaises(ValueError):SyntheticOnlyProvider().call(dict(messages=[{},dict(content=json.dumps(dict(articles=[dict(title='Real research')])))]))

    def test_git_metadata_excluded_but_public_secrets_detected(self):
        root=self.base/'repo';root.mkdir()
        (root/'.git').mkdir();(root/'.git'/'object').write_bytes(bytes([255,0]))
        (root/'safe.txt').write_text('public',encoding='utf-8')
        self.assertTrue(scan(root)['passed'])
        (root/'secret.json').write_text(json.dumps(dict(api_key='real-secret-fixture')),encoding='utf-8')
        self.assertFalse(scan(root)['passed'])

    def test_output_must_not_enter_installed_code_tree(self):
        with self.assertRaises(ValueError):doctor(ROOT/'bad-output')

    def test_installed_script_can_uninstall_itself_without_touching_data(self):
        target=self.base/'journal-quality-assessment';install(target)
        data=self.base/'my-data';data.mkdir()
        p=subprocess.run([sys.executable,'-B',str(target/'scripts/install.py'),'--target',str(target),'--uninstall'],
                         capture_output=True,encoding='utf-8')
        self.assertEqual(p.returncode,0,p.stderr);self.assertFalse(target.exists());self.assertTrue(data.is_dir())

if __name__=='__main__':unittest.main()
