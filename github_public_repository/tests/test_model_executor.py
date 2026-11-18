"""Mock-only API tests. No real credentials, network or research assessments."""
import json
import threading
import time
import unittest
from decimal import Decimal
from unittest.mock import patch

from tests import test_batch_execution as fixtures
from core.model_executor import ModelExecutor, prepare_api_run, ProcessLock, config_from, DEFAULT_CONFIG
from core.providers.deepseek_provider import ProviderError, DeepSeekProvider, read_key
from scripts.jqa import main


class MockProvider:
    def __init__(self,actions=(), *, malformed=None,synchronize_first_two=False):
        self.actions=list(actions);self.calls=[];self.malformed=malformed
        self.lock=threading.Lock();self.active=0;self.peak=0
        self.barrier=threading.Barrier(2) if synchronize_first_two else None
    def call(self,body):
        with self.lock:
            self.calls.append(body);self.active+=1;self.peak=max(self.peak,self.active)
            action=self.actions.pop(0) if self.actions else None
            index=len(self.calls)
        try:
            if self.barrier and index<=2:self.barrier.wait(timeout=10)
            time.sleep(.005)
            if isinstance(action,BaseException):raise action
            t=json.loads(body['messages'][1]['content'])
            result={k:t[k] for k in ('task_id','evaluation_run_id','model_name','model_version','prompt_version')}
            result.update(schema_version='jqa.result.v1',articles=[dict(article_id=a['article_id'],originality=2,significance=3,rigour=2,evidence_note='离线模拟：中文、α、😀、引号"和换行\n；非真实论文评分',uncertainty='high') for a in t['articles']])
            if self.malformed=='score':result['articles'][0]['originality']=9
            if self.malformed=='fields':del result['articles'][0]['rigour']
            content=json.dumps(result,ensure_ascii=False,indent=2)
            if self.malformed=='empty':content='   '
            if self.malformed=='json':content='{bad'
            envelope=dict(id='offline-'+str(len(self.calls)),model='deepseek-flash',system_fingerprint='mock-only',
                usage=dict(prompt_tokens=1000,completion_tokens=300,prompt_cache_hit_tokens=100),
                choices=[dict(finish_reason='length' if self.malformed=='length' else 'stop',message=dict(content=content))])
            return json.dumps(envelope,ensure_ascii=False).encode('utf-8')
        finally:
            with self.lock:self.active-=1


