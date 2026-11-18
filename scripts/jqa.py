"""Journal Quality Assessment operations. Paid API requires explicit api-run authorization."""
import argparse
import csv
import hashlib
import json
import re
import shutil
import sqlite3
import sys
from collections import Counter
from dataclasses import asdict
from pathlib import Path
from statistics import mean, pstdev

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from core.data_processing import load_csv, Article
from core.task_manager import TaskManager, digest
from core.execution_logger import ExecutionLogger
from core.article_scoring import aggregate_article_scores, DIMENSIONS
from core.journal_scoring import aggregate_journal_scores
from pilot.prepare_run import prepare_run
from pilot.safe_execution import SafeBatchExecution as BatchExecution


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('x', encoding='utf-8') as f:
        json.dump(value, f, ensure_ascii=False, indent=2)
    return str(path.resolve())


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def quality(articles, run=None):
    journals = {a.journal_name for a in articles}
    flags = []
    for a in articles:
        text = a.title + '\n' + a.abstract
        reasons = []
        if re.search(r'会议报道|会议纪要|征稿|征文|启事|书评|卷首语|conference report|call for papers|editorial|book review', a.title, re.I):
            reasons.append('possible_nonresearch_article')
        if a.abstract.startswith('<正>'):
            reasons.append('possible_body_excerpt_not_abstract')
        if any(j.casefold() in text.casefold() for j in journals):
            reasons.append('explicit_journal_identity')
        if re.search(r'影响因子|impact factor|E-mail:|@', text, re.I):
            reasons.append('possible_reputation_or_identity_signal')
        if reasons:
            flags.append(dict(source_row=a.source_row, title=a.title, reasons=reasons,
                article_id='article-' + digest([run, a.doi])[:24] if run else None))
    return dict(article_count=len(articles), journal_count=len(journals),
        journal_counts=dict(Counter(a.journal_name for a in articles)),
        year_counts=dict(Counter(a.year for a in articles)), flags=flags,
        requires_review=bool(flags), automated_screen_only=True,
        limitation='Heuristic screening is not proof of research eligibility or complete anonymization. Agent must review titles/abstracts; never silently remove frozen articles.')


def frozen(root):
    root = Path(root).resolve()
    m = read(root/'run_manifest.json')
    for name, expected in m['frozen_sha256'].items():
        p = (root/name).resolve()
        if not p.is_relative_to(root/'frozen') or sha(p) != expected:
            raise ValueError('Frozen file/hash mismatch: ' + name)
    if m['prompt_version'] != 'jqa.assessment.v1.2' or m['rubric_version'] != 'jqa.rubric.v1.1':
        raise ValueError('This entrypoint requires the confirmed v1.2/v1.1 run')
    for kind, name in [('prompt','assessment_prompt.md'), ('rubric','scoring_rubric.md')]:
        if sha(root/'frozen'/name) != m[kind+'_sha256']:
            raise ValueError('Instruction pin mismatch')
    return root, m


def state(root, m):
    manager = TaskManager(root/'store')
    try:
        tasks = manager.tasks(m['evaluation_run_id'])
        with ExecutionLogger(root/'store') as log:
            attempts = log.recover_run(m['evaluation_run_id'])
            summary = log.summary(m['evaluation_run_id'])
    finally:
        manager.close()
    return tasks, attempts, summary


def latest_attempts(attempts):
    latest = {}
    for a in attempts:
        if a['task_id'] not in latest or a['attempt_number'] > latest[a['task_id']]['attempt_number']:
            latest[a['task_id']] = a
    return latest


def run_quality(root, m, tasks):
    roster = [Article(**a) for a in read(root/'frozen/valid_articles.json')]
    report = quality(roster, m['evaluation_run_id'])
    ids = {a['article_id'] for a in report['flags']}
    report['affected_task_ids'] = [t['payload']['task_id'] for t in tasks
        if ids & {a['article_id'] for a in t['payload']['articles']}]
    report['affected_unique_task_count'] = len(report['affected_task_ids'])
    report['strict_anonymity_established'] = False if ids else None
    return report


