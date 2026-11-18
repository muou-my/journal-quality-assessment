"""Prepare a frozen real-paper evaluation bundle; no model execution or API client.

python -B -m pilot.prepare_run --csv PATH --config PATH --output NEW_DIRECTORY
"""
import argparse
import hashlib
import json
import shutil
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from core.data_processing import load_csv, write_quality_report
from core.execution_logger import ExecutionLogger
from core.pairwise_comparison import generate_comparison_plan
from core.task_manager import TaskManager
from .versioning import resolve_versions, validate_identity


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_json(path,value):
    path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('x',encoding='utf-8') as stream:
        json.dump(value,stream,ensure_ascii=False,indent=2)
        stream.write('\n')


def prepare_run(csv_path,config_path,output):
    """Requires the configured valid, nonconflicting records; existing bundles never overwritten."""
    source=Path(csv_path).resolve()
    config_path=Path(config_path).resolve()
    config=json.loads(config_path.read_text(encoding='utf-8-sig'))
    required={'evaluation_run_id','agent','model_name','model_version','prompt_version','rubric_version',
              'mode','expected_articles','target_comparisons','seed','model_identity_note','input_provenance_note'}
    optional={'prompt_sha256','rubric_sha256','model_identity_verification','batch_size'}
    if not required <= set(config) or set(config)-required-optional:
        raise ValueError('Configuration keys must match run_config.example.json')
    for field in ('evaluation_run_id','model_name','model_version','prompt_version','rubric_version','input_provenance_note','model_identity_note'):
        if not isinstance(config[field],str) or not config[field].strip() or 'REPLACE_' in config[field]:
            raise ValueError(f'Fill configuration field: {field}')
    if config['agent'] not in ('codex','chatgpt') or config['mode'] not in ('single','multi'):
        raise ValueError('Invalid agent/mode')
    if type(config['expected_articles']) is not int or config['expected_articles'] < 2:
        raise ValueError('expected_articles must be an integer of at least 2')
    batch_size=config.get('batch_size',50)
    if type(batch_size) is not int or not 1<=batch_size<=50:
        raise ValueError('batch_size must be an integer from 1 to 50')
    if type(config['target_comparisons']) is not int or config['target_comparisons']!=100000:
        raise ValueError('First pilot uses the confirmed default target 100000')
    if type(config['seed']) is not int:
        raise ValueError('seed must be an integer')
    repository=Path(__file__).resolve().parents[1]
    if config['prompt_version']=='jqa.assessment.v1.2':
        for key in ('prompt_sha256','rubric_sha256'):
            value=config.get(key)
            if not isinstance(value,str) or len(value)!=64 or any(c not in '0123456789abcdef' for c in value):
                raise ValueError('New pilot config requires explicit lowercase SHA-256 pins')
    versions=resolve_versions(repository,config['prompt_version'],config['rubric_version'],
        prompt_sha256=config.get('prompt_sha256'),rubric_sha256=config.get('rubric_sha256'))
    identity=validate_identity(config)
    if config['prompt_version']=='jqa.assessment.v1.2' and any(k not in config for k in ('prompt_sha256','rubric_sha256')):
        raise ValueError('New pilot config must pin both instruction hashes')
    prompt,rubric=versions['prompt']['path'],versions['rubric']['path']
    source_hash=sha(source)
    data=load_csv(source)
    if data.report['total_rows']!=config['expected_articles'] or len(data.articles)!=config['expected_articles'] or data.report['excluded_rows'] or data.report['missing_columns'] or data.report['duplicate_headers']:
        raise ValueError('CSV must contain exactly expected_articles eligible nonduplicated rows; review load_csv(...).report first')
    plan=generate_comparison_plan(data.articles,mode=config['mode'],budget=100000,seed=config['seed'])
    root=Path(output).resolve()
    if root.exists():
        raise FileExistsError('Use a new run directory; do not overwrite an existing pilot')
    if sha(source)!=source_hash:
        raise RuntimeError('Input CSV changed during validation')
    core_hashes={p.relative_to(repository).as_posix():sha(p) for p in sorted((repository/'core').glob('*.py'))}
    root.mkdir(parents=True)
    for name in ('frozen','exchange','responses','reports','validation'):
        (root/name).mkdir()
    shutil.copyfile(source,root/'frozen/input.csv')
    shutil.copyfile(config_path,root/'frozen/config.json')
    shutil.copyfile(prompt,root/'frozen/assessment_prompt.md')
    shutil.copyfile(rubric,root/'frozen/scoring_rubric.md')
    shutil.copyfile(repository/'SKILL.md',root/'frozen/SKILL.md')
    write_quality_report(data,root/'validation/data_quality_report.json')
    write_json(root/'frozen/valid_articles.json',[asdict(a) for a in data.articles])
    write_json(root/'frozen/comparison_plan.json',asdict(plan))
    write_json(root/'reports/plan_summary.json',plan.design_summary)
    manager=TaskManager(root/'store')
    try:
        manager.create_tasks(plan,data.articles,evaluation_run_id=config['evaluation_run_id'],
            agent=config['agent'],model_name=config['model_name'],model_version=config['model_version'],
            prompt_version=config['prompt_version'],
            prompt_text=versions['prompt']['text'],rubric_text=versions['rubric']['text'],
            instruction_metadata=dict(rubric_version=config['rubric_version'],
                prompt_sha256=versions['prompt']['sha256'],rubric_sha256=versions['rubric']['sha256']))
        manager.export_tasks(config['evaluation_run_id'],root/'exchange/tasks_initial.jsonl')
        exported=[json.loads(line) for line in (root/'exchange/tasks_initial.jsonl').read_text(encoding='utf-8').splitlines()]
        (root/'exchange/first_task.jsonl').write_text(json.dumps(exported[0],ensure_ascii=False)+'\n',encoding='utf-8')
        first=exported[0]
        if any(versions['prompt']['text'] not in t['instructions'] or
               versions['rubric']['text'] not in t['instructions'] for t in exported):
            raise RuntimeError('Every exported task must embed the complete selected versions')
        if len({t['instructions'] for t in exported})!=1:
            raise RuntimeError('All pilot tasks must share the same complete instructions')
        result={k:first[k] for k in ('task_id','evaluation_run_id','model_name','model_version','prompt_version')}
        result.update(schema_version='jqa.result.v1',articles=[dict(article_id=a['article_id'],
            originality=None,significance=None,rigour=None,evidence_note='',uncertainty=None) for a in first['articles']])
        write_json(root/'exchange/RESULT_FORMAT_ONLY_DO_NOT_IMPORT.json',result)
        # Creation only: preparation produces zero execution starts/valid results.
        with ExecutionLogger(root/'store') as logger:
            write_json(root/'reports/preparation_execution_summary.json',logger.summary(config['evaluation_run_id']))
    finally:
        manager.close()
    frozen_hashes={p.relative_to(root).as_posix():sha(p) for p in sorted((root/'frozen').iterdir())}
    manifest=dict(schema_version='jqa.pilot.manifest.v1',
        prepared_at=datetime.now(timezone(timedelta(hours=8))).isoformat(),
        evaluation_run_id=config['evaluation_run_id'],agent=config['agent'],
        model_name=config['model_name'],model_version=config['model_version'],
        model_identity_note=config['model_identity_note'],prompt_version=config['prompt_version'],
        rubric_version=config['rubric_version'],
        prompt_sha256=versions['prompt']['sha256'],rubric_sha256=versions['rubric']['sha256'],
        instruction_source_paths={k:v['path'].relative_to(repository).as_posix() for k,v in versions.items()},
        model_identity_verification=identity,
        input_provenance_note=config['input_provenance_note'],source_csv=str(source),source_csv_sha256=source_hash,
        frozen_sha256=frozen_hashes,core_sha256=core_hashes,
        task_instruction_sha256=hashlib.sha256(first['instructions'].encode()).hexdigest(),
        exported_tasks_sha256=sha(root/'exchange/tasks_initial.jsonl'),
        task_ids=[t['task_id'] for t in exported],planned_comparisons=len(plan.events),K=plan.k,
        expected_articles=config['expected_articles'],batch_size=batch_size,
        actual_execution_attempts_at_preparation=0,valid_ratings_at_preparation=0,
        status='prepared_not_evaluated',live_model_calls=0)
    if sha(source)!=source_hash or sha(root/'frozen/input.csv')!=source_hash:
        raise RuntimeError('Input CSV changed during preparation')
    if sha(root/'frozen/assessment_prompt.md')!=manifest['prompt_sha256'] or sha(root/'frozen/scoring_rubric.md')!=manifest['rubric_sha256']:
        raise RuntimeError('Frozen instruction hashes differ from selected versions')
    write_json(root/'run_manifest.json',manifest)
    return manifest


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--csv',required=True)
    parser.add_argument('--config',required=True)
    parser.add_argument('--output',required=True)
    args=parser.parse_args()
    manifest=prepare_run(args.csv,args.config,args.output)
    print(json.dumps(dict(status=manifest['status'],planned=manifest['planned_comparisons'],K=manifest['K'],actual=0,valid=0,
                          output=str(Path(args.output).resolve())),indent=2))


if __name__=='__main__':
    main()