class ModelExecutorTests(unittest.TestCase):
    setUp=fixtures.BatchExecutionTests.setUp
    tearDown=fixtures.BatchExecutionTests.tearDown
    prepared=fixtures.BatchExecutionTests.prepared
    def api_run(self,n=4,**overrides):
        _,source=self.prepared(n)
        cfg=json.loads(DEFAULT_CONFIG.read_text(encoding='utf-8'));cfg.update(concurrency=1,backoff_seconds=0,max_cost_usd='10');cfg.update(overrides)
        self.cfg=self.root/'api_config.json';self.cfg.write_text(json.dumps(cfg),encoding='utf-8')
        self.source=source;self.api=self.root/'api'
        return prepare_api_run(source,self.cfg,self.api,'offline-deepseek-run')

    def test_normal_chinese_json_and_exact_independent_anonymous_messages(self):
        m=self.api_run();mock=MockProvider()
        with ModelExecutor(self.api,mock) as ex:
            result=ex.run(max_tasks=1)
            self.assertEqual(result['valid_comparisons'],1);self.assertEqual(result['valid_article_ratings'],2)
            self.assertEqual(result['prompt_tokens'],1000)
            self.assertEqual(result['completion_tokens'],300)
            raw=list((self.api/'api/requests').glob('*/http.json'))[0].read_bytes()
            self.assertIn('中文'.encode('utf-8'),raw)
            body=mock.calls[0];self.assertEqual(body['thinking'],{'type':'disabled'})
            self.assertEqual(body['response_format'],{'type':'json_object'});self.assertEqual(len(body['messages']),2)
            t=json.loads(body['messages'][1]['content']);self.assertEqual(len(t['articles']),2)
            self.assertTrue(all(set(a)=={'article_id','title','abstract'} for a in t['articles']))
            self.assertIn('jqa.assessment.v1.2',body['messages'][0]['content'])
            self.assertIn('jqa.rubric.v1.1',body['messages'][0]['content'])
            for name in ('assessment_prompt.md','scoring_rubric.md'):
                self.assertIn((self.api/'frozen'/name).read_bytes().decode('utf-8-sig'),body['messages'][0]['content'])

    def test_empty_json_schema_scores_and_truncation_stop_immediately(self):
        for malformed in ('empty','json','fields','score','length'):
            with self.subTest(malformed=malformed):
                # New fixture per subcase; preserve no cross-case state.
                if hasattr(self,'api'):
                    self.tmp.cleanup();self.setUp()
                self.api_run();mock=MockProvider(malformed=malformed)
                with ModelExecutor(self.api,mock) as ex:
                    result=ex.run();self.assertEqual(len(mock.calls),1)
                    self.assertEqual(result['valid_comparisons'],0);self.assertTrue(result['safety']['open_halts'])
                    self.assertEqual(result['usage_reported_requests'],1)
                    with self.assertRaises(ValueError):ex.run()

    def test_timeout_unknown_request_never_blindly_retried(self):
        self.api_run();mock=MockProvider([ProviderError('timeout',uncertain=True)])
        with ModelExecutor(self.api,mock) as ex:
            result=ex.run();self.assertEqual(len(mock.calls),1)
            self.assertEqual(result['request_states'],{'unknown':1})
            self.assertGreater(Decimal(result['missing_usage_or_inflight_reserved_usd']),0)
        with ModelExecutor(self.api,mock) as ex:
            with self.assertRaises(ValueError):ex.run()
        self.assertEqual(len(mock.calls),1)

    def test_429_5xx_and_presend_fault_have_finite_explicit_retries(self):
        for error in (ProviderError('http_error',status=429,retryable=True),ProviderError('http_error',status=503,retryable=True),ProviderError('connect_error',retryable=True)):
            if hasattr(self,'api'):self.tmp.cleanup();self.setUp()
            self.api_run();mock=MockProvider([error])
            with ModelExecutor(self.api,mock) as ex:
                result=ex.run(max_tasks=1);self.assertEqual(len(mock.calls),2)
                self.assertEqual(result['valid_comparisons'],1)
                history=ex.control._history();self.assertEqual(len(history),2)
                self.assertEqual(history[-1]['retry_of'],history[0]['attempt_id'])

    def test_balance_and_authentication_errors_stop_without_retry(self):
        self.api_run();mock=MockProvider([ProviderError('http_error',status=402)])
        with ModelExecutor(self.api,mock) as ex:
            result=ex.run();self.assertEqual(len(mock.calls),1)
            self.assertEqual(result['valid_comparisons'],0);self.assertTrue(result['safety']['open_halts'])

    def test_retry_exhaustion(self):
        self.api_run(max_retries=1);mock=MockProvider([ProviderError('http_error',status=429,retryable=True)]*3)
        with ModelExecutor(self.api,mock) as ex:
            result=ex.run();self.assertEqual(len(mock.calls),2)
            self.assertEqual(result['valid_comparisons'],0);self.assertTrue(result['safety']['open_halts'])

    def test_completed_tasks_skip_and_direct_recovery_import_is_idempotent(self):
        self.api_run();mock=MockProvider()
        with ModelExecutor(self.api,mock) as ex:
            ex.run(max_tasks=1)
            row=dict(ex.db.execute('select * from requests').fetchone())
            replay=ex._accept(row);self.assertEqual(replay['import_result']['idempotent'],1)
            result=ex.run();self.assertEqual(result['valid_comparisons'],6)
            self.assertEqual(len(mock.calls),6)

    def test_saved_response_interrupt_resumes_without_model_replay(self):
        self.api_run();mock=MockProvider()
        with ModelExecutor(self.api,mock) as ex:
            with patch.object(ex.control,'complete_task',side_effect=KeyboardInterrupt):
                result=ex.run(max_tasks=1)
            self.assertEqual(result['valid_comparisons'],0);self.assertEqual(len(mock.calls),1)
        with ModelExecutor(self.api,mock) as ex:
            ex._recover();self.assertEqual(ex.status()['valid_comparisons'],1)
            self.assertEqual(len(mock.calls),1)

    def test_write_failure_preserves_unknown_attempt_and_stops(self):
        self.api_run();mock=MockProvider()
        from core.model_executor import atomic_bytes
        def fail_http(path,raw):
            if path.name=='http.json':raise OSError('offline simulated full disk')
            return atomic_bytes(path,raw)
        with ModelExecutor(self.api,mock) as ex:
            with patch('core.model_executor.atomic_bytes',side_effect=fail_http):result=ex.run()
            self.assertEqual(result['valid_comparisons'],0);self.assertEqual(len(mock.calls),1)
            self.assertEqual(ex.control._history()[0]['status'],'running')
            self.assertEqual(result['request_states'],{'storage_error':1})

    def test_os_lock_rejects_second_executor_and_no_duplicate_tasks(self):
        self.api_run();mock=MockProvider()
        with ProcessLock(self.api/'api/executor.lock'):
            with ModelExecutor(self.api,mock) as ex:
                with self.assertRaises(ValueError):ex.run()
        self.assertFalse(mock.calls)

    def test_two_worker_concurrency_and_20_task_checkpoint_not_total_limit(self):
        self.api_run(10,concurrency=2);mock=MockProvider(synchronize_first_two=True)
        with ModelExecutor(self.api,mock) as ex:
            result=ex.run();self.assertEqual(result['valid_comparisons'],45)
            self.assertEqual(mock.peak,2);self.assertEqual(len(mock.calls),45)
            self.assertEqual(len(list((self.api/'api').glob('checkpoint_*.json'))),3)

    def test_cost_zero_and_max_total_tasks(self):
        self.api_run(max_cost_usd='0');mock=MockProvider()
        with ModelExecutor(self.api,mock) as ex:
            result=ex.run();self.assertEqual(result['reason'],'budget_limit');self.assertFalse(mock.calls)
        self.tmp.cleanup();self.setUp();self.api_run(max_total_tasks=2);mock=MockProvider()
        with ModelExecutor(self.api,mock) as ex:
            result=ex.run();self.assertEqual(len(mock.calls),2);self.assertEqual(result['valid_comparisons'],2)

    def test_source_isolation_and_frozen_config_tamper(self):
        self.api_run()
        from core.model_executor import sha
        before=sha(self.source/'store/tasks.sqlite3');mock=MockProvider()
        with ModelExecutor(self.api,mock) as ex:ex.run(max_tasks=1)
        self.assertEqual(sha(self.source/'store/tasks.sqlite3'),before)
        with self.assertRaises(ValueError):ModelExecutor(self.source,mock)
        (self.api/'frozen/api_config.json').write_text('{}',encoding='utf-8')
        with self.assertRaises(ValueError):ModelExecutor(self.api,mock)

    def test_configuration_credentials_and_cli_no_paid_opt_in(self):
        self.api_run()
        self.assertTrue(main(['api-check','--config',str(self.cfg)])['config_valid'])
        self.assertEqual(main(['api-estimate','--run',str(self.api)])['pending_tasks'],6)
        with self.assertRaises(ValueError):main(['api-run','--run',str(self.api)])
        secret=self.root/'.local';secret.mkdir();path=secret/'keys.json'
        path.write_text(json.dumps(dict(api_key='offline-secret')),encoding='utf-8')
        self.assertEqual(read_key(path),'offline-secret')
        with self.assertRaises(ValueError):read_key(self.root/'public.json')

    def test_concurrent_failure_drains_inflight_but_never_launches_third(self):
        self.api_run(concurrency=2);mock=MockProvider(malformed='score',synchronize_first_two=True)
        with ModelExecutor(self.api,mock) as ex:
            result=ex.run();self.assertEqual(len(mock.calls),2)
            self.assertEqual(result['valid_comparisons'],0)
            self.assertTrue(result['safety']['open_halts'])

    def test_concurrent_budget_reservations_and_cumulative_usage(self):
        self.api_run(concurrency=2,max_cost_usd='0.02');mock=MockProvider()
        with ModelExecutor(self.api,mock) as ex:
            result=ex.run()
            self.assertLessEqual(Decimal(result['budget_accounted_usd']),Decimal('0.02'))
            self.assertEqual(result['usage_reported_requests'],len(mock.calls))
            self.assertEqual(result['prompt_tokens'],1000*len(mock.calls))

    def test_provider_http_errors_timeout_and_no_secret_exception(self):
        import urllib.error
        secret='offline-test-secret-do-not-log';provider=DeepSeekProvider(secret)
        with patch('core.providers.deepseek_provider.urllib.request.build_opener') as factory:
            factory.return_value.open.side_effect=urllib.error.HTTPError(provider.url,429,secret,{},None)
            with self.assertRaises(ProviderError) as raised:provider.call({})
            self.assertEqual(raised.exception.status,429);self.assertTrue(raised.exception.retryable)
            self.assertNotIn(secret,str(raised.exception))
            factory.return_value.open.side_effect=TimeoutError(secret)
            with self.assertRaises(ProviderError) as raised:provider.call({})
            self.assertTrue(raised.exception.uncertain);self.assertNotIn(secret,str(raised.exception))

    def test_model_fingerprint_change_stops_instead_of_mixing(self):
        self.api_run();mock=MockProvider()
        with ModelExecutor(self.api,mock) as ex:
            ex.run(max_tasks=1)
            call=mock.call
            def changed(body):
                envelope=json.loads(call(body));envelope['system_fingerprint']='other-deployment'
                return json.dumps(envelope).encode('utf-8')
            mock.call=changed
            result=ex.run(max_tasks=1)
            self.assertEqual(result['valid_comparisons'],1)
            self.assertTrue(result['safety']['open_halts'])

    def test_thinking_experiment_is_frozen_separate_configuration(self):
        self.api_run(thinking='enabled');mock=MockProvider()
        with ModelExecutor(self.api,mock) as ex:ex.run(max_tasks=1)
        self.assertEqual(mock.calls[0]['thinking'],{'type':'enabled'})

    def test_request_ledger_run_binding_rejects_cross_run_copy(self):
        self.api_run()
        with ModelExecutor(self.api,MockProvider()) as ex:
            ex.db.execute("UPDATE metadata SET value='other-run' WHERE key='run_id'");ex.db.commit()
        with self.assertRaises(ValueError):ModelExecutor(self.api,MockProvider())

    def test_operator_reconciles_unknown_without_hiding_attempt_or_cost(self):
        self.api_run();mock=MockProvider([ProviderError('timeout',uncertain=True)])
        with ModelExecutor(self.api,mock) as ex:
            ex.run(max_tasks=1)
            with self.assertRaises(ValueError):ex.reconcile(1,evidence='Not yet confirmed')
            ex.reconcile(1,evidence='OFFLINE TEST: prior work ended, original response demonstrably lost; retry authorized',delivery_reviewed=True)
            self.assertEqual(len(mock.calls),1)
            result=ex.run(max_tasks=1);self.assertEqual(result['valid_comparisons'],1)
            self.assertEqual(len(mock.calls),2)
            self.assertGreater(Decimal(result['missing_usage_or_inflight_reserved_usd']),0)
            self.assertEqual(ex.control._history()[0]['status'],'interrupted')

    def test_invalid_result_explicit_review_then_retry_preserves_original(self):
        self.api_run();mock=MockProvider(malformed='score')
        with ModelExecutor(self.api,mock) as ex:
            ex.run(max_tasks=1);old=(self.api/'api/requests/000001/http.json').read_bytes()
            ex.reconcile(1,evidence='OFFLINE TEST: invalid score reviewed; explicit new evaluation authorized')
            mock.malformed=None
            result=ex.run(max_tasks=1);self.assertEqual(result['valid_comparisons'],1)
            self.assertEqual((self.api/'api/requests/000001/http.json').read_bytes(),old)

    def test_getpass_echo_fallback_is_rejected(self):
        import getpass
        with patch('core.providers.deepseek_provider.getpass.getpass',side_effect=getpass.GetPassWarning('no echo control')):
            with self.assertRaises(ValueError):read_key()


if __name__=='__main__':unittest.main()
