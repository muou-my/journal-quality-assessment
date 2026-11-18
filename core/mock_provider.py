"""Explicit synthetic-only plumbing demo. Never evaluates user research."""
import json


class SyntheticOnlyProvider:
    def call(self,body):
        task=json.loads(body['messages'][1]['content'])
        if not all(a['title'].startswith('[SYNTHETIC]') for a in task['articles']):
            raise ValueError('Mock provider is restricted to packaged synthetic demonstration data')
        result={k:task[k] for k in ('task_id','evaluation_run_id','model_name','model_version','prompt_version')}
        result.update(schema_version='jqa.result.v1',articles=[dict(article_id=a['article_id'],originality=2,
            significance=2,rigour=2,evidence_note='SYNTHETIC DEMO fixture; not a model judgment or real research quality rating.',uncertainty='high') for a in task['articles']])
        envelope=dict(id='synthetic-demo',model='deepseek-flash',system_fingerprint='synthetic-only',
            usage=dict(prompt_tokens=1,completion_tokens=1),choices=[dict(finish_reason='stop',message=dict(content=json.dumps(result)))])
        return json.dumps(envelope,ensure_ascii=False).encode('utf-8')
