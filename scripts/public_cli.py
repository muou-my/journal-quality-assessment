"""Portable user interface. Algorithms live in the preserved core modules."""
from __future__ import annotations
import argparse
import csv
import json
import os
import platform
import shutil
import sqlite3
import sys
import tempfile
import uuid
from decimal import Decimal, ROUND_DOWN
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from scripts import jqa as legacy
from core.data_processing import load_csv
from core.model_executor import prepare_api_run, ModelExecutor, ProcessLock
from pilot.prepare_run import prepare_run
from pilot.safe_execution import atomic_bytes

VERSION='0.2.0'


def data_home():
    if os.name=='nt':return Path(os.environ.get('LOCALAPPDATA',Path.home()/'AppData/Local'))/'JournalQualityAssessment'
    if sys.platform=='darwin':return Path.home()/'Library/Application Support/JournalQualityAssessment'
    return Path(os.environ.get('XDG_DATA_HOME',Path.home()/'.local/share'))/'journal-quality-assessment'


def outside_install(path):
    value=Path(path).expanduser().resolve()
    if value==ROOT or value.is_relative_to(ROOT):raise ValueError('User data/output must be outside the installed Skill directory')
    return value


def write(path,value):
    return atomic_bytes(path,(json.dumps(value,ensure_ascii=False,indent=2)+'\n').encode('utf-8'))


def doctor(workdir=None):
    parent=outside_install(workdir or data_home()/'diagnostics');parent.mkdir(parents=True,exist_ok=True)
    checks=dict(python=sys.version_info>=(3,10),sqlite=bool(sqlite3.sqlite_version),utf8=True)
    with tempfile.TemporaryDirectory(dir=parent,prefix='jqa-') as directory:
        sample=Path(directory)/'中文 path.json'
        try:atomic_bytes(sample,'{"text":"中文😀"}\n'.encode('utf-8'));checks['atomic_no_clobber']=json.loads(sample.read_text(encoding='utf-8'))['text']=='中文😀'
        except OSError:checks['atomic_no_clobber']=False
    from pilot.versioning import resolve_versions
    resolve_versions(ROOT,'jqa.assessment.v1.2','jqa.rubric.v1.1')
    checks['version_hashes']=True
    return dict(version=VERSION,platform=platform.system(),checks=checks,passed=all(checks.values()),network_requests=0,
                default_data_directory=str(data_home()),runtime_dependencies='Python 3.10+ standard library',
                limitation='A host must expose local Python and filesystem tools; API mode additionally needs approved outbound HTTPS')


def prepare(args):
    source=Path(args.csv).expanduser().resolve();data=load_csv(source)
    if data.report['excluded_rows'] or len(data.articles)<2:raise ValueError('Review validation report; need at least two complete nonconflicting articles')
    count=len(data.articles);journals={a.journal_name for a in data.articles}
    if args.mode=='single' and len(journals)!=1:raise ValueError('Single mode requires exactly one journal')
    if args.mode=='multi' and len(journals)<2:raise ValueError('Multi mode requires at least two journals')
    run_id=args.run_id or 'jqa-'+uuid.uuid4().hex
    import re
    if not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9._-]{0,79}',run_id):raise ValueError('Use a short filesystem-safe run ID')
    output=outside_install(args.output or data_home()/'runs'/run_id)
    if output.exists():raise FileExistsError('New run directory required')
    configuration=legacy.read(ROOT/'pilot/run_config.example.json')
    configuration.update(evaluation_run_id=run_id,expected_articles=count,mode=args.mode,seed=args.seed,
        input_provenance_note=args.provenance,model_identity_note='User Agent identity unverified; unknown stays unknown',batch_size=20)
    with tempfile.TemporaryDirectory(prefix='jqa-prepare-') as tmp:
        tmp=Path(tmp);cfg=tmp/'run.json';cfg.write_text(json.dumps(configuration),encoding='utf-8')
        if args.provider=='agent':manifest=prepare_run(source,cfg,output)
        else:
            api=legacy.read(args.api_config or ROOT/'config/deepseek.example.json')
            if args.budget_usd is not None and args.budget_cny is not None:raise ValueError('Choose one budget currency')
            if args.budget_cny is not None:
                if not args.usd_to_cny or not args.fx_date:raise ValueError('CNY requires user-supplied --usd-to-cny and --fx-date; no guessed exchange rate')
                rate=Decimal(args.usd_to_cny);amount=Decimal(args.budget_cny)
                if not rate.is_finite() or rate<=0 or not amount.is_finite() or amount<0:raise ValueError('Invalid conversion')
                api['max_cost_usd']=str((amount/rate).quantize(Decimal('.000001'),rounding=ROUND_DOWN))
            elif args.budget_usd is not None:api['max_cost_usd']=args.budget_usd
            if args.concurrency is not None:api['concurrency']=args.concurrency
            base=tmp/'plan';prepare_run(source,cfg,base)
            api_cfg=tmp/'api.json';api_cfg.write_text(json.dumps(api),encoding='utf-8')
            manifest=prepare_api_run(base,api_cfg,output,run_id+'-api')
            # Final evaluator run ID is distinct from its ephemeral plan source.
            if args.budget_cny is not None:
                write(output/'reports/budget_conversion.json',dict(requested_cny=args.budget_cny,usd_to_cny=args.usd_to_cny,
                    fx_date=args.fx_date,max_cost_usd=api['max_cost_usd'],source='User-supplied conversion, not a verified live quote'))
            shutil.copytree(base/'validation',output/'validation')
    write(output/'.jqa-user-run.json',dict(version=VERSION,evaluation_run_id=manifest['evaluation_run_id']))
    return dict(run_directory=str(output),evaluation_run_id=manifest['evaluation_run_id'],articles=count,
                planned=manifest['planned_comparisons'],K=manifest['K'],provider=args.provider,executed=0)


