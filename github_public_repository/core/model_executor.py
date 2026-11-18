"""Journaled, recoverable API execution. Does not change scoring mathematics.

HTTP workers never touch SQLite. One dispatcher owns state/imports, guarded by an
OS process lock. Each request has its own attempt, receipt, usage and reservation.
Only explicit CLI --allow-paid can activate the production transport.
"""
from __future__ import annotations

import hashlib
import json
import math
import shutil
import sqlite3
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from dataclasses import fields
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from core.data_processing import Article
from core.pairwise_comparison import ComparisonPlan, Pair, ComparisonEvent
from core.result_parser import decode_json, parse_result
from core.task_manager import TaskManager
from core.providers.deepseek_provider import DeepSeekProvider, ProviderError, read_key
from pilot.safe_execution import SafeBatchExecution, atomic_bytes

VERSION='jqa.api-executor.v1'
DEFAULT_CONFIG=Path(__file__).resolve().parents[1]/'config/deepseek.example.json'


def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()
def read(path):return json.loads(Path(path).read_text(encoding='utf-8-sig'))
def now():return datetime.now(timezone.utc).isoformat()
def dump(path,value):
    return atomic_bytes(path,(json.dumps(value,ensure_ascii=False,indent=2)+'\n').encode('utf-8'))


def config_from(path):
    value=read(path)
    defaults=read(DEFAULT_CONFIG)
    if not isinstance(value,dict) or set(value)!=set(defaults):
        raise ValueError('API config fields must match config/deepseek.example.json; secrets belong in a separate ignored credential file')
    if value['provider']!='deepseek' or value['base_url']!='https://api.deepseek.com':
        raise ValueError('Only official DeepSeek is supported')
    if value['thinking'] not in ('disabled','enabled') or value['model_version']!='unknown':
        raise ValueError('Use disabled/enabled thinking and unknown unverified deployment version')
    if value['model'] not in ('deepseek-flash','deepseek-v4-pro'):
        raise ValueError('Unsupported official model')
    for k in ('max_tokens','timeout_seconds','concurrency','checkpoint_every','max_total_tasks','input_reserve_tokens','expected_output_tokens'):
        if type(value[k]) is not int or value[k]<1:raise ValueError('Positive integer required: '+k)
    if value['max_tokens']>393216 or value['concurrency']>32 or value['checkpoint_every']!=20:
        raise ValueError('max_tokens<=393216, concurrency<=32 and checkpoint_every=20 required')
    if type(value['max_retries']) is not int or not 0<=value['max_retries']<=10:
        raise ValueError('max_retries must be 0..10')
    if not isinstance(value['backoff_seconds'],(int,float)) or not math.isfinite(value['backoff_seconds']) or not 0<=value['backoff_seconds']<=120:
        raise ValueError('Invalid backoff_seconds')
    for amount in [value['max_cost_usd'],*value['prices_usd_per_million'].values()]:
        try:d=Decimal(str(amount))
        except Exception:raise ValueError('Invalid monetary value') from None
        if not d.is_finite() or d<0:raise ValueError('Invalid monetary value')
    if set(value['prices_usd_per_million'])!={'input_miss','input_hit','output'}:
        raise ValueError('Invalid price fields')
    if not isinstance(value['accepted_response_models'],list) or not value['accepted_response_models'] or any(not isinstance(x,str) or not x for x in value['accepted_response_models']):
        raise ValueError('accepted_response_models required')
    value['max_cost_usd']=str(Decimal(str(value['max_cost_usd'])))
    value['prices_usd_per_million']={k:str(Decimal(str(v))) for k,v in value['prices_usd_per_million'].items()}
    return value