def select(tasks, attempts, size):
    latest = latest_attempts(attempts)
    ready, blocked = [], []
    for t in tasks:
        if t['status'] == 'completed':
            continue
        p = t['payload']; a = latest.get(p['task_id'])
        # Successful output must be reconciled, never blindly re-evaluated.
        if a and a['status'] in ('running','succeeded'):
            blocked.append(dict(task_id=p['task_id'], attempt_id=a['attempt_id'], reason=a['status']))
        else:
            ready.append(dict(task=p, retry_of=a['attempt_id'] if a else None))
    return ready[:size], blocked


def summaries(root, m, tasks, summary):
    q = run_quality(root, m, tasks)
    counts = Counter(a['article_id'] for t in tasks for a in t['payload']['articles'])
    scored = {a['article_id'] for t in tasks if t['status']=='completed' for a in t['payload']['articles']}
    result = {k:v for k,v in summary.items() if k!='per_task'}
    result.update(article_count=q['article_count'], journal_count=q['journal_count'],
        remaining_comparisons=len(tasks)-summary['valid_comparison_count'],
        comparison_coverage=summary['valid_comparison_count']/len(tasks) if tasks else 0,
        scored_article_count=len(scored), article_coverage=len(scored)/q['article_count'],
        K=m['K'], planned_participation_by_article=dict(counts), research_quality=q)
    return result


def scoring(root, m):
    roster = [Article(**a) for a in read(root/'frozen/valid_articles.json')]
    with ExecutionLogger(root/'store') as log:
        s = log.summary(m['evaluation_run_id'])
        if s['validated_results_without_success_log_count']:
            raise ValueError('Completed results lack successful execution evidence')
        events = log.executed_event_ids(m['evaluation_run_id'])
    a = aggregate_article_scores(root/'store', m['evaluation_run_id'], roster, executed_event_ids=events)
    a.provenance.update(rubric_version=m['rubric_version'], prompt_sha256=m['prompt_sha256'],
        rubric_sha256=m['rubric_sha256'], frozen_sha256=m['frozen_sha256'],
        model_identity_verification=m['model_identity_verification'], run_manifest=str(root/'run_manifest.json'))
    j = aggregate_journal_scores(a)
    complete = s['valid_comparison_count']==s['planned_comparison_count'] and not s['attempt_status_counts']['running']
    # Supplemental diagnostics; no changes to existing score aggregation.
    dist = {d:dict(Counter(str(v[d]) for p in a.articles for v in p['sources'])) for d in DIMENSIONS}
    stability = []
    for p in a.articles:
        sources = p['sources']
        stability.append(dict(article_id=p['article_id'], count=len(sources),
            dimension_population_std={d:pstdev(v[d] for v in sources) if sources else None for d in DIMENSIONS},
            composite_population_std=p['score_std'],
            stability_estimable=len(sources)>=2))
    diagnostics = dict(dimension_instance_distributions=dist, per_article_stability=stability,
        dimension_modal_share={d:max(map(int,values.values()))/sum(values.values()) if values else None for d,values in dist.items()},
        interpretation='Descriptive partner-context variability, not causal context effects or model calibration. SD=0 for one rating does not establish stability.')
    return dict(status='complete' if complete else 'partial_not_final', articles=asdict(a), journals=asdict(j), diagnostics=diagnostics)


