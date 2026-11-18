"""Official non-streaming Chat Completions transport; standard library only.

Reference: https://api-docs.deepseek.com/api/create-chat-completion/
No automatic HTTP retries here: the executor journals EVERY actual request.
"""
from __future__ import annotations

import getpass
import json
import socket
import urllib.error
import urllib.request
import warnings
from pathlib import Path


class ProviderError(Exception):
    def __init__(self, kind, *, status=None, retryable=False, uncertain=False, delay=0):
        # Never interpolate provider error bodies, URLs, headers or credentials.
        self.kind, self.status = kind, status
        self.retryable, self.uncertain, self.delay = retryable, uncertain, delay
        super().__init__(f'{kind}; HTTP={status}; delivery_unknown={uncertain}')


def read_key(credentials=None, *, interactive=True):
    if credentials:
        path=Path(credentials).resolve()
        if '.local' not in path.parts and not path.name.endswith('.local.json'):
            raise ValueError('Keep credentials in ignored .local/ or *.local.json')
        try:
            data=json.loads(path.read_text(encoding='utf-8-sig'))
            key=data['api_key']
        except (OSError,ValueError,KeyError):
            raise ValueError('Cannot read local credential file') from None
    elif interactive:
        try:
            with warnings.catch_warnings():
                warnings.simplefilter('error',getpass.GetPassWarning)
                key=getpass.getpass('DeepSeek API key (hidden): ')
        except (getpass.GetPassWarning,EOFError):
            raise ValueError('No secure hidden-input terminal; use an ignored local credential file') from None
    else:
        raise ValueError('Credentials required')
    if not isinstance(key,str) or not key.strip() or '\n' in key or '\r' in key:
        raise ValueError('Invalid credential')
    return key.strip()


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None  # Never forward Authorization to another endpoint.


class DeepSeekProvider:
    def __init__(self, api_key, *, base_url='https://api.deepseek.com', timeout=120):
        if base_url!='https://api.deepseek.com':
            raise ValueError('Only the official DeepSeek HTTPS endpoint is allowed')
        self._key=api_key
        self.url=base_url+'/chat/completions'
        self.timeout=timeout

    @staticmethod
    def request(task, config):
        # A fresh independent conversation for exactly one pair. Frozen text is
        # included verbatim; no extra scoring instruction or score generation.
        public={k:task[k] for k in ('task_id','evaluation_run_id','model_name','model_version','prompt_version')}
        public['articles']=[{k:a[k] for k in ('article_id','title','abstract')} for a in task['articles']]
        return dict(model=config['model'],thinking={'type':config['thinking']},
                    max_tokens=config['max_tokens'],stream=False,
                    response_format={'type':'json_object'},
                    messages=[dict(role='system',content=task['instructions']),
                              dict(role='user',content=json.dumps(public,ensure_ascii=False))])

    def call(self, body):
        request=urllib.request.Request(self.url,data=json.dumps(body,ensure_ascii=False).encode('utf-8'),
                headers={'Authorization':'Bearer '+self._key,'Content-Type':'application/json'},method='POST')
        try:
            opener=urllib.request.build_opener(_NoRedirect())
            with opener.open(request,timeout=self.timeout) as response:
                raw=response.read()
            # Never persist a provider accidentally echoing a credential.
            if self._key.encode('utf-8') in raw:
                raise ProviderError('credential_echo',uncertain=True)
            return raw
        except urllib.error.HTTPError as exc:
            try:delay=min(120,max(0,float(exc.headers.get('Retry-After','0'))))
            except (TypeError,ValueError):delay=0
            raise ProviderError('http_error',status=exc.code,
                    retryable=exc.code==429 or 500<=exc.code<=599,delay=delay) from None
        except (TimeoutError,socket.timeout):
            # POST may already have executed. Do not blindly repeat a charge.
            raise ProviderError('timeout',uncertain=True) from None
        except urllib.error.URLError as exc:
            pre_send=isinstance(exc.reason,(socket.gaierror,ConnectionRefusedError))
            raise ProviderError('connect_error' if pre_send else 'transport_error',
                                retryable=pre_send,uncertain=not pre_send) from None
        except OSError:
            raise ProviderError('transport_error',uncertain=True) from None
