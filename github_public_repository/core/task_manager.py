"""Transactional local task store and Agent file exchange; no network calls."""
import hashlib
import json
import os
import sqlite3
import tempfile
from collections import Counter
from pathlib import Path

from .result_parser import ResultError, decode_json, parse_result, read_jsonl


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


class TaskManager:
    def __init__(self, directory):
        self.directory = Path(directory).resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.directory / 'tasks.sqlite3')
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY, config TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS tasks(id TEXT PRIMARY KEY, run TEXT NOT NULL,
          payload TEXT NOT NULL, private TEXT NOT NULL, status TEXT NOT NULL, result TEXT);
        CREATE TABLE IF NOT EXISTS errors(id INTEGER PRIMARY KEY, run TEXT, task TEXT,
          kind TEXT, line INTEGER, raw TEXT, reason TEXT);
        CREATE TABLE IF NOT EXISTS exports(id INTEGER PRIMARY KEY, run TEXT, path TEXT, manifest TEXT);
        ''')

    def close(self):
        self.db.close()

    def create_tasks(self, plan, articles, *, evaluation_run_id, model_name,
                     model_version='unknown', prompt_version='jqa.assessment.v1',
                     agent='codex', prompt_text=None, rubric_text=None,
                     instruction_metadata=None):
        if agent not in ('codex', 'chatgpt'):
            raise ValueError('agent must be codex or chatgpt')
        for value in (evaluation_run_id, model_name, model_version, prompt_version):
            if not isinstance(value, str) or not value.strip():
                raise ValueError('run/model/prompt metadata must be nonempty')
        records = list(articles)
        lookup = {a.doi: a for a in records}
        if len(lookup) != len(records):
            raise ValueError('duplicate article identifiers')
        root = Path(__file__).resolve().parents[1]
        # Optional version-selected text; omitted arguments preserve legacy behaviour.
        if (prompt_text is None) != (rubric_text is None):
            raise ValueError('Pass both prompt_text and rubric_text, or neither')
        if prompt_text is None:
            prompt_text = (root / 'references/assessment_prompt.md').read_text(encoding='utf-8-sig')
            rubric_text = (root / 'references/scoring_rubric.md').read_text(encoding='utf-8-sig')
        if any(not isinstance(t, str) or not t.strip() for t in (prompt_text, rubric_text)):
            raise ValueError('Prompt and rubric text must be nonempty')
        instructions = prompt_text + '\n' + rubric_text
        instructions += ('\nReturn jqa.result.v1 JSON with task_id, evaluation_run_id, model_name, '
                         'model_version, prompt_version and articles. Each article must contain '
                         'article_id, originality, significance, rigour, evidence_note, uncertainty. '
                         'Copy routing metadata exactly. If the specified model/version cannot be '
                         'verified, stop and ask the user to configure an unknown-model run.')
        if instruction_metadata is not None:
            if (set(instruction_metadata) != {'rubric_version', 'prompt_sha256', 'rubric_sha256'} or
                    any(not isinstance(v, str) or not v.strip() for v in instruction_metadata.values())):
                raise ValueError('Invalid instruction version metadata')
        pairs = {p.pair_id: p for p in plan.pairs}
        prepared = []
        for event in plan.events:
            pair = pairs[event.pair_id]
            anonymous, private = [], []
            for doi in (pair.article_a, pair.article_b):
                article = lookup[doi]
                token = 'article-' + digest([evaluation_run_id, doi])[:24]
                anonymous.append(dict(article_id=token, title=article.title, abstract=article.abstract))
                private.append(dict(article_id=token, doi=doi, journal_name=article.journal_name))
            payload = dict(schema_version='jqa.task.v1', evaluation_run_id=evaluation_run_id,
                           model_name=model_name, model_version=model_version,
                           prompt_version=prompt_version, instructions=instructions, articles=anonymous)
            if instruction_metadata is not None:
                payload.update(instruction_metadata)  # Task routing only; result schema unchanged.
            task_id = digest([event.event_id, payload])
            payload['task_id'] = task_id
            prepared.append((task_id, canonical(payload), canonical(dict(event_id=event.event_id,
                             pair_id=pair.pair_id, articles=private))))
        config = canonical(dict(agent=agent, model_name=model_name, model_version=model_version,
                                prompt_version=prompt_version, tasks=prepared))
        with self.db:
            old = self.db.execute('SELECT config FROM runs WHERE id=?', (evaluation_run_id,)).fetchone()
            if old and old[0] != config:
                raise ValueError('evaluation_run_id already has different configuration or inputs')
            self.db.execute('INSERT OR IGNORE INTO runs VALUES (?,?)', (evaluation_run_id, config))
            for task_id, payload, private in prepared:
                self.db.execute('INSERT OR IGNORE INTO tasks VALUES (?,?,?,?,?,NULL)',
                                (task_id, evaluation_run_id, payload, private, 'pending'))
        return [p[0] for p in prepared]

    def tasks(self, evaluation_run_id):
        return [dict(payload=json.loads(p), status=s) for p, s in self.db.execute(
            'SELECT payload,status FROM tasks WHERE run=? ORDER BY id', (evaluation_run_id,))]

    def export_tasks(self, evaluation_run_id, path, *, resume=False):
        """Normal export excludes exported work; explicit resume includes unresolved work."""
        destination = Path(path).resolve()
        if destination == self.directory / 'tasks.sqlite3':
            raise ValueError('cannot overwrite task store')
        destination.parent.mkdir(parents=True, exist_ok=True)
        self.db.execute('BEGIN IMMEDIATE')
        temporary = None
        try:
            if not self.db.execute('SELECT 1 FROM runs WHERE id=?', (evaluation_run_id,)).fetchone():
                raise ValueError('unknown evaluation_run_id')
            allowed = ('pending', 'failed', 'exported') if resume else ('pending', 'failed')
            rows = self.db.execute('SELECT id,payload,status FROM tasks WHERE run=? ORDER BY id',
                                   (evaluation_run_id,)).fetchall()
            rows = [(i,p) for i,p,s in rows if s in allowed]
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', newline='\n',
                                             dir=destination.parent, delete=False) as stream:
                temporary = stream.name
                for _, payload in rows:
                    stream.write(payload + '\n')
            os.replace(temporary, destination)
            for task_id, _ in rows:
                self.db.execute("UPDATE tasks SET status='exported' WHERE id=?", (task_id,))
            manifest = dict(task_ids=[i for i,_ in rows], sha256=hashlib.sha256(destination.read_bytes()).hexdigest())
            self.db.execute('INSERT INTO exports(run,path,manifest) VALUES (?,?,?)',
                            (evaluation_run_id, str(destination), canonical(manifest)))
            self.db.commit()
            return manifest
        except Exception:
            self.db.rollback()
            if temporary and os.path.exists(temporary):
                os.unlink(temporary)
            raise

    def import_results(self, evaluation_run_id, path):
        records = list(read_jsonl(path))
        identified = []
        for line, raw in records:
            try:
                obj = decode_json(raw)
                task_id = obj.get('task_id') if isinstance(obj, dict) else None
                if not isinstance(task_id, str):
                    task_id = None
            except ResultError:
                task_id = None
            identified.append((line, raw, task_id))
        counts = Counter(t for _,_,t in identified if t)
        report = dict(completed=0, idempotent=0, errors=0, conflicts=0)
        with self.db:
            if not self.db.execute('SELECT 1 FROM runs WHERE id=?', (evaluation_run_id,)).fetchone():
                raise ValueError('unknown evaluation_run_id')
            for line, raw, task_id in identified:
                row = self.db.execute('SELECT payload,status,result FROM tasks WHERE run=? AND id=?',
                                      (evaluation_run_id, task_id)).fetchone()
                try:
                    if not row:
                        raise ResultError('unknown task for this evaluation run')
                    if counts[task_id] > 1:
                        raise ResultError('multiple records for one task in batch')
                    result = parse_result(raw, json.loads(row[0]))
                    result['articles'].sort(key=lambda a: a['article_id'])
                    accepted = canonical(result)
                    if row[1] == 'completed':
                        if accepted == row[2]:
                            report['idempotent'] += 1
                            continue
                        raise ResultError('conflicting completed result')
                    self.db.execute("UPDATE tasks SET status='completed',result=? WHERE id=?", (accepted, task_id))
                    # Preserve original accepted bytes in the audit table as well.
                    self.db.execute('INSERT INTO errors(run,task,kind,line,raw,reason) VALUES (?,?,?,?,?,?)',
                                    (evaluation_run_id, task_id, 'accepted_raw', line, raw, 'validated'))
                    report['completed'] += 1
                except ResultError as exc:
                    conflict = bool(row and (row[1] == 'completed' or counts[task_id] > 1))
                    kind = 'conflict' if conflict else 'error'
                    self.db.execute('INSERT INTO errors(run,task,kind,line,raw,reason) VALUES (?,?,?,?,?,?)',
                                    (evaluation_run_id, task_id, kind, line, raw, str(exc)))
                    if row and row[1] != 'completed':
                        self.db.execute("UPDATE tasks SET status='failed' WHERE id=?", (task_id,))
                    report['conflicts' if conflict else 'errors'] += 1
        return report

    def results(self, evaluation_run_id):
        """Run-scoped results only; intentionally no combined-model accessor."""
        return [json.loads(r[0]) for r in self.db.execute(
            'SELECT result FROM tasks WHERE run=? AND status=? ORDER BY id', (evaluation_run_id, 'completed'))]

    def errors(self, evaluation_run_id):
        return [dict(task_id=t, kind=k, line=n, raw=r, reason=e) for t,k,n,r,e in self.db.execute(
            "SELECT task,kind,line,raw,reason FROM errors WHERE run=? AND kind!='accepted_raw' ORDER BY id",
            (evaluation_run_id,))]
