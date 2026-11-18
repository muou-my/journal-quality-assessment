"""Persistent, run-isolated execution attempts; no model calls or task writes.

Execution starts must be recorded explicitly when an Agent actually begins work,
not at task export. This ledger cannot observe an external Agent automatically.
It uses a separate SQLite database and never edits task/result storage.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import traceback
import uuid
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .result_parser import ResultError, decode_json, parse_result


class ExecutionLogError(ValueError):
    """Invalid lifecycle, run binding, or inconsistent task-store provenance."""


def _time(value=None):
    if value is None:
        value=datetime.now(timezone.utc)
    elif isinstance(value,str):
        try:
            value=datetime.fromisoformat(value.replace('Z','+00:00'))
        except ValueError as exc:
            raise ExecutionLogError('Invalid ISO-8601 timestamp') from exc
    if not isinstance(value,datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ExecutionLogError('Timestamps must be timezone-aware')
    return value.astimezone(timezone.utc).isoformat(timespec='microseconds')


class ExecutionLogger:
    """Append-only starts/finishes in execution.sqlite3, bound to one task store.

    Use one logger connection per thread/process. SQLite IMMEDIATE transactions
    serialize competing starts; only one unfinished attempt per run/task is allowed.
    A restart merely reopens this ledger: unfinished work is not auto-retried.
    """
    def __init__(self, task_store_directory, log_directory=None):
        self.task_store=(Path(task_store_directory).resolve()/'tasks.sqlite3')
        if not self.task_store.is_file():
            raise ExecutionLogError('Task store does not exist')
        self.directory=Path(log_directory).resolve() if log_directory is not None else self.task_store.parent/'execution'
        self.directory.mkdir(parents=True,exist_ok=True)
        self.path=self.directory/'execution.sqlite3'
        self.db=sqlite3.connect(self.path,timeout=30)
        self.db.row_factory=sqlite3.Row
        try:
            self.db.execute('PRAGMA foreign_keys=ON')
            self.db.executescript('''
            CREATE TABLE IF NOT EXISTS ledger_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS run_bindings(run_id TEXT PRIMARY KEY, config_hash TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS attempts(
              attempt_id TEXT PRIMARY KEY, evaluation_run_id TEXT NOT NULL, task_id TEXT NOT NULL,
              comparison_event_id TEXT NOT NULL, model_name TEXT NOT NULL, model_version TEXT NOT NULL,
              prompt_version TEXT NOT NULL, started_at TEXT NOT NULL, executor TEXT NOT NULL,
              attempt_number INTEGER NOT NULL, retry_count INTEGER NOT NULL, retry_of TEXT,
              UNIQUE(evaluation_run_id,task_id,attempt_number), UNIQUE(evaluation_run_id,attempt_id),
              FOREIGN KEY(retry_of) REFERENCES attempts(attempt_id));
            CREATE TABLE IF NOT EXISTS finishes(
              attempt_id TEXT PRIMARY KEY, evaluation_run_id TEXT NOT NULL, ended_at TEXT NOT NULL,
              status TEXT NOT NULL CHECK(status IN ('succeeded','failed','interrupted')),
              failure_reason TEXT, exception_type TEXT, exception_message TEXT, exception_traceback TEXT,
              FOREIGN KEY(evaluation_run_id,attempt_id) REFERENCES attempts(evaluation_run_id,attempt_id));
            ''')
            with self._transaction():
                row=self.db.execute("SELECT value FROM ledger_meta WHERE key='task_store'").fetchone()
                if row and row['value']!=str(self.task_store):
                    raise ExecutionLogError('Ledger is already bound to another task store')
                self.db.execute('INSERT OR IGNORE INTO ledger_meta VALUES (?,?)',('task_store',str(self.task_store)))
        except Exception:
            self.db.close()
            raise

    def close(self):
        self.db.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    @contextmanager
    def _transaction(self):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            yield
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise

    def _snapshot(self, run):
        if not isinstance(run,str) or not run.strip():
            raise ExecutionLogError('evaluation_run_id must be nonempty text')
        source=sqlite3.connect(self.task_store.as_uri()+'?mode=ro',uri=True)
        try:
            source.execute('BEGIN')
            row=source.execute('SELECT config FROM runs WHERE id=?',(run,)).fetchone()
            if row is None:
                raise ExecutionLogError('Unknown evaluation_run_id')
            config=decode_json(row[0])
            fingerprint=hashlib.sha256(row[0].encode()).hexdigest()
            rows=source.execute('SELECT id,payload,private,status,result FROM tasks WHERE run=? ORDER BY id',(run,)).fetchall()
        finally:
            source.close()
        metadata={k:config[k] for k in ('model_name','model_version','prompt_version')}
        tasks={}
        for task_id,payload,private,status,result in rows:
            payload,private=decode_json(payload),decode_json(private)
            if payload.get('task_id')!=task_id or payload.get('evaluation_run_id')!=run or any(payload.get(k)!=v for k,v in metadata.items()):
                raise ExecutionLogError('Task run/model/prompt identity mismatch')
            tasks[task_id]=dict(payload=payload,private=private,status=status,result=result)
        return fingerprint,metadata,tasks

    def _check_binding(self,run,fingerprint,*,create=False):
        row=self.db.execute('SELECT config_hash FROM run_bindings WHERE run_id=?',(run,)).fetchone()
        if row and row['config_hash']!=fingerprint:
            raise ExecutionLogError('Run configuration changed since execution logging began')
        if create:
            self.db.execute('INSERT OR IGNORE INTO run_bindings VALUES (?,?)',(run,fingerprint))

    def _attempt_rows(self,run):
        return [dict(row) for row in self.db.execute('''
            SELECT a.*, f.ended_at, COALESCE(f.status,'running') AS status,
                   f.failure_reason,f.exception_type,f.exception_message,f.exception_traceback
            FROM attempts a LEFT JOIN finishes f ON a.attempt_id=f.attempt_id
            WHERE a.evaluation_run_id=? ORDER BY a.started_at,a.task_id,a.attempt_number
            ''',(run,))]

    def start_attempt(self,evaluation_run_id,task_id,*,executor='manual_agent',retry_of=None,started_at=None):
        """Record an observed start. Explicit retry_of is required after any attempt.

        A task export is NOT a start. A completed task cannot be started again.
        Optional aware started_at supports explicit ingestion of an observed time.
        Returns a UUID attempt_id; retries never allocate new comparison events.
        """
        if not isinstance(executor,str) or not executor.strip():
            raise ExecutionLogError('executor must be nonempty text')
        start=_time(started_at)
        fingerprint,metadata,tasks=self._snapshot(evaluation_run_id)
        if task_id not in tasks:
            raise ExecutionLogError('Unknown task for evaluation_run_id')
        if tasks[task_id]['status']=='completed':
            raise ExecutionLogError('Completed task cannot be executed again')
        with self._transaction():
            self._check_binding(evaluation_run_id,fingerprint,create=True)
            previous=self.db.execute('''SELECT a.attempt_id,a.attempt_number,f.ended_at
                FROM attempts a LEFT JOIN finishes f ON a.attempt_id=f.attempt_id
                WHERE a.evaluation_run_id=? AND a.task_id=? ORDER BY a.attempt_number DESC LIMIT 1''',
                (evaluation_run_id,task_id)).fetchone()
            if previous:
                if previous['ended_at'] is None:
                    raise ExecutionLogError('Task already has a running attempt; inspect recovery first')
                if retry_of!=previous['attempt_id']:
                    raise ExecutionLogError('Retry must explicitly reference the latest finished attempt')
                if start<previous['ended_at']:
                    raise ExecutionLogError('Retry cannot start before the previous attempt ended')
                number=previous['attempt_number']+1
            else:
                if retry_of is not None:
                    raise ExecutionLogError('First attempt cannot have retry_of')
                number=1
            attempt_id=uuid.uuid4().hex
            self.db.execute('''INSERT INTO attempts (
                attempt_id,evaluation_run_id,task_id,comparison_event_id,model_name,model_version,
                prompt_version,started_at,executor,attempt_number,retry_count,retry_of)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)''',
                (attempt_id,evaluation_run_id,task_id,tasks[task_id]['private']['event_id'],
                 metadata['model_name'],metadata['model_version'],metadata['prompt_version'],start,
                 executor,number,number-1,retry_of))
        return attempt_id

    def finish_attempt(self,evaluation_run_id,attempt_id,*,status,
                       failure_reason=None,exception=None,ended_at=None):
        """Append one terminal outcome. Succeeded does not mean a valid score.

        Identical terminal outcomes are idempotent; conflicting finishes are
        rejected, never overwrite the original record. A supplied end time must
        also match on replay. Exceptions are recorded as text, not re-raised.
        """
        if status not in ('succeeded','failed','interrupted'):
            raise ExecutionLogError('Invalid terminal status')
        if exception is not None and not isinstance(exception,BaseException):
            raise ExecutionLogError('exception must be a BaseException instance')
        if status=='succeeded' and (exception is not None or failure_reason is not None):
            raise ExecutionLogError('Successful attempts cannot have failure information')
        if failure_reason is None and exception is not None:
            failure_reason=str(exception) or type(exception).__name__
        if status!='succeeded' and (not isinstance(failure_reason,str) or not failure_reason.strip()):
            raise ExecutionLogError('Failed/interrupted attempts require a reason')
        outcome=dict(status=status,failure_reason=failure_reason,
            exception_type=type(exception).__name__ if exception is not None else None,
            exception_message=str(exception) if exception is not None else None,
            exception_traceback=''.join(traceback.format_exception(type(exception),exception,exception.__traceback__)) if exception is not None else None)
        fingerprint,_,_=self._snapshot(evaluation_run_id)
        end=_time(ended_at)
        with self._transaction():
            self._check_binding(evaluation_run_id,fingerprint)
            row=self.db.execute('SELECT * FROM attempts WHERE evaluation_run_id=? AND attempt_id=?',
                                (evaluation_run_id,attempt_id)).fetchone()
            if row is None:
                raise ExecutionLogError('Unknown attempt for evaluation_run_id')
            existing=self.db.execute('SELECT * FROM finishes WHERE attempt_id=?',(attempt_id,)).fetchone()
            if existing:
                if any(existing[k]!=v for k,v in outcome.items()) or (ended_at is not None and existing['ended_at']!=end):
                    raise ExecutionLogError('Conflicting terminal outcome; existing log preserved')
                return dict(existing)
            if end<row['started_at']:
                raise ExecutionLogError('Attempt cannot end before it started')
            self.db.execute('INSERT INTO finishes VALUES (?,?,?,?,?,?,?,?)',
                (attempt_id,evaluation_run_id,end,status,failure_reason,outcome['exception_type'],
                 outcome['exception_message'],outcome['exception_traceback']))
        return dict(attempt_id=attempt_id,evaluation_run_id=evaluation_run_id,ended_at=end,**outcome)

    def recover_run(self,evaluation_run_id):
        """Read persisted attempts, including running work; never auto-finish/retry."""
        fingerprint,_,_=self._snapshot(evaluation_run_id)
        with self._transaction():
            self._check_binding(evaluation_run_id,fingerprint)
            return self._attempt_rows(evaluation_run_id)

    def summary(self,evaluation_run_id):
        """Separate plan, observed attempts, unique events and accepted valid scores.

        Validity comes from completed task results rechecked by result_parser, not
        execution status. Legacy valid results without successful execution logs
        are exposed separately; their execution is never fabricated from imports.
        """
        fingerprint,metadata,tasks=self._snapshot(evaluation_run_id)
        with self._transaction():
            self._check_binding(evaluation_run_id,fingerprint)
            attempts=self._attempt_rows(evaluation_run_id)
        completed=set()
        for task_id,task in tasks.items():
            if task['status']=='completed':
                try:
                    parse_result(task['result'],task['payload'])
                except ResultError as exc:
                    raise ExecutionLogError(f'Invalid completed result {task_id}: {exc}') from exc
                completed.add(task_id)
        observed={a['task_id'] for a in attempts}
        successful={a['task_id'] for a in attempts if a['status']=='succeeded'}
        statuses=Counter(a['status'] for a in attempts)
        per_task_attempts=Counter(a['task_id'] for a in attempts)
        unlinked=completed-successful
        return dict(evaluation_run_id=evaluation_run_id,**metadata,
            planned_comparison_count=len(tasks),
            executed_comparison_count=len(attempts),executed_attempt_count=len(attempts),
            executed_unique_comparison_count=len({a['comparison_event_id'] for a in attempts}),
            retry_attempt_count=len(attempts)-len(observed),
            attempt_status_counts={s:statuses[s] for s in ('running','succeeded','failed','interrupted')},
            valid_comparison_count=len(completed),valid_article_rating_count=2*len(completed),
            valid_logged_comparison_count=len(completed & successful),
            validated_results_without_success_log_count=len(unlinked),
            validated_results_without_success_log_task_ids=sorted(unlinked),
            completed_without_any_execution_log_count=len(completed-observed),
            execution_log_covers_all_completed=not (completed-observed),
            unexecuted_task_count=len(tasks)-len(observed),
            per_task=[dict(task_id=t,task_status=tasks[t]['status'],
                attempt_count=per_task_attempts[t],
                retry_count=max(0,per_task_attempts[t]-1),
                valid_article_rating_count=2 if t in completed else 0) for t in sorted(tasks)],
            task_store=str(self.task_store),log_store=str(self.path),
            execution_counts_source='explicit_persisted_attempt_starts')

    def executed_event_ids(self,evaluation_run_id):
        """Distinct observed events for the existing article_scoring interface.

        This intentionally collapses retries. Missing logs for already-completed
        tasks raise, rather than falsely asserting a complete execution history.
        """
        statistics=self.summary(evaluation_run_id)
        if not statistics['execution_log_covers_all_completed']:
            raise ExecutionLogError('Some completed tasks lack execution logs; cannot supply complete execution evidence')
        return sorted({a['comparison_event_id'] for a in self.recover_run(evaluation_run_id)})

    @staticmethod
    def _export_new(path,text):
        destination=Path(path)
        destination.parent.mkdir(parents=True,exist_ok=True)
        # Exclusive creation prevents ANY existing task, response or log export
        # from being overwritten, including races with another exporter.
        with destination.open('x',encoding='utf-8',newline='\n') as stream:
            stream.write(text)
        return str(destination.resolve())

    def export_jsonl(self,evaluation_run_id,path):
        rows=self.recover_run(evaluation_run_id)
        return self._export_new(path,''.join(json.dumps(dict(schema_version='jqa.execution.v1',**r),ensure_ascii=False)+'\n' for r in rows))

    def export_summary(self,evaluation_run_id,path):
        return self._export_new(path,json.dumps(self.summary(evaluation_run_id),ensure_ascii=False,indent=2)+'\n')