def paid_disclosure(args):
    """Applied to BOTH public and legacy CLI routes before key input or network."""
    with ModelExecutor(args.run) as executor:
        estimate=executor.estimate()
        disclosure=dict(destination='https://api.deepseek.com/chat/completions',
            transmitted='Frozen instructions; two anonymous IDs, titles and abstracts; task/run/model routing metadata. No local DOI/journal mapping fields. Text itself can reveal identity.',
            privacy='Third-party processing follows your provider agreement; only submit data you are entitled to send.',
            next_batch_estimate_usd=estimate['batch_expected_usd'],all_pending_estimate_usd=estimate['all_expected_usd'],
            configured_max_cost_usd=executor.config['max_cost_usd'],note='Local estimates/reservations, not guaranteed invoices')
    print(json.dumps(disclosure,ensure_ascii=True,indent=2),file=sys.stderr)
    if not args.allow_paid:raise ValueError('Paid API disabled; inspect estimate before explicitly authorizing')
    if not getattr(args,'confirm_send',False):
        if not sys.stdin.isatty() or input('Type SEND to authorize the displayed transmission and estimated charges: ')!='SEND':
            raise ValueError('Explicit data-transfer consent required; automation must pass --confirm-send after review')


def demo(output):
    output=outside_install(output)
    if output.exists():raise FileExistsError('New demo output required')
    args=argparse.Namespace(csv=ROOT/'assets/synthetic_multi.csv',mode='multi',run_id='synthetic-demo-'+uuid.uuid4().hex[:12],
        output=output,provider='deepseek',seed=42,provenance='SYNTHETIC DEMO ONLY; not real scholarly judgments',
        api_config=None,budget_usd=None,budget_cny=None,usd_to_cny=None,fx_date=None,concurrency=None)
    result=prepare(args)
    from core.mock_provider import SyntheticOnlyProvider
    with ModelExecutor(output,SyntheticOnlyProvider()) as executor:executor.run()
    legacy.main(['export','--run',str(output),'--output',str(output/'demo_export')])
    for name in ('articles.csv','journals.csv'):
        path=output/'demo_export'/name
        with path.open(encoding='utf-8-sig',newline='') as f:
            reader=csv.DictReader(f);columns=list(reader.fieldnames);rows=list(reader)
        with path.open('w',encoding='utf-8-sig',newline='') as f:
            writer=csv.DictWriter(f,fieldnames=columns+['assessment_kind']);writer.writeheader()
            for row in rows:writer.writerow(dict(**row,assessment_kind='SYNTHETIC_DEMO_ONLY'))
    write(output/'SYNTHETIC_DEMO_ONLY.json',dict(not_real_research=True,network_requests=0))
    return dict(**result,demo_only=True,network_requests=0,export=str(output/'demo_export'))


def relocate(run,confirmation):
    """Audit ONLY physical DB bindings after moving a run; preserve scoring rows."""
    run=outside_install(run)
    manifest=legacy.read(run/'run_manifest.json')
    if confirmation!=manifest['evaluation_run_id']:raise ValueError('Confirm the exact run ID')
    with ProcessLock(run/'api/executor.lock'):
        stamp=uuid.uuid4().hex[:8];backup=run/'reports'/('move_'+stamp);backup.mkdir()
        updates=[]
        for relative,table,key,new in [('store/execution/execution.sqlite3','ledger_meta','task_store',str((run/'store/tasks.sqlite3').resolve())),
                                      ('api/requests.sqlite3','metadata','task_store',str((run/'store/tasks.sqlite3').resolve()))]:
            dbpath=run/relative
            if not dbpath.exists():continue
            source=sqlite3.connect(dbpath);copy=None
            try:
                copy=sqlite3.connect(backup/dbpath.name)
                source.backup(copy)
                if relative.startswith('store/'):
                    running=source.execute('SELECT count(*) FROM attempts a LEFT JOIN finishes f USING(attempt_id) WHERE f.attempt_id IS NULL').fetchone()[0]
                    if running:raise ValueError('Resolve running/unknown attempts before relocating')
                old=source.execute('SELECT value FROM '+table+' WHERE key=?',(key,)).fetchone()
                with source:
                    source.execute('UPDATE '+table+' SET value=? WHERE key=?',(new,key))
                    if relative.startswith('api/'):
                        for rid,body in source.execute('SELECT id,body_path FROM requests').fetchall():
                            expected=run/'api/requests'/f'{rid:06}'/'http.json'
                            source.execute('UPDATE requests SET body_path=? WHERE id=?',(str(expected),rid))
                updates.append(dict(database=relative,old_binding=old[0] if old else None,new_binding=new))
            finally:
                if copy:copy.close()
                source.close()
        write(backup/'audit.json',dict(evaluation_run_id=confirmation,updates=updates,method='Physical path rebind only; execution and accepted-score rows unchanged'))
    return dict(rebound=True,audit=str(backup/'audit.json'))


