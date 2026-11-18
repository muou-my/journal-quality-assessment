"""Offline ten-paper integration run. No external services or model calls.

python -B -m examples.end_to_end_test --output artifacts/e2e_v02_20261111
The output directory must be empty; existing evaluation artifacts are never replaced.
"""
from __future__ import annotations

import argparse
import copy
import csv
import hashlib
import json
import math
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from fractions import Fraction
from pathlib import Path

from core.article_scoring import ScoringError, aggregate_article_scores
from core.data_processing import load_csv, write_quality_report
from core.journal_scoring import aggregate_journal_scores
from core.pairwise_comparison import generate_comparison_plan, summarize_events
from core.task_manager import TaskManager

DIMENSIONS = ('originality', 'significance', 'rigour')
RUN_METADATA = {
    'complete_model_a_v1_p1': ('synthetic-model-A', 'v1', 'synthetic-prompt-v1', 'codex'),
    'partial_then_recovered': ('synthetic-model-A', 'v1', 'synthetic-prompt-v1', 'codex'),
    'other_model': ('synthetic-model-B', 'v1', 'synthetic-prompt-v1', 'chatgpt'),
    'other_model_version': ('synthetic-model-A', 'v2', 'synthetic-prompt-v1', 'codex'),
    'other_prompt': ('synthetic-model-A', 'v1', 'synthetic-prompt-v2', 'codex'),
}


def timestamp():
    return datetime.now(timezone(timedelta(hours=8))).isoformat()


def json_text(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2)


def save_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json_text(value)+'\n', encoding='utf-8')


def save_jsonl(path, values):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(''.join(json.dumps(v, ensure_ascii=False)+'\n' for v in values), encoding='utf-8')


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def md_value(value):
    if value is None:
        return '—'
    if isinstance(value, float):
        return f'{value:.6f}'
    return str(value).replace('|', '\\|')


def table(rows, columns):
    return '\n'.join(['| '+' | '.join(columns)+' |',
        '| '+' | '.join('---' for _ in columns)+' |']+
        ['| '+' | '.join(md_value(row.get(c)) for c in columns)+' |' for row in rows])


def mock_result(task, constant=None):
    """A deterministic synthetic response generated only from the exported task."""
    indices=[int(article['title'].rsplit(' ',1)[1]) for article in task['articles']]
    ratings=[]
    for position,article in enumerate(task['articles']):
        own,other=indices[position],indices[1-position]
        scores=(constant,constant,constant) if constant is not None else (
            1+own%4, 1+(2*own+other)%4, 1+(own+2*other)%4)
        ratings.append(dict(article_id=article['article_id'],
            **dict(zip(DIMENSIONS,scores)),
            evidence_note='SYNTHETIC TEST RESPONSE: arithmetic fixture, not a scholarly judgment.',
            uncertainty='high'))
    return dict(schema_version='jqa.result.v1',articles=ratings,
        **{key:task[key] for key in ('task_id','evaluation_run_id','model_name','model_version','prompt_version')})


def verify_scores(check, label, article_report, journal_report, responses, valid_task_ids):
    """Independent Fraction-based oracle; does not use scoring module internals."""
    observations={a['article_id']:[] for a in article_report.articles}
    for response in responses:
        if response['task_id'] in valid_task_ids:
            for rating in response['articles']:
                observations[rating['article_id']].append(rating)
    expected={}
    good=True
    for paper in article_report.articles:
        rows=observations[paper['article_id']]
        good &= paper['valid_evaluation_count']==len(rows)
        if not rows:
            good &= all(paper[key] is None for key in ('llm_ref','score_std',*(d+'_mean' for d in DIMENSIONS)))
            expected[paper['article_id']]=None
            continue
        values=[Fraction(sum(r[d] for d in DIMENSIONS),3) for r in rows]
        average=sum(values)/len(values)
        std=math.sqrt(float(sum((x-average)**2 for x in values)/len(values)))
        dimensions={d:Fraction(sum(r[d] for r in rows),len(rows)) for d in DIMENSIONS}
        good &= math.isclose(paper['llm_ref'],float(average),abs_tol=1e-12)
        good &= math.isclose(paper['score_std'],std,abs_tol=1e-12)
        good &= all(math.isclose(paper[d+'_mean'],float(dimensions[d]),abs_tol=1e-12) for d in DIMENSIONS)
        good &= {s['task_id'] for s in paper['sources']}=={
            r['task_id'] for r in responses if r['task_id'] in valid_task_ids and
            any(a['article_id']==paper['article_id'] for a in r['articles'])}
        expected[paper['article_id']]=(average,dimensions)
    check(label+': own valid ratings, per-dimension means, LLM_REF, population SD and provenance',good)
    good=True
    for journal in journal_report.journals:
        papers=[a for a in article_report.articles if a['journal_name']==journal['journal_name']]
        scored=[expected[a['article_id']] for a in papers if expected[a['article_id']] is not None]
        good &= journal['total_article_count']==len(papers)
        good &= journal['scored_article_count']==len(scored)
        good &= math.isclose(journal['coverage'],len(scored)/len(papers))
        good &= journal['partial_sample']==(len(scored)<len(papers))
        if scored:
            average=sum(p[0] for p in scored)/len(scored)
            good &= math.isclose(journal['journal_score'],float(average),abs_tol=1e-12)
            std=math.sqrt(float(sum((p[0]-average)**2 for p in scored)/len(scored)))
            good &= math.isclose(journal['article_score_std'],std,abs_tol=1e-12)
            for dimension in DIMENSIONS:
                expected_mean=sum(p[1][dimension] for p in scored)/len(scored)
                good &= math.isclose(journal[dimension+'_mean'],float(expected_mean),abs_tol=1e-12)
        else:
            good &= journal['journal_score'] is None
    check(label+': equal independent-paper journal scores, dimension means, SD and coverage',good)


