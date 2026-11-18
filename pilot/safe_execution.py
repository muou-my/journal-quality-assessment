"""jqa.execution-safety.v1: additive file/lifecycle safeguards, no scoring changes.

Historical BatchExecution and all core modules remain byte-identical. Use this
controller through scripts/jqa.py. One coordinated scheduler per run is required.
"""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from core.result_parser import decode_json, parse_result
from pilot.batch_execution import BatchExecution, BatchExecutionError, _sha

VERSION = 'jqa.execution-safety.v1'


def atomic_bytes(path, raw):
    """Publish complete bytes with atomic no-clobber hard link; retain failed temp.

    No replace/rename fallback: unsupported filesystems must fail closed. The
    temporary file lives on the destination volume. A crash can leave a temp or
    a fully committed destination, never a newly truncated destination.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise FileExistsError(str(path))
    fd, name = tempfile.mkstemp(prefix='.jqa-', suffix='.tmp', dir=path.parent)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    temp = Path(name)
    if temp.read_bytes() != raw:
        raise OSError('Temporary response integrity mismatch: ' + name)
    os.link(temp, path)  # Atomic creation; an existing destination is never replaced.
    temp.unlink()
    return path


class SafeBatchExecution(BatchExecution):
    def __init__(self, run_directory):
        super().__init__(run_directory)
        directory = self.root/'safety_v1'
        directory.mkdir(exist_ok=True)
        self.safety = sqlite3.connect(directory/'audit.sqlite3', timeout=30)
        self.safety.row_factory = sqlite3.Row
        self.safety.execute('CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, kind TEXT NOT NULL, payload TEXT NOT NULL)')
        self.safety.commit()

    def close(self):
        if hasattr(self, 'safety'):
            self.safety.close()
        super().close()

    def event(self, kind, **payload):
        payload.update(evaluation_run_id=self.run, controller_version=VERSION,
                       timestamp=datetime.now(timezone.utc).isoformat())
        with self.safety:
            cursor = self.safety.execute('INSERT INTO events(kind,payload) VALUES (?,?)',
                                        (kind, json.dumps(payload, ensure_ascii=False)))
        return cursor.lastrowid

    def halt(self, stage, task_id=None, attempt_id=None, error=None, **details):
        return self.event('halt', stage=stage, task_id=task_id, attempt_id=attempt_id,
                          exception_type=type(error).__name__ if error else None,
                          error=str(error) if error else None, **details)

    def safety_status(self):
        events = [dict(id=r['id'], kind=r['kind'], **json.loads(r['payload']))
                  for r in self.safety.execute('SELECT * FROM events ORDER BY id')]
        reviewed = {i for e in events if e['kind']=='review' for i in e['halt_ids']}
        return dict(controller_version=VERSION, events=events,
                    open_halts=[e for e in events if e['kind']=='halt' and e['id'] not in reviewed])

    def review_stop(self, *, evidence, retryable_attempts=()):
        """Explicit operator audit, not a model retry or alteration of old logs.

        Only terminal, unaccepted attempts may be declared retryable after loss
        or invalidity has actually been reviewed. Running work must be reconciled
        with its original response or explicitly failed/interrupted first.
        """
        if not isinstance(evidence, str) or not evidence.strip():
            raise BatchExecutionError('Review evidence required')
        history = self._history()
        if any(a['status']=='running' for a in history):
            raise BatchExecutionError('Reconcile all running attempts before review')
        retryable = set(retryable_attempts)
        known = {a['attempt_id']: a for a in history}
        if not retryable <= known.keys():
            raise BatchExecutionError('Unknown reviewed attempt')
        for attempt in retryable:
            row = known[attempt]
            with self._source() as db:
                status = db.execute('SELECT status FROM tasks WHERE id=? AND run=?', (row['task_id'],self.run)).fetchone()[0]
            if status=='completed':
                raise BatchExecutionError('Completed work cannot be declared retryable')
        allowed = self._reviewed_attempts() | retryable
        with self._source() as db:
            completed = {r[0] for r in db.execute("SELECT id FROM tasks WHERE run=? AND status='completed'",(self.run,))}
        if any(a['status']=='succeeded' and a['task_id'] not in completed and a['attempt_id'] not in allowed for a in history):
            raise BatchExecutionError('Successful unimported output requires explicit review or original import')
        return self.event('review', evidence=evidence, retryable_attempts=sorted(retryable),
                          reviewed_terminal_failures=[a['attempt_id'] for a in history if a['status'] in ('failed','interrupted')],
                          halt_ids=[e['id'] for e in self.safety_status()['open_halts']])

    def _reviewed_attempts(self):
        return {a for e in self.safety_status()['events'] if e['kind']=='review'
                for a in e['retryable_attempts']+e.get('reviewed_terminal_failures',[])}

    def _guard(self):
        if self.safety_status()['open_halts']:
            raise BatchExecutionError('Run stopped: resolve incident and explicitly review-stop before starting new work')
        allowed = self._reviewed_attempts()
        with self._source() as db:
            completed = {r[0] for r in db.execute("SELECT id FROM tasks WHERE run=? AND status='completed'",(self.run,))}
        for a in self._history():
            if a['status']=='running' or (a['status'] in ('succeeded','failed','interrupted') and a['task_id'] not in completed and a['attempt_id'] not in allowed):
                raise BatchExecutionError('Unresolved execution in this run; reconcile before any new task')

    def start_task(self, number, task_id, *, retry_of=None):
        self._guard()
        # Legacy invalid successful attempts require an explicit audited exception.
        if retry_of in self._reviewed_attempts():
            task,status,_ = self._task(number,task_id)
            if status=='completed':
                raise BatchExecutionError('Completed task must not be evaluated again')
            attempt = self.log.start_attempt(self.run,task_id,executor='codex-agent',retry_of=retry_of)
            return dict(attempt_id=attempt,task=task)
        return super().start_task(number,task_id,retry_of=retry_of)

    def export_batch(self, number, *, snapshot=1):
        self._guard()
        return super().export_batch(number,snapshot=snapshot)

    def save_response(self, number, task_id, attempt_id, path, text):
        """Save actual Agent text; never synthesizes ratings or fills routing data."""
        previous = self._owned_attempt(number,task_id,attempt_id)
        _,status,_=self._task(number,task_id)
        if previous['status'] not in ('running','succeeded') or status=='completed':
            raise BatchExecutionError('Save requires an existing unaccepted execution, not a new or completed task')
        try:
            raw = text.encode('utf-8', errors='strict')
            # Check completeness, not scoring validity. Keep malformed output as
            # an explicitly invalid raw file for diagnosis, never an accepted score.
            if not text.strip():
                raise ValueError('Empty response rejected')
            if len(text.splitlines())!=1:
                raise ValueError('Exactly one JSONL result required')
            decode_json(text)
        except (ValueError,UnicodeError) as exc:
            # Preserve malformed, nonempty UTF-8 model text too. It is evidence,
            # not a valid score. Storage errors remain a distinct incident stage.
            if isinstance(text,str) and text.strip():
                try:
                    atomic_bytes(path,text.encode('utf-8',errors='strict'))
                except (OSError,UnicodeError) as storage:
                    self.halt('response_write',task_id,attempt_id,storage,generated_response=True,
                              destination=str(Path(path).resolve()))
                    raise BatchExecutionError('Invalid response and storage failure; stop and audit original message') from storage
            self.halt('response_format',task_id,attempt_id,exc,generated_response=True)
            raise BatchExecutionError('Invalid response text; stop and preserve original Agent output for audit') from exc
        try:
            result = atomic_bytes(path,raw)
        except (OSError,UnicodeError) as exc:
            self.halt('response_write',task_id,attempt_id,exc,generated_response=True,
                      destination=str(Path(path).resolve()),recovery='Inspect original Agent message and .jqa-*.tmp; do not re-evaluate')
            raise BatchExecutionError('Response storage failed; no successful score recorded; stop batch') from exc
        self.event('response_saved',task_id=task_id,attempt_id=attempt_id,
                   path=str(result.resolve()),sha256=_sha(result))
        return str(result)

    def complete_task(self, number, task_id, attempt_id, response_path):
        previous = self._owned_attempt(number,task_id,attempt_id)
        if previous['status'] not in ('running','succeeded'):
            raise BatchExecutionError('Failed/interrupted attempt requires review, not silent import')
        task,status,_ = self._task(number,task_id)
        path=self._response_folder(task_id)/f"a{previous['attempt_number']:03}.jsonl"
        try:
            raw=Path(response_path).read_bytes()
            if not raw:
                raise ValueError('Empty response: possible storage failure, not evidence of invalid model judgment')
            # Enforce UTF-8 without BOM for new archival output. Never transcode
            # unknown bytes or silently change a supplied original response.
            raw.decode('utf-8',errors='strict')
            if path.exists():
                if path.read_bytes()!=raw:
                    # Recover a genuinely existing response for the legacy empty
                    # archive without replacing it or re-evaluating the task.
                    if path.stat().st_size==0 and previous['status']=='succeeded':
                        import hashlib
                        recovered=path.with_name(path.stem+'.recovered_'+hashlib.sha256(raw).hexdigest()[:12]+'.jsonl')
                        if status=='completed' and not recovered.exists():
                            raise ValueError('Completed response conflict; original preserved')
                        if recovered.exists():
                            if recovered.read_bytes()!=raw:
                                raise ValueError('Recovered response conflict')
                        else:
                            atomic_bytes(recovered,raw)
                            self.event('legacy_response_recovered',task_id=task_id,attempt_id=attempt_id,
                                       original_empty_path=str(path),recovered_path=str(recovered),sha256=_sha(recovered))
                        path=recovered
                    else:
                        raise ValueError('Conflicting response bytes; original preserved')
            else:
                atomic_bytes(path,raw)
        except (OSError,ValueError,UnicodeError) as exc:
            self.halt('response_storage',task_id,attempt_id,exc,response_path=str(response_path))
            raise BatchExecutionError('Response storage/encoding failure; stop and audit; no success recorded') from exc
        try:
            text=raw.decode('utf-8-sig')
            if len(text.splitlines())!=1:
                raise ValueError('Exactly one JSONL task result required')
            parse_result(text,task)
        except ValueError as exc:
            if previous['status']=='running':
                self.log.finish_attempt(self.run,attempt_id,status='failed',failure_reason='Invalid model response',exception=exc)
            self.halt('result_validation',task_id,attempt_id,exc,response_path=str(path),sha256=_sha(path))
            raise BatchExecutionError('Invalid model response archived; batch stopped') from exc
        try:
            self.log.finish_attempt(self.run,attempt_id,status='succeeded')
            result=self.manager.import_results(self.run,path)
            if result['errors'] or result['conflicts'] or result['completed']+result['idempotent']!=1:
                raise BatchExecutionError('Validated response import rejected')
        except Exception as exc:
            self.halt('result_import',task_id,attempt_id,exc,response_path=str(path))
            raise
        return dict(import_result=result,raw_response=str(path),raw_response_sha256=_sha(path))

    def fail_task(self, number, task_id, attempt_id, **kwargs):
        result=super().fail_task(number,task_id,attempt_id,**kwargs)
        self.halt('execution_failure',task_id,attempt_id,reason=kwargs.get('reason'))
        return result