def batch_for(b, task_id):
    for row in b.plan['batches']:
        if task_id in row['task_ids']:
            return row['batch_number']
    raise ValueError('Unknown task ID')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest='command', required=True)
    p=subs.add_parser('validate');p.add_argument('--csv',required=True);p.add_argument('--output',required=True)
    p=subs.add_parser('prepare');p.add_argument('--csv',required=True);p.add_argument('--config',required=True);p.add_argument('--output',required=True)
    for command in ('status','next','resume','start','import','fail','score','export','save-response','review-stop','safety-status'):
        p=subs.add_parser(command);p.add_argument('--run',required=True)
        if command in ('status','next','resume','score','export'):p.add_argument('--output',required=True)
        if command in ('next','resume'):p.add_argument('--batch-size',type=int,default=20)
        if command in ('start','import','fail','save-response'):p.add_argument('--task-id',required=True)
        if command in ('import','fail','save-response'):p.add_argument('--attempt-id',required=True)
        if command=='save-response':
            p.add_argument('--input',required=True);p.add_argument('--response',required=True)
        if command=='review-stop':
            p.add_argument('--evidence',required=True);p.add_argument('--retryable-attempt',action='append',default=[])
        if command=='start':
            p.add_argument('--retry-of');p.add_argument('--accept-quality-risks',action='store_true')
        if command=='import':p.add_argument('--response',required=True)
        if command=='fail':p.add_argument('--reason',required=True);p.add_argument('--interrupted',action='store_true')
        if command=='export':p.add_argument('--allow-partial',action='store_true')
    p=subs.add_parser('api');p.add_argument('--run',required=True)
    p=subs.add_parser('api-check');p.add_argument('--config',required=True)
    p=subs.add_parser('api-prepare');p.add_argument('--source-run',required=True);p.add_argument('--config',required=True);p.add_argument('--output',required=True);p.add_argument('--run-id',required=True)
    p=subs.add_parser('api-reconcile');p.add_argument('--run',required=True);p.add_argument('--request-id',required=True,type=int);p.add_argument('--evidence',required=True);p.add_argument('--delivery-reviewed',action='store_true')
    for command in ('api-estimate','api-status','api-run','api-resume'):
        p=subs.add_parser(command);p.add_argument('--run',required=True)
        if command in ('api-run','api-resume'):
            p.add_argument('--credentials');p.add_argument('--allow-paid',action='store_true');p.add_argument('--confirm-send',action='store_true');p.add_argument('--max-tasks',type=int)
    args=parser.parse_args(argv)
    if args.command.startswith('api-'):
        from core.model_executor import api_cli
        if args.command in ('api-run','api-resume'):
            from scripts.public_cli import paid_disclosure
            paid_disclosure(args)
        return api_cli(args)
    if args.command=='api':
        raise ValueError('API adapter is reserved, not implemented; use Agent file mode. No network request made.')
    if args.command=='validate':
        data=load_csv(args.csv)
        result=dict(data_quality=data.report,research_quality=quality(data.articles))
        write(args.output,result)
        return result
    if args.command=='prepare':
        c=read(args.config)
        if c['prompt_version']!='jqa.assessment.v1.2' or c['rubric_version']!='jqa.rubric.v1.1':
            raise ValueError('Prepare requires Prompt v1.2 and Rubric v1.1')
        result=prepare_run(args.csv,args.config,args.output)
        data=load_csv(args.csv)
        write(Path(args.output)/'validation/research_quality.json',quality(data.articles,result['evaluation_run_id']))
        return dict(evaluation_run_id=result['evaluation_run_id'],status=result['status'],planned=result['planned_comparisons'],K=result['K'])
    root,m=frozen(args.run)
    if args.command in ('safety-status','review-stop'):
        with BatchExecution(root) as b:
            if args.command=='review-stop':
                b.review_stop(evidence=args.evidence,retryable_attempts=args.retryable_attempt)
            return b.safety_status()
    if args.command=='save-response':
        with BatchExecution(root) as b:
            number=batch_for(b,args.task_id)
            try:
                text=Path(args.input).read_bytes().decode('utf-8',errors='strict')
            except (OSError,UnicodeError) as exc:
                b.halt('response_input',args.task_id,args.attempt_id,exc)
                raise
            return dict(response=b.save_response(number,args.task_id,args.attempt_id,args.response,text))
    if args.command in ('start','import','fail'):
        if args.command=='start':
            tasks,attempts,s=state(root,m)
            q=run_quality(root,m,tasks)
            if q['requires_review'] and not args.accept_quality_risks:
                raise ValueError('Review quality report and existing user authorization before start; use --accept-quality-risks only for explicitly retained risks')
        with BatchExecution(root) as b:
            number=batch_for(b,args.task_id)
            if args.command=='start':
                result=b.start_task(number,args.task_id,retry_of=args.retry_of)
            elif args.command=='import':
                result=b.complete_task(number,args.task_id,args.attempt_id,args.response)
            else:
                result=b.fail_task(number,args.task_id,args.attempt_id,reason=args.reason,interrupted=args.interrupted)
            # Each mutation checkpoints after its committed operation. Unique names.
            import uuid
            snapshot=root/'reports'/('checkpoint_'+uuid.uuid4().hex+'.json')
            write(snapshot,b.batch_status(number))
            return result
    tasks,attempts,s=state(root,m)
    if args.command=='status':
        result=summaries(root,m,tasks,s);write(args.output,result);return result
    if args.command in ('next','resume'):
        with BatchExecution(root) as b:
            if b.safety_status()['open_halts']:
                raise ValueError('Run stopped; review safety-status and reconcile incident before dispatch')
        if not 1<=args.batch_size<=50:raise ValueError('batch-size must be 1..50')
        ready,blocked=select(tasks,attempts,args.batch_size)
        out=Path(args.output);out.mkdir(parents=True,exist_ok=False)
        task_path=out/'tasks.jsonl'
        with task_path.open('x',encoding='utf-8') as f:
            for row in ready:f.write(json.dumps(row['task'],ensure_ascii=False)+'\n')
        result=dict(evaluation_run_id=m['evaluation_run_id'],task_file=str(task_path.resolve()),exported_count=len(ready),
            blocked=blocked,required_retry_of={r['task']['task_id']:r['retry_of'] for r in ready if r['retry_of']},
            execution_starts_created=0,task_file_sha256=sha(task_path))
        write(out/'dispatch.json',result);return result
    result=scoring(root,m)
    if args.command=='score':write(args.output,result);return dict(status=result['status'],output=str(Path(args.output).resolve()))
    if result['status']!='complete' and not args.allow_partial:
        raise ValueError('Run incomplete; --allow-partial explicitly exports a nonfinal report')
    out=Path(args.output).resolve()
    if out.is_relative_to(root/'responses'):
        raise ValueError('Export cannot be nested inside original responses')
    out.mkdir(parents=True,exist_ok=False)
    write(out/'scores.json',result)
    write(out/'progress.json',summaries(root,m,tasks,s))
    write(out/'provenance.json',m)
    for name,rows in [('articles',result['articles']['articles']),('journals',result['journals']['journals'])]:
        columns=[k for k in rows[0] if not isinstance(rows[0][k],(dict,list,tuple))] if rows else []
        with (out/(name+'.csv')).open('x',encoding='utf-8-sig',newline='') as f:
            writer=csv.DictWriter(f,fieldnames=columns);writer.writeheader()
            for row in rows:writer.writerow({k:row[k] for k in columns})
    with ExecutionLogger(root/'store') as log:log.export_jsonl(m['evaluation_run_id'],out/'execution_log.jsonl')
    db=sqlite3.connect((root/'store/tasks.sqlite3').as_uri()+'?mode=ro',uri=True)
    try:
        with (out/'accepted_results.jsonl').open('x',encoding='utf-8') as f:
            for raw, in db.execute('SELECT result FROM tasks WHERE run=? AND status=? ORDER BY id',(m['evaluation_run_id'],'completed')):f.write(raw+'\n')
    finally:db.close()
    # Preserve raw bytes and invalid/unimported output too, for a portable export.
    shutil.copytree(root/'responses',out/'raw_responses')
    write(out/'raw_response_inventory.json',[dict(source_path=str(p.resolve()),exported_path=str((out/'raw_responses'/p.relative_to(root/'responses')).resolve()),sha256=sha(p)) for p in sorted((root/'responses').rglob('*.jsonl'))])
    manager=TaskManager(root/'store')
    try:write(out/'import_errors.json',manager.errors(m['evaluation_run_id']))
    finally:manager.close()
    (out/'report.md').write_text('# Journal quality assessment\n\nStatus: '+result['status']+'\n\nValid comparisons: '+str(s['valid_comparison_count'])+'/'+str(s['planned_comparison_count'])+'\n\nSee articles.csv, journals.csv, scores.json, progress.json and provenance.json. Missing scores remain blank/null. Partial results are not final journal estimates. Research-quality flags and affected task IDs are in progress.json. SD is population SD; a single observation does not establish stability. This is a REF-inspired content assessment, not an official REF rating.\n',encoding='utf-8')
    return dict(status=result['status'],output=str(out.resolve()))


if __name__=='__main__':
    try:
        result=main()
        # Avoid dumping the entire task corpus or private mapping to conversation.
        if isinstance(result,dict) and 'planned_participation_by_article' in result:
            result={k:v for k,v in result.items() if k not in ('planned_participation_by_article','research_quality')}
        print(json.dumps(result,ensure_ascii=True,indent=2))
    except (ValueError,OSError,KeyError,sqlite3.Error) as exc:
        print(json.dumps(dict(error=str(exc)),ensure_ascii=True),file=sys.stderr)
        sys.exit(2)