def run_integration(output_directory):
    root=Path(output_directory).resolve()
    if root.exists() and any(root.iterdir()):
        raise ValueError('Output directory must be empty; choose a new path')
    root.mkdir(parents=True,exist_ok=True)
    started=timestamp()
    repository=Path(__file__).resolve().parents[1]
    core_before={str(p.relative_to(repository)):sha(p) for p in sorted((repository/'core').glob('*.py'))}
    checks=[]
    operations=[]
    def check(name, passed):
        checks.append(dict(name=name,passed=bool(passed)))
    def operation(run,action,result):
        operations.append(dict(timestamp=timestamp(),evaluation_run_id=run,action=action,result=result))
        return result

    csv_path=root/'input/simulated_articles.csv'
    csv_path.parent.mkdir()
    with csv_path.open('w',newline='',encoding='utf-8-sig') as stream:
        writer=csv.DictWriter(stream,fieldnames=['doi','journal_name','title','abstract','year'])
        writer.writeheader()
        for index in range(10):
            writer.writerow(dict(doi=f'10.5555/sim{index:02}',
                journal_name='Synthetic Journal A' if index<5 else 'Synthetic Journal B',
                title=f'Synthetic paper {index:02}',
                abstract=f'Synthetic study {index:02}: fabricated text for offline software verification only.',year=2025))
    csv_before=sha(csv_path)
    data=load_csv(csv_path)
    (root/'validation').mkdir()
    write_quality_report(data,root/'validation/data_quality_report.json')
    check('CSV: 10 valid papers, two journals, no exclusions/conflicts',
        len(data.articles)==10 and len({a.journal_name for a in data.articles})==2 and
        not data.report['excluded_rows'] and not data.report['metadata_conflicts'])
    plan=generate_comparison_plan(data.articles,mode='multi',seed=42)
    plan_before=json_text(asdict(plan))
    save_json(root/'plan/comparison_plan.json',asdict(plan))
    plan_file_before=sha(root/'plan/comparison_plan.json')
    save_json(root/'plan/graph_statistics.json',plan.design_summary)
    pair_by_id={p.pair_id:p for p in plan.pairs}
    check('Graph: complete N=10, K=9, U=T=45, R=0',
        plan.k==9 and plan.planned_counts=={'U':45,'T':45,'R':0} and
        set(plan.article_comparison_counts.values())=={9})
    edge_sets={frozenset((p.article_a,p.article_b)) for p in plan.pairs}
    check('Graph: no self-loops, duplicate edges or repeated comparison events',
        len(edge_sets)==45 and all(len(p)==2 for p in edge_sets) and
        len({e.event_id for e in plan.events})==45 and
        len({e.pair_id for e in plan.events})==45 and all(e.repetition_index==1 for e in plan.events))
    check('Graph: independent adjacency degree reconstruction equals 9 for all papers',
        all(sum(a.doi in edge for edge in edge_sets)==9 for a in data.articles))
    save_json(root/'plan/executed_event_statistics.json',summarize_events(plan,[e.event_id for e in plan.events]))

    manager=TaskManager(root/'store')
    run_outputs={}
    exported={}
    snapshots={}
    try:
        for run,(model,version,prompt,agent) in RUN_METADATA.items():
            directory=root/'runs'/run
            directory.mkdir(parents=True)
            manager.create_tasks(plan,data.articles,evaluation_run_id=run,model_name=model,
                                 model_version=version,prompt_version=prompt,agent=agent)
            manifest=operation(run,'export',manager.export_tasks(run,directory/'tasks.jsonl'))
            tasks=[json.loads(line) for line in (directory/'tasks.jsonl').read_text(encoding='utf-8').splitlines()]
            exported[run]=tasks
            check(run+': exported 45 unique tasks with one distinct pair each',
                len(tasks)==45 and len({t['task_id'] for t in tasks})==45 and
                len({frozenset(a['article_id'] for a in t['articles']) for t in tasks})==45 and
                len(manifest['task_ids'])==45)
            expected_article_keys={'article_id','title','abstract'}
            export_text=(directory/'tasks.jsonl').read_text(encoding='utf-8')
            check(run+': anonymous export has no DOI/journal/author/affiliation metadata',
                '10.5555/' not in export_text and 'Synthetic Journal' not in export_text and
                all(set(a)==expected_article_keys for t in tasks for a in t['articles']))
            rows=manager.db.execute('SELECT id,private FROM tasks WHERE run=?',(run,)).fetchall()
            private={task_id:json.loads(raw) for task_id,raw in rows}
            event_by_id={e.event_id:e for e in plan.events}
            check(run+': each task private mapping matches exactly one original plan edge',
                len({p['event_id'] for p in private.values()})==45 and
                all(p['pair_id']==event_by_id[p['event_id']].pair_id and
                    {a['doi'] for a in p['articles']}=={
                        pair_by_id[p['pair_id']].article_a,pair_by_id[p['pair_id']].article_b}
                    for p in private.values()))
            constant={'other_model':4,'other_model_version':1,'other_prompt':2}.get(run)
            responses=[mock_result(t,constant) for t in tasks]
            save_jsonl(directory/'responses_valid.jsonl',responses)
            submitted=copy.deepcopy(responses)
            invalid_ids=set()
            if run=='partial_then_recovered':
                for task,response in zip(tasks,submitted):
                    indices={int(a['title'].rsplit(' ',1)[1]) for a in task['articles']}
                    if 9 in indices or indices=={0,1}:
                        del response['articles'][0]['rigour']
                        invalid_ids.add(response['task_id'])
                check('Failure fixture: exactly 10 invalid tasks (all paper9 edges plus 0/1)',len(invalid_ids)==10)
            save_jsonl(directory/'responses_submitted.jsonl',submitted)
            # Reject another run's results before accepting this run's own results.
            if run in ('other_model','other_prompt'):
                cross=operation(run,'reject_other_run_results',manager.import_results(
                    run,root/'runs/complete_model_a_v1_p1/responses_valid.jsonl'))
                check(run+': all 45 foreign task results rejected',cross['errors']==45 and not manager.results(run))
            imported=operation(run,'import',manager.import_results(run,directory/'responses_submitted.jsonl'))
            check(run+': import count and validation errors match fixture',
                  imported['completed']==45-len(invalid_ids) and imported['errors']==len(invalid_ids))
            # Explicit local mock-execution evidence; never inferred from export/status.
            execution=[dict(evaluation_run_id=run,task_id=t['task_id'],
                comparison_event_id=private[t['task_id']]['event_id'],executor='local_synthetic_fixture',
                submitted=True,timestamp=timestamp()) for t in tasks]
            save_jsonl(directory/'execution_events.jsonl',execution)
            article=aggregate_article_scores(root/'store',run,data.articles,
                executed_event_ids=[x['comparison_event_id'] for x in execution])
            journal=aggregate_journal_scores(article)
            verify_scores(check,run,article,journal,responses,{r['task_id'] for r in responses}-invalid_ids)
            check(run+': planned/executed/valid and rating counts distinguished',
                  article.run_statistics['planned_comparison_count']==45 and
                  article.run_statistics['executed_comparison_count']==45 and
                  article.run_statistics['valid_comparison_count']==45-len(invalid_ids) and
                  article.run_statistics['valid_article_rating_count']==2*(45-len(invalid_ids)))
            check(run+': every accepted source retains correct run, model and prompt version',
                all(s['evaluation_run_id']==run and s['model_name']==model and
                    s['model_version']==version and s['prompt_version']==prompt
                    for p in article.articles for s in p['sources']))
            snapshots[run]=(article,journal)
            def persist_scores(folder,a,j):
                save_json(folder/'article_scores.json',asdict(a))
                save_json(folder/'journal_scores.json',asdict(j))
                (folder/'article_scores.md').write_text('# Synthetic article scores\n\n'+table(a.articles,
                    ['doi','journal_name','planned_comparison_count','executed_comparison_count',
                     'valid_evaluation_count','originality_mean','significance_mean','rigour_mean','llm_ref','score_std'])+'\n',encoding='utf-8')
                (folder/'journal_scores.md').write_text('# Synthetic journal scores\n\n'+table(j.journals,
                    ['journal_name','total_article_count','scored_article_count','coverage','originality_mean',
                     'significance_mean','rigour_mean','journal_score','article_score_std','evaluation_status'])+'\n',encoding='utf-8')
            persist_scores(directory,article,journal)
            before=asdict(article)
            replay_file=directory/'responses_valid_only.jsonl'
            valid_responses=[r for r in responses if r['task_id'] not in invalid_ids]
            save_jsonl(replay_file,valid_responses)
            replay=operation(run,'repeat_import',manager.import_results(run,replay_file))
            after=aggregate_article_scores(root/'store',run,data.articles,
                executed_event_ids=[x['comparison_event_id'] for x in execution])
            check(run+': repeated import is idempotent and scores/provenance unchanged',
                replay['idempotent']==len(valid_responses) and replay['completed']==0 and asdict(after)==before)
            if run=='complete_model_a_v1_p1':
                conflict=copy.deepcopy(responses[0])
                conflict['articles'][0]['originality']=1+conflict['articles'][0]['originality']%4
                save_jsonl(directory/'conflicting_result.jsonl',[conflict])
                result=operation(run,'conflict_import',manager.import_results(run,directory/'conflicting_result.jsonl'))
                check('Completed result conflict quarantined without changing scoring',result['conflicts']==1 and
                    asdict(aggregate_article_scores(root/'store',run,data.articles,
                        executed_event_ids=[e.event_id for e in plan.events]))==before)
                resume=operation(run,'resume_completed',manager.export_tasks(run,directory/'resume.jsonl',resume=True))
                check('Completed tasks are excluded from resume export',not resume['task_ids'])
            if invalid_ids:
                failure_journals={j['journal_name']:j for j in journal.journals}
                counts=[p['valid_evaluation_count'] for p in article.articles]
                check('Failed tasks yield 7/8/0 valid-rating counts and B coverage=80%',
                    sorted(counts)==[0,7,7,8,8,8,8,8,8,8] and
                    failure_journals['Synthetic Journal A']['coverage']==1 and
                    failure_journals['Synthetic Journal B']['coverage']==0.8 and
                    failure_journals['Synthetic Journal B']['partial_sample'])
                # An explicit counterexample makes a pooled-events implementation fail.
                a_papers=[p for p in article.articles if p['journal_name']=='Synthetic Journal A']
                pooled=sum(s['originality'] for p in a_papers for s in p['sources'])/sum(p['valid_evaluation_count'] for p in a_papers)
                correct=failure_journals['Synthetic Journal A']['originality_mean']
                check('Dimension counterexample: equal-paper originality=2.2, NOT pooled 85/38',
                    math.isclose(correct,2.2) and math.isclose(pooled,85/38) and not math.isclose(correct,pooled))
                save_json(directory/'equal_weight_counterexample.json',dict(equal_paper_originality=correct,
                    incorrect_pooled_originality=pooled,expected_equal_paper='11/5',incorrect_pooled='85/38'))
                manager.close()
                manager=TaskManager(root/'store')
                resume=operation(run,'resume_after_restart',manager.export_tasks(run,directory/'resume.jsonl',resume=True))
                check('Restart resumes only 10 failed tasks; 35 completed tasks preserved',set(resume['task_ids'])==invalid_ids)
                recovered=operation(run,'recover_all_valid',manager.import_results(run,directory/'responses_valid.jsonl'))
                recovered_a=aggregate_article_scores(root/'store',run,data.articles,executed_event_ids=[e.event_id for e in plan.events])
                recovered_j=aggregate_journal_scores(recovered_a)
                recovery_directory=directory/'recovered'
                recovery_directory.mkdir()
                persist_scores(recovery_directory,recovered_a,recovered_j)
                save_jsonl(recovery_directory/'additional_attempts.jsonl',[
                    dict(evaluation_run_id=run,task_id=r['task_id'],comparison_event_id=private[r['task_id']]['event_id'],
                         executor='local_synthetic_fixture',timestamp=timestamp(),retry_of_task_id=r['task_id'])
                    for r in responses if r['task_id'] in invalid_ids])
                verify_scores(check,'recovered',recovered_a,recovered_j,responses,{r['task_id'] for r in responses})
                check('Recovery completes remaining tasks without adding independent papers',
                    recovered['completed']==10 and recovered['idempotent']==35 and
                    recovered_a.run_statistics['total_article_count']==10 and
                    all(j['coverage']==1 for j in recovered_j.journals))
            save_json(directory/'errors_and_conflicts.json',manager.errors(run))
            run_outputs[run]=dict(metadata=dict(model_name=model,model_version=version,prompt_version=prompt,agent=agent),
                initial_statistics=article.run_statistics,initial_import=imported,repeat_import=replay,
                synthetic_execution_attempts=55 if invalid_ids else 45)

        main_a,main_j=snapshots['complete_model_a_v1_p1']
        check('All model/version/prompt runs have disjoint task IDs',
            sum(len({t['task_id'] for t in tasks}) for tasks in exported.values())==
            len({t['task_id'] for tasks in exported.values() for t in tasks}))
        for run,constant in (('other_model',4),('other_model_version',1),('other_prompt',2)):
            check(run+': journal scores remain isolated at synthetic constant '+str(constant),
                all(j['journal_score']==constant for j in snapshots[run][1].journals))
        mixed=copy.deepcopy(main_a)
        mixed.articles[0]['sources'][0]['prompt_version']='different-prompt'
        rejected=False
        try:
            aggregate_journal_scores(mixed)
        except ScoringError:
            rejected=True
        check('In-memory mixed Prompt source report is explicitly rejected',rejected)
        without_evidence=aggregate_article_scores(root/'store','complete_model_a_v1_p1',data.articles)
        check('No independent execution evidence means unknown actual count, not export count',
            without_evidence.run_statistics['executed_comparison_count'] is None)
    finally:
        manager.close()

    core_after={str(p.relative_to(repository)):sha(p) for p in sorted((repository/'core').glob('*.py'))}
    check('Original CSV bytes unchanged',sha(csv_path)==csv_before)
    check('Original plan object and persisted plan unchanged',json_text(asdict(plan))==plan_before and
        sha(root/'plan/comparison_plan.json')==plan_file_before)
    check('Every pre-existing core module remains byte-for-byte unchanged',core_before==core_after)
    save_json(root/'run_records.json',dict(started_at=started,finished_at=timestamp(),
        python_execution='offline synthetic fixtures only',operations=operations,runs=run_outputs,
        integrity=dict(csv_before=csv_before,csv_after=sha(csv_path),plan_before=plan_file_before,
            plan_after=sha(root/'plan/comparison_plan.json'),core_before=core_before,core_after=core_after)))
    summary=dict(passed=all(c['passed'] for c in checks),check_count=len(checks),
                 checks=checks,planned_graph=plan.design_summary,runs=run_outputs,
                 live_model_calls=0,api_cost=0,output_directory=str(root))
    save_json(root/'summary.json',summary)
    failed=[c['name'] for c in checks if not c['passed']]
    partial_a,partial_j=snapshots['partial_then_recovered']
    report=['# Journal Quality Assessment V0.2 — offline end-to-end test',
        '',f'Started: {started}',f'Finished: {timestamp()}',
        '',f'Overall: {"PASS" if summary["passed"] else "FAIL"}; {len(checks)} explicit checks; live API calls=0; API cost=0.',
        'All papers, abstracts, model names and scores are synthetic. No scientific validity or model calibration is inferred.',
        '', '## Graph and workflow', '',
        '10 valid papers, 5 per journal; N=10, K=9, all 45 unordered pairs, no loops/repeats.',
        'All articles have exactly 9 planned comparisons; target=100,000, generated comparisons=45.',
        'CSV validation -> graph -> anonymous JSONL -> synthetic JSONL -> strict import -> article scores -> equal-paper journal scores.',
        '', '## Complete-run article scores', '',
        table(main_a.articles,['doi','journal_name','valid_evaluation_count','originality_mean','significance_mean','rigour_mean','llm_ref','score_std']),
        '', '## Complete-run journal scores', '',
        table(main_j.journals,['journal_name','total_article_count','scored_article_count','coverage','originality_mean','significance_mean','rigour_mean','journal_score','article_score_std']),
        '', '## Failure scenario, before recovery', '',
        '10 malformed task results are rejected; 35 comparisons and 70 article ratings remain valid. All 45 events were explicitly simulated.',
        'Paper9 has no valid score; papers0/1 have 7 valid ratings; papers2–8 have 8. A coverage=100%, B coverage=80%.',
        table(partial_j.journals,['journal_name','scored_article_count','coverage','originality_mean','significance_mean','rigour_mean','journal_score','evaluation_status']),
        '', '### Explicit dimension-weighting check', '',
        'Journal A originality: first average each paper, then equal-weight the 5 papers = 11/5 = 2.2.',
        'Incorrect event pooling would give 85/38 = 2.236842. The production implementation returns 2.2.',
        'All three dimension means, composite scores and population SDs were independently checked with Fraction-based arithmetic.',
        '', '## Imports and recovery', '',
        'Complete run: 45 accepted, then 45 idempotent reimports; one altered result quarantined; completed-task resume exports zero tasks.',
        'Failure run: 35 accepted, 10 failed; restart exports only those 10; recovery accepts 10, skips 35 idempotently; article count stays 10.',
        'Different model name, model version and Prompt version each have independent run IDs, tasks and result queries.',
        '', '## Run statistics', '',
        table([dict(run=run,**value['initial_statistics'],synthetic_attempts=value['synthetic_execution_attempts']) for run,value in run_outputs.items()],
            ['run','planned_comparison_count','executed_comparison_count','valid_comparison_count','valid_article_rating_count','scored_article_count','synthetic_attempts']),
        'Failure-run synthetic attempts=55 includes 10 recovery attempts; initial snapshot has 45 distinct executed events. Recovery still has only 45 unique comparison events.',
        '', '## Checks', '',
        table([dict(check=c['name'],status='PASS' if c['passed'] else 'FAIL') for c in checks],['check','status']),
        '', '## Issues and recommendations', '',
        ('No scoring or integration defect was detected in these scenarios.' if not failed else 'Failed checks: '+repr(failed)),
        '1. Actual execution counts are not independently logged by TaskManager. This test supplies explicit mock execution records; production should add a durable attempt ledger, including retries. Do not infer execution from exported/failed status.',
        '2. Coverage=100% only means every eligible paper has at least one valid score, not that every planned rating completed. Inspect per-paper planned/valid counts and task statistics.',
        '3. Preserve the original valid-article roster per run. The scoring interface requires it to compute coverage; especially for zero-task runs its completeness cannot be reconstructed from the current store.',
        '4. Journal identity currently uses validated journal_name; future canonical ISSN/alias mapping would prevent accidental splits. The synthetic fixture uses unambiguous names.',
        '5. This test cannot establish content-rating reliability, cross-model score comparability, or exactly-once human evaluation. No real model, remote permissions, concurrent export crash or production-scale job was exercised.',
        '', '## Artifacts and rerun', '',
        '- input/: original synthetic CSV; validation/: quality report.',
        '- plan/: full immutable plan, graph statistics, explicitly simulated execution statistics.',
        '- runs/<run>/: anonymous tasks, synthetic response JSONL, scoring JSON/Markdown, audit errors; recovery snapshots remain separate.',
        '- store/tasks.sqlite3: persistent task/result/audit store (private local mappings; do not upload).',
        '- run_records.json: operations, isolation metadata and before/after SHA-256 checks; summary.json: machine-readable checks.',
        '', '`python -B -m examples.end_to_end_test --output artifacts/new-empty-directory`',
        '`python -B -m unittest discover -s tests -v`',
        '', 'No scoring method or existing core implementation was changed. No BT_score or Bayesian shrinkage was calculated.',
    ]
    (root/'end_to_end_report.md').write_text('\n'.join(report)+'\n',encoding='utf-8')
    return summary


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',required=True)
    args=parser.parse_args()
    summary=run_integration(args.output)
    print(json.dumps(dict(passed=summary['passed'],check_count=summary['check_count'],
                         output_directory=summary['output_directory'],live_model_calls=0),indent=2))
    if not summary['passed']:
        raise SystemExit(1)


if __name__=='__main__':
    main()