def prepare_api_run(source_run, config_path, output, evaluation_run_id):
    """New TaskManager run from the EXACT frozen roster and pair plan; no replanning."""
    source=Path(source_run).resolve();root=Path(output).resolve();cfg=config_from(config_path)
    original=read(source/'run_manifest.json')
    if not evaluation_run_id or evaluation_run_id==original['evaluation_run_id']:
        raise ValueError('Use a fresh evaluation_run_id')
    if root.exists():raise FileExistsError('New API run directory required')
    for name,h in original['frozen_sha256'].items():
        if sha(source/name)!=h:raise ValueError('Source frozen hash mismatch')
    plan_data=read(source/'frozen/comparison_plan.json')
    plan=ComparisonPlan(**{**plan_data,'pairs':tuple(Pair(**x) for x in plan_data['pairs']),
                         'events':tuple(ComparisonEvent(**x) for x in plan_data['events'])})
    roster=[Article(**a) for a in read(source/'frozen/valid_articles.json')]
    if len(plan.events)!=len(plan.pairs) or len(roster)!=plan.independent_article_count:
        raise ValueError('Source plan inconsistent')
    root.mkdir(parents=True)
    for name in ('frozen','exchange','responses','reports','api'): (root/name).mkdir()
    for name in ('input.csv','valid_articles.json','comparison_plan.json','assessment_prompt.md','scoring_rubric.md','SKILL.md'):
        shutil.copyfile(source/'frozen'/name,root/'frozen'/name)
    dump(root/'frozen/api_config.json',cfg)
    run_cfg=read(source/'frozen/config.json')
    run_cfg.update(evaluation_run_id=evaluation_run_id,model_name=cfg['model'],model_version='unknown',
                   model_identity_note='DeepSeek official API; exact deployed version remains unverified',
                   input_provenance_note='Exact roster and pair plan cloned from '+original['evaluation_run_id'])
    # The existing agent field is a legacy TaskManager file-format label, NOT the
    # evaluator identity. All new evaluation provenance is provider=deepseek/API.
    run_cfg['model_identity_verification']=dict(checked_at=None,environment_visible_name=cfg['model'],
        environment_visible_version='unknown',version_information_source='not_exposed',verification_scope='unverified',
        evidence_basis='unavailable',evidence_references=[])
    dump(root/'frozen/config.json',run_cfg)
    prompt=(root/'frozen/assessment_prompt.md').read_bytes().decode('utf-8-sig')
    rubric=(root/'frozen/scoring_rubric.md').read_bytes().decode('utf-8-sig')
    manager=TaskManager(root/'store')
    try:
        ids=manager.create_tasks(plan,roster,evaluation_run_id=evaluation_run_id,
            model_name=cfg['model'],model_version='unknown',agent=run_cfg['agent'],
            prompt_version=original['prompt_version'],prompt_text=prompt,rubric_text=rubric,
            instruction_metadata={k:original[k] for k in ('rubric_version','prompt_sha256','rubric_sha256')})
        manager.export_tasks(evaluation_run_id,root/'exchange/tasks_initial.jsonl')
    finally:manager.close()
    repo=Path(__file__).resolve().parents[1]
    manifest={**{k:original[k] for k in ('prompt_version','rubric_version','prompt_sha256','rubric_sha256','K','expected_articles')},
        'schema_version':'jqa.api.run.v1','evaluation_run_id':evaluation_run_id,'agent':run_cfg['agent'],
        'execution_mode':'api','provider':'deepseek','model_name':cfg['model'],'model_version':'unknown',
        'model_identity_verification':run_cfg['model_identity_verification'],
        'prepared_at':now(),'executor_version':VERSION,'source_run_id':original['evaluation_run_id'],
        'source_run_manifest_sha256':sha(source/'run_manifest.json'),
        'source_csv_sha256':sha(root/'frozen/input.csv'),'source_pair_plan_sha256':sha(root/'frozen/comparison_plan.json'),
        'api_config_sha256':sha(root/'frozen/api_config.json'),
        'frozen_sha256':{p.relative_to(root).as_posix():sha(p) for p in (root/'frozen').iterdir()},
        'core_sha256':{p.relative_to(repo).as_posix():sha(p) for p in (repo/'core').glob('*.py')},
        'provider_code_sha256':sha(Path(__file__).parent/'providers/deepseek_provider.py'),
        'task_ids':ids,'planned_comparisons':len(ids),'batch_size':20,
        'exported_tasks_sha256':sha(root/'exchange/tasks_initial.jsonl'),'live_model_calls':0,
        'status':'prepared_not_evaluated'}
    dump(root/'run_manifest.json',manifest)
    return manifest