def main(argv=None):
    argv=list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in ('validate','status','next','start','import','fail','score','export','save-response','review-stop','safety-status','api-check','api-estimate','api-status','api-reconcile'):
        return legacy.main(argv)
    p=argparse.ArgumentParser(description='Portable Journal Quality Assessment '+VERSION)
    s=p.add_subparsers(dest='command',required=True)
    d=s.add_parser('doctor');d.add_argument('--workdir')
    d=s.add_parser('prepare');d.add_argument('--csv',required=True);d.add_argument('--mode',choices=['single','multi'],required=True);d.add_argument('--provider',choices=['agent','deepseek'],default='agent');d.add_argument('--output');d.add_argument('--run-id');d.add_argument('--seed',type=int,default=42);d.add_argument('--provenance',default='User-supplied CSV; screening and rights are the user responsibility');d.add_argument('--api-config');d.add_argument('--concurrency',type=int);d.add_argument('--budget-usd');d.add_argument('--budget-cny');d.add_argument('--usd-to-cny');d.add_argument('--fx-date')
    for name in ('run','resume'):
        d=s.add_parser(name);d.add_argument('--run',required=True);d.add_argument('--allow-paid',action='store_true');d.add_argument('--confirm-send',action='store_true');d.add_argument('--credentials');d.add_argument('--max-tasks',type=int)
    d=s.add_parser('demo');d.add_argument('--output',required=True)
    d=s.add_parser('relocate');d.add_argument('--run',required=True);d.add_argument('--confirm-run-id',required=True)
    d=s.add_parser('clean');d.add_argument('--run',required=True);d.add_argument('--data-root',required=True);d.add_argument('--confirm-run-id',required=True)
    d=s.add_parser('runs');d.add_argument('--data-root')
    a=p.parse_args(argv)
    if a.command=='doctor':return doctor(a.workdir)
    if a.command=='prepare':return prepare(a)
    if a.command=='demo':return demo(a.output)
    if a.command=='relocate':return relocate(a.run,a.confirm_run_id)
    if a.command=='runs':
        root=outside_install(a.data_root or data_home()/'runs')
        return [dict(directory=str(p.parent),evaluation_run_id=legacy.read(p)['evaluation_run_id']) for p in root.glob('*/run_manifest.json')]
    if a.command=='clean':
        root=outside_install(a.data_root);run=outside_install(a.run)
        if run==root or not run.is_relative_to(root) or Path(a.run).is_symlink():raise ValueError('Run must be a nonsymlink child of the explicit data root')
        if legacy.read(run/'.jqa-user-run.json')['evaluation_run_id']!=a.confirm_run_id:raise ValueError('Confirm managed run ID')
        with ProcessLock(run/'api/executor.lock'):
            log=run/'store/execution/execution.sqlite3'
            if log.exists():
                c=sqlite3.connect(log)
                try:running=c.execute('SELECT count(*) FROM attempts a LEFT JOIN finishes f USING(attempt_id) WHERE f.attempt_id IS NULL').fetchone()[0]
                finally:c.close()
                if running:raise ValueError('Resolve running work before cleanup')
        shutil.rmtree(run)  # Resolved target checked against the explicitly selected data root above.
        return dict(removed=True,evaluation_run_id=a.confirm_run_id)
    mapping=['api-run' if a.command=='run' else 'api-resume','--run',a.run]
    if a.allow_paid:mapping+=['--allow-paid']
    if a.confirm_send:mapping+=['--confirm-send']
    if a.credentials:mapping+=['--credentials',a.credentials]
    if a.max_tasks:mapping+=['--max-tasks',str(a.max_tasks)]
    return legacy.main(mapping)


def entry():
    try:
        value=main();print(json.dumps(value,ensure_ascii=True,indent=2))
        if isinstance(value,dict) and value.get('passed') is False:raise SystemExit(2)
    except (ValueError,OSError,sqlite3.Error,KeyError) as exc:
        print(json.dumps(dict(error=str(exc)),ensure_ascii=True),file=sys.stderr);raise SystemExit(2)

if __name__=='__main__':entry()
