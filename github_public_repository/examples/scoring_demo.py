"""Run with python -B -m examples.scoring_demo; synthetic local files only."""
import json
import tempfile
from pathlib import Path

from core.article_scoring import aggregate_article_scores
from core.journal_scoring import aggregate_journal_scores
from core.data_processing import load_csv
from core.pairwise_comparison import generate_comparison_plan
from core.task_manager import TaskManager, digest


def main():
    with tempfile.TemporaryDirectory(prefix='jqa-scoring-demo-') as directory:
        root=Path(directory)
        csv_path=root/'articles.csv'
        csv_path.write_text('doi,journal_name,title,abstract,year\n'
            '10.1234/0,A,Paper0,Synthetic abstract,2025\n'
            '10.1234/1,A,Paper1,Synthetic abstract,2025\n'
            '10.1234/2,B,Paper2,Synthetic abstract,2025\n'
            '10.1234/3,B,Paper3,Synthetic abstract,2025\n',encoding='utf-8')
        original=csv_path.read_bytes()
        data=load_csv(csv_path)
        plan=generate_comparison_plan(data.articles,mode='multi',seed=42)
        manager=TaskManager(root/'store')
        try:
            run='offline-demo-model-v1'
            manager.create_tasks(plan,data.articles,evaluation_run_id=run,
                                 model_name='synthetic-model',model_version='v1')
            manager.export_tasks(run,root/'tasks.jsonl')
            results=[]
            first_task_ids=[]
            # Only two of six comparisons initially return valid results.
            scores={(0,1):(4,1),(0,2):(2,3)}
            for left,right in ((0,1),(0,2),(0,3),(1,2),(1,3),(2,3)):
                tokens=['article-'+digest([run,f'10.1234/{i}'])[:24] for i in (left,right)]
                task=next(t['payload'] for t in manager.tasks(run)
                          if {a['article_id'] for a in t['payload']['articles']}==set(tokens))
                value={k:task[k] for k in ('task_id','evaluation_run_id','model_name','model_version','prompt_version')}
                value.update(schema_version='jqa.result.v1',articles=[])
                for token,score in zip(tokens,scores.get((left,right),(2,2))):
                    value['articles'].append(dict(article_id=token,originality=score,
                        significance=score,rigour=score,evidence_note='Synthetic demonstration, not a real judgment.',uncertainty='high'))
                results.append(value)
                if (left,right) in scores:
                    first_task_ids.append(task['task_id'])
            results_path=root/'results.jsonl'
            partial=[r for r in results if r['task_id'] in first_task_ids]
            bad=json.loads(json.dumps(next(r for r in results if r['task_id'] not in first_task_ids)))
            del bad['articles'][0]['rigour']
            partial.append(bad)
            results_path.write_text('\n'.join(json.dumps(r) for r in partial),encoding='utf-8')
            print('partial_import:',manager.import_results(run,results_path))
            event_by_task={}
            for _,payload,private in manager.db.execute('SELECT id,payload,private FROM tasks WHERE run=?',(run,)):
                event_by_task[json.loads(payload)['task_id']]=json.loads(private)['event_id']
            # This is synthetic execution evidence, not inferred from exports.
            executed=[event_by_task[r['task_id']] for r in partial]
            article=aggregate_article_scores(root/'store',run,data.articles,executed_event_ids=executed)
            journal=aggregate_journal_scores(article)
            def display(label,a,j):
                print(label,json.dumps(dict(
                    counts=a.run_statistics,
                    papers=[{k:p[k] for k in ('doi','journal_name','valid_evaluation_count','llm_ref','score_std')} for p in a.articles],
                    journals=[{k:r[k] for k in ('journal_name','total_article_count','scored_article_count','coverage','journal_score','article_score_std','evaluation_status')} for r in j.journals]),indent=2))
            display('PARTIAL',article,journal)
            manager.close()
            manager=TaskManager(root/'store')
            manager.export_tasks(run,root/'resume.jsonl',resume=True)
            # Import all six; the two prior accepted records are idempotent.
            results_path.write_text('\n'.join(json.dumps(r) for r in results),encoding='utf-8')
            print('full_import:',manager.import_results(run,results_path))
            complete=aggregate_article_scores(root/'store',run,data.articles,
                                             executed_event_ids=[e.event_id for e in plan.events])
            display('COMPLETE',complete,aggregate_journal_scores(complete))
            assert csv_path.read_bytes()==original
            assert len(plan.events)==6
        finally:
            manager.close()


if __name__=='__main__':
    main()