class ProcessLock:
    def __init__(self,path):self.path=Path(path)
    def __enter__(self):
        self.path.parent.mkdir(parents=True,exist_ok=True)
        self.stream=self.path.open('a+b');self.stream.seek(0,2)
        if not self.stream.tell():self.stream.write(b'0');self.stream.flush()
        self.stream.seek(0)
        try:
            if __import__('os').name=='nt':
                import msvcrt
                msvcrt.locking(self.stream.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(self.stream.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        except OSError:
            self.stream.close();raise ValueError('Another API executor owns this run') from None
        return self
    def __exit__(self,*args):self.stream.close()


class ModelExecutor:
    def __init__(self,run,provider=None,*,sleeper=time.sleep):
        self.root=Path(run).resolve();self.manifest=read(self.root/'run_manifest.json')
        if self.manifest.get('provider')!='deepseek' or self.manifest.get('execution_mode')!='api':
            raise ValueError('API executor refuses Agent runs; create an isolated API run first')
        self.config=config_from(self.root/'frozen/api_config.json')
        self.provider=provider;self.sleep=sleeper;self.stop=threading.Event()
        for name,h in self.manifest['frozen_sha256'].items():
            if sha(self.root/name)!=h:raise ValueError('Frozen API run changed')
        repo=Path(__file__).resolve().parents[1]
        for name,h in self.manifest['core_sha256'].items():
            if sha(repo/name)!=h:raise ValueError('Frozen engine code changed; audit compatibility before execution')
        if sha(Path(__file__).parent/'providers/deepseek_provider.py')!=self.manifest['provider_code_sha256']:
            raise ValueError('Frozen provider code changed')
        self.control=SafeBatchExecution(self.root)
        self.run_id=self.manifest['evaluation_run_id']
        self.db=sqlite3.connect(self.root/'api/requests.sqlite3');self.db.row_factory=sqlite3.Row
        self.db.execute('''CREATE TABLE IF NOT EXISTS requests(
            id INTEGER PRIMARY KEY,task TEXT NOT NULL,attempt TEXT NOT NULL UNIQUE,state TEXT NOT NULL,
            reserved TEXT NOT NULL,cost TEXT,usage TEXT,body_path TEXT NOT NULL,error_kind TEXT,
            http_status INTEGER,not_before REAL NOT NULL DEFAULT 0,created TEXT NOT NULL,
            response_model TEXT,system_fingerprint TEXT)''');self.db.commit()
        self.db.execute('CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL)')
        binding=dict(run_id=self.run_id,config_sha256=self.manifest['api_config_sha256'],
                     task_store=str((self.root/'store/tasks.sqlite3').resolve()))
        existing=dict(self.db.execute('SELECT key,value FROM metadata'))
        if (existing and existing!=binding) or (not existing and self.db.execute('SELECT count(*) FROM requests').fetchone()[0]):
            self.close();raise ValueError('API request ledger belongs to another run/configuration or requires a migration audit')
        with self.db:
            for key,value in binding.items():self.db.execute('INSERT OR IGNORE INTO metadata VALUES (?,?)',(key,value))
        self.numbers={t:i for i,t in enumerate(self.manifest['task_ids'],1)}

    def close(self):self.db.close();self.control.close()
    def __enter__(self):return self
    def __exit__(self,*args):self.close()
    def _task(self,task_id):
        number=next(b['batch_number'] for b in self.control.plan['batches'] if task_id in b['task_ids'])
        return number,self.control._task(number,task_id)[0]
    def _update(self,rid,**values):
        with self.db:
            self.db.execute('UPDATE requests SET '+','.join(k+'=?' for k in values)+' WHERE id=?',(*values.values(),rid))
    def _price(self,input_tokens,output_tokens,hit=0):
        p=self.config['prices_usd_per_million'];d=Decimal
        return (d(input_tokens-hit)*d(p['input_miss'])+d(hit)*d(p['input_hit'])+d(output_tokens)*d(p['output']))/d(1000000)
    def reservation(self,body):
        # Deliberately conservative local reservation, NOT an exact tokenizer or
        # guarantee against a provider's price changes/hidden token accounting.
        tokens=max(self.config['input_reserve_tokens'],len(json.dumps(body,ensure_ascii=False).encode('utf-8'))+512)
        return self._price(tokens,self.config['max_tokens'])
    def status(self):
        rows=[dict(r) for r in self.db.execute('SELECT * FROM requests ORDER BY id')]
        used=sum((Decimal(r['cost']) for r in rows if r['cost'] is not None),Decimal(0))
        reserve=sum((Decimal(r['reserved']) for r in rows if r['cost'] is None),Decimal(0))
        usage=[json.loads(r['usage']) for r in rows if r['usage']]
        task_counts=dict(self.control.manager.db.execute('SELECT status,count(*) FROM tasks WHERE run=? GROUP BY status',(self.run_id,)))
        return dict(evaluation_run_id=self.run_id,provider='deepseek',model=self.config['model'],model_version='unknown',
            planned_comparisons=self.manifest['planned_comparisons'],valid_comparisons=task_counts.get('completed',0),
            valid_article_ratings=2*task_counts.get('completed',0),task_status_counts=task_counts,
            http_request_records=len(rows),request_states={s:sum(r['state']==s for r in rows) for s in {r['state'] for r in rows}},
            receipt_files_saved=sum(Path(r['body_path']).is_file() for r in rows),
            confirmed_http_error_responses=sum(r['http_status'] is not None for r in rows),
            usage_reported_requests=len(usage),prompt_tokens=sum(u['prompt_tokens'] for u in usage),
            completion_tokens=sum(u['completion_tokens'] for u in usage),
            usage_based_estimated_usd=str(used),missing_usage_or_inflight_reserved_usd=str(reserve),
            budget_accounted_usd=str(used+reserve),max_cost_usd=self.config['max_cost_usd'],
            billing_note='Provider-reported usage plus configured-price estimates, NOT verified billed requests or final invoices; missing-usage requests retain conservative reserves',
            safety=self.control.safety_status())
    def estimate(self,limit=20):
        pending=[t for t in self.control.manager.tasks(self.run_id) if t['status']!='completed']
        costs=[]
        for row in pending:
            body=DeepSeekProvider.request(row['payload'],self.config)
            approx=math.ceil(len(json.dumps(body,ensure_ascii=False))/4)
            costs.append((self._price(approx,self.config['expected_output_tokens']),self.reservation(body)))
        return dict(pending_tasks=len(pending),batch_tasks=min(limit,len(costs)),
            batch_expected_usd=str(sum((x[0] for x in costs[:limit]),Decimal(0))),
            batch_reservation_usd=str(sum((x[1] for x in costs[:limit]),Decimal(0))),
            all_expected_usd=str(sum((x[0] for x in costs),Decimal(0))),
            estimator='Characters/4 heuristic, not actual DeepSeek tokenization; cache misses at configured prices',
            status=self.status())
    def checkpoint(self,reason):
        name='checkpoint_'+uuid.uuid4().hex+'.json'
        dump(self.root/'api'/name,dict(reason=reason,timestamp=now(),**self.status()))
        return str(self.root/'api'/name)
    def _halt(self,stage,row,exc):
        self.stop.set()
        self.control.halt('api_'+stage,row['task'],row['attempt'],exc,
                          request_id=row['id'],response_path=row['body_path'])
    def _usage(self,row,envelope):
        u=envelope.get('usage')
        if not isinstance(u,dict):raise ValueError('Missing usage; retain budget reserve and stop')
        for k in ('prompt_tokens','completion_tokens'):
            if type(u.get(k)) is not int or u[k]<0:raise ValueError('Invalid API usage')
        hit=u.get('prompt_cache_hit_tokens',u.get('prompt_tokens_details',{}).get('cached_tokens',0))
        if type(hit) is not int or not 0<=hit<=u['prompt_tokens']:raise ValueError('Invalid cache usage')
        price=self._price(u['prompt_tokens'],u['completion_tokens'],hit)
        self._update(row['id'],usage=json.dumps(u),cost=str(price),response_model=envelope.get('model'),
                     system_fingerprint=envelope.get('system_fingerprint'))
        if price>Decimal(row['reserved']):raise ValueError('Usage exceeded local budget reservation; stop and reconcile')
    def _accept(self,row):
        number,task=self._task(row['task'])
        envelope=decode_json(Path(row['body_path']).read_bytes().decode('utf-8'))
        self._usage(row,envelope)
        if envelope.get('model') not in self.config['accepted_response_models']:
            raise ValueError('Provider response model differs from frozen allowed identities')
        first=self.db.execute('SELECT response_model,system_fingerprint FROM requests WHERE response_model IS NOT NULL AND id!=? ORDER BY id LIMIT 1',(row['id'],)).fetchone()
        if first and (first[0]!=envelope.get('model') or first[1]!=envelope.get('system_fingerprint')):
            raise ValueError('Provider model/fingerprint changed within run; stop, do not mix')
        choices=envelope.get('choices')
        if not isinstance(choices,list) or len(choices)!=1:raise ValueError('Exactly one completion required')
        choice=choices[0];content=choice.get('message',{}).get('content')
        if not isinstance(content,str):raise ValueError('No text response')
        content_path=Path(row['body_path']).with_name('content.txt')
        raw=content.encode('utf-8',errors='strict')
        if not content_path.exists():atomic_bytes(content_path,raw)
        elif content_path.read_bytes()!=raw:raise ValueError('Conflicting archived content')
        if choice.get('finish_reason')!='stop':raise ValueError('Incomplete/filtered model output')
        parsed=parse_result(content,task)  # Never coerce scores, repair JSON or fill routing.
        normalized=Path(row['body_path']).with_name('result.jsonl')
        encoded=(json.dumps(parsed,ensure_ascii=False)+'\n').encode('utf-8')
        if not normalized.exists():atomic_bytes(normalized,encoded)
        elif normalized.read_bytes()!=encoded:raise ValueError('Conflicting normalized result')
        result=self.control.complete_task(number,row['task'],row['attempt'],normalized)
        self._update(row['id'],state='completed')
        return result
    def _worker(self,body,path,task):
        try:
            if self.stop.is_set():raise ProviderError('cancelled_before_send')
            raw=self.provider.call(body)
            atomic_bytes(path,raw)  # Persist original HTTP bytes before parsing.
            envelope=decode_json(raw.decode('utf-8'))
            task_content=envelope.get('choices',[{}])[0].get('message',{}).get('content')
            # Detect structural/score failures on the worker so no later dispatch
            # can ignore a completed bad response while another request is slow.
            if not isinstance(task_content,str) or not task_content.strip():raise ValueError('Empty completion')
            if envelope['choices'][0].get('finish_reason')!='stop':raise ValueError('Truncated completion')
            parse_result(task_content,task)
            return raw
        except ProviderError as exc:
            if not exc.retryable or exc.uncertain:self.stop.set()
            raise
        except BaseException:
            self.stop.set();raise

    def _recover(self):
        """Import saved receipts first; never resend a possibly delivered POST."""
        for item in self.db.execute("SELECT * FROM requests WHERE state IN ('sent','response_saved','unknown','storage_error') ORDER BY id").fetchall():
            row=dict(item)
            if Path(row['body_path']).is_file():
                try:self._accept(row)
                except Exception as exc:
                    self._halt('recovery',row,ValueError('Saved API receipt requires review'));raise ValueError('Saved receipt recovery failed; no requests issued') from exc
            else:
                self._halt('unknown_delivery',row,ValueError('POST may have executed; inspect provider request history/original receipt'))
                raise ValueError('Uncertain delivery blocks automatic replay')
        if self.control.safety_status()['open_halts']:raise ValueError('Review persistent run stop before issuing requests')
        api_attempts={r[0]:r[1] for r in self.db.execute('SELECT attempt,state FROM requests')}
        for a in self.control._history():
            task_status=self.control.manager.db.execute('SELECT status FROM tasks WHERE run=? AND id=?',(self.run_id,a['task_id'])).fetchone()[0]
            if task_status=='completed':continue
            if a['status']=='running' or api_attempts.get(a['attempt_id'])!='http_retryable':
                raise ValueError('Unreconciled attempt; no automatic retry')

    def reconcile(self,request_id,*,evidence,delivery_reviewed=False):
        """Operator-authorized retry disposition, NEVER an API call or score fix.

        Unknown delivery needs explicit confirmation that prior work has ended and
        its original response is unrecoverable. Conservative cost hold is retained.
        """
        if not isinstance(evidence,str) or not evidence.strip():raise ValueError('Specific audit evidence required')
        with ProcessLock(self.root/'api/executor.lock'):
            record=self.db.execute('SELECT * FROM requests WHERE id=?',(request_id,)).fetchone()
            if record is None:raise ValueError('Unknown request')
            row=dict(record)
            if row['state'] not in ('http_fatal','unknown','storage_error','invalid_output'):
                raise ValueError('Only stopped, reviewed requests can receive a retry disposition')
            number,task=self._task(row['task'])
            status=self.control.manager.db.execute('SELECT status FROM tasks WHERE id=? AND run=?',(row['task'],self.run_id)).fetchone()[0]
            if status=='completed':raise ValueError('Completed task cannot be retried')
            count=self.db.execute('SELECT count(*) FROM requests WHERE task=?',(row['task'],)).fetchone()[0]
            if count>self.config['max_retries']:raise ValueError('Frozen retry limit exhausted; no further automatic calls')
            history=next(a for a in self.control._history() if a['attempt_id']==row['attempt'])
            if history['status']=='running':
                if not delivery_reviewed:raise ValueError('Confirm ended/unrecoverable prior delivery explicitly; inspect receipts first')
                self.control.log.finish_attempt(self.run_id,row['attempt'],status='interrupted',
                    failure_reason='Operator confirmed prior execution ended; original response unrecoverable: '+evidence)
            self.control.event('api_request_retry_review',request_id=request_id,task_id=row['task'],attempt_id=row['attempt'],
                               previous_state=row['state'],evidence=evidence,delivery_reviewed=delivery_reviewed)
            self._update(request_id,state='http_retryable',not_before=0)
            # Will refuse clearance if another running/unreconciled success exists.
            self.control.review_stop(evidence=evidence,retryable_attempts=[row['attempt']])
            self.checkpoint('operator retry disposition; no request issued')
            return dict(request_id=request_id,disposition='explicit_retry_approved',network_requests=0,
                        retained_cost_reservation=row['reserved'] if row['cost'] is None else '0')

    def run(self,*,max_tasks=None):
        if self.provider is None:raise ValueError('Explicit provider/paid authorization required')
        if max_tasks is not None and (type(max_tasks) is not int or max_tasks<1):raise ValueError('max_tasks must be positive')
        with ProcessLock(self.root/'api/executor.lock'):
            dump(self.root/'api'/('invocation_'+uuid.uuid4().hex+'.json'),
                 dict(timestamp=now(),max_new_unique_tasks_this_launch=max_tasks,
                      config_sha256=self.manifest['api_config_sha256'],transport=type(self.provider).__name__))
            self._recover();self.stop.clear()
            pending=[t['payload'] for t in self.control.manager.tasks(self.run_id) if t['status']!='completed']
            if max_tasks:pending=pending[:max_tasks]
            todo=list(pending);active={};reason='max_tasks' if max_tasks and len(pending)==max_tasks else 'complete';finished=self.status()['valid_comparisons']
            pool=ThreadPoolExecutor(max_workers=self.config['concurrency'])
            try:
                while todo or active:
                    # Consume ALL completed work before any replenishment.
                    done=[f for f in active if f.done()]
                    for future in done:
                        row,task=active.pop(future)
                        try:
                            future.result();self._update(row['id'],state='response_saved');self._accept(row);finished+=1
                            if finished%self.config['checkpoint_every']==0:self.checkpoint('20 completed comparisons')
                        except ProviderError as exc:
                            self._update(row['id'],state='unknown' if exc.uncertain else 'http_fatal',
                                         error_kind=exc.kind,http_status=exc.status)
                            if exc.uncertain:
                                self._halt('unknown_delivery',row,exc)
                            else:
                                self.control.log.finish_attempt(self.run_id,row['attempt'],status='failed',failure_reason=str(exc))
                                count=self.db.execute('SELECT count(*) FROM requests WHERE task=?',(row['task'],)).fetchone()[0]
                                if exc.retryable and count<=self.config['max_retries'] and not self.stop.is_set():
                                    delay=max(exc.delay,min(120,self.config['backoff_seconds']*2**(count-1)))
                                    self._update(row['id'],state='http_retryable',not_before=time.time()+delay)
                                    todo.insert(0,task)
                                else:self._halt('http_error',row,exc)
                        except Exception as exc:
                            saved=Path(row['body_path']).is_file()
                            if saved:
                                try:self._usage(row,decode_json(Path(row['body_path']).read_bytes().decode('utf-8')))
                                except Exception:pass  # Missing/invalid usage retains reservation, never invented.
                            self._update(row['id'],state='invalid_output' if saved else 'storage_error',error_kind=type(exc).__name__)
                            # A failed save leaves the existing attempt unresolved.
                            if saved:
                                latest=next(a for a in self.control._history() if a['attempt_id']==row['attempt'])
                                if latest['status']=='running':
                                    self.control.log.finish_attempt(self.run_id,row['attempt'],status='failed',failure_reason='API response invalid; inspect receipt')
                            self._halt('response_or_import',row,ValueError(type(exc).__name__+'; inspect archived receipt and request ledger'))
                    if self.stop.is_set():
                        reason='stopped_for_review';todo.clear()
                    while todo and len(active)<self.config['concurrency'] and not self.stop.is_set():
                        task=todo.pop(0)
                        if self.control.safety_status()['open_halts']:self.stop.set();reason='stopped_for_review';break
                        distinct=self.db.execute('SELECT count(distinct task) FROM requests').fetchone()[0]
                        old=self.db.execute('SELECT * FROM requests WHERE task=? ORDER BY id DESC LIMIT 1',(task['task_id'],)).fetchone()
                        if not old and distinct>=self.config['max_total_tasks']:todo.clear();reason='max_total_tasks';break
                        if old and old['not_before']>time.time():
                            todo.insert(0,task);break
                        body=DeepSeekProvider.request(task,self.config);reserve=self.reservation(body)
                        if Decimal(self.status()['budget_accounted_usd'])+reserve>Decimal(self.config['max_cost_usd']):
                            todo.clear();reason='budget_limit';break
                        rid=self.db.execute('SELECT coalesce(max(id),0)+1 FROM requests').fetchone()[0]
                        path=self.root/'api/requests'/f'{rid:06}'/'http.json'
                        dump(path.with_name('request.json'),body)
                        retry=old['attempt'] if old else None
                        attempt=self.control.log.start_attempt(self.run_id,task['task_id'],executor='deepseek-api',retry_of=retry)
                        with self.db:
                            self.db.execute('INSERT INTO requests(id,task,attempt,state,reserved,body_path,created) VALUES (?,?,?,?,?,?,?)',
                              (rid,task['task_id'],attempt,'sent',str(reserve),str(path),now()))
                        row=dict(self.db.execute('SELECT * FROM requests WHERE id=?',(rid,)).fetchone())
                        active[pool.submit(self._worker,body,path,task)]=(row,task)
                    if active:wait(active,timeout=0.25,return_when=FIRST_COMPLETED)
                    elif todo and not self.stop.is_set():self.sleep(min(0.25,max(0,todo and 0.25)))
            except KeyboardInterrupt:
                self.stop.set();reason='interrupted'
                # Already-issued requests are allowed to finish/save; journaled
                # sent entries are imported on resume, never blindly repeated.
            except Exception as exc:
                self.stop.set();reason='dispatcher_error'
                self.control.halt('api_dispatch',error=ValueError(type(exc).__name__+'; review request/attempt journals'))
                raise
            finally:
                pool.shutdown(wait=True,cancel_futures=False)
                checkpoint_path=self.checkpoint(reason)
            return dict(reason=reason,checkpoint_path=checkpoint_path,**self.status())


def api_cli(args):
    if args.command=='api-check':return dict(config_valid=True,config=config_from(args.config),network_requests=0,credentials_logged=False)
    if args.command=='api-prepare':
        m=prepare_api_run(args.source_run,args.config,args.output,args.run_id)
        return dict(run=m['evaluation_run_id'],planned=m['planned_comparisons'],provider=m['provider'],live_requests=0)
    with ModelExecutor(args.run) as executor:
        if args.command=='api-status':return executor.status()
        if args.command=='api-estimate':return executor.estimate()
        if args.command=='api-reconcile':return executor.reconcile(args.request_id,evidence=args.evidence,delivery_reviewed=args.delivery_reviewed)
        if not args.allow_paid:raise ValueError('Paid API is disabled without explicit --allow-paid authorization')
        key=read_key(args.credentials)
        executor.provider=DeepSeekProvider(key,base_url=executor.config['base_url'],timeout=executor.config['timeout_seconds'])
        return executor.run(max_tasks=args.max_tasks)
