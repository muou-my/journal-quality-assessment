"""Run-isolated, read-only article aggregation from accepted task-store results."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from statistics import mean, pstdev
from typing import Iterable

from .data_processing import Article
from .result_parser import ResultError, decode_json, parse_result
from .task_manager import digest

DIMENSIONS = ('originality', 'significance', 'rigour')


class ScoringError(ValueError):
    """Incomplete/inconsistent provenance or corrupted accepted results."""


@dataclass
class ArticleScoreReport:
    evaluation_run_id: str
    model_name: str
    model_version: str
    prompt_version: str
    articles: tuple[dict, ...]
    run_statistics: dict
    provenance: dict


def aggregate_article_scores(store_directory: str | Path, evaluation_run_id: str,
                             articles: Iterable[Article], *,
                             executed_event_ids: Iterable[str] | None = None) -> ArticleScoreReport:
    """Read one committed SQLite snapshot without changing any stored data.

    `articles` must be the valid input roster used for this run. It supplies the
    coverage denominator, including papers with no tasks. Known task identities
    are checked against the task manager's private mapping and input text. The
    legacy store cannot prove the completeness of a zero-task roster; callers
    must preserve the original valid-input roster for such runs.

    `executed_event_ids`, when supplied, is caller-attested execution evidence for
    distinct planned events, not retry-attempt counts. Without it, actual execution
    is unknown; exported/failed statuses must not be used to infer execution.
    Population SD is computed over each paper's valid per-instance composite
    scores. A single rating has SD=0; an unscored paper has null scores/SD.
    """
    source = (Path(store_directory).resolve() / 'tasks.sqlite3')
    roster = list(articles)
    by_doi = {a.doi: a for a in roster}
    if len(by_doi) != len(roster) or any(not a.doi or not a.journal_name for a in roster):
        raise ScoringError('Roster requires unique nonempty article and journal identifiers')
    db = sqlite3.connect(source.as_uri() + '?mode=ro', uri=True)
    try:
        db.execute('BEGIN')
        row = db.execute('SELECT config FROM runs WHERE id=?', (evaluation_run_id,)).fetchone()
        if row is None:
            raise ScoringError('Unknown evaluation_run_id')
        config = decode_json(row[0])
        task_rows = db.execute('SELECT id,payload,private,status,result FROM tasks WHERE run=? ORDER BY id',
                               (evaluation_run_id,)).fetchall()
    finally:
        db.close()
    metadata = {key: config[key] for key in ('model_name', 'model_version', 'prompt_version')}
    output = {}
    for article in sorted(roster, key=lambda a: a.doi):
        token = 'article-' + digest([evaluation_run_id, article.doi])[:24]
        if token in output:
            raise ScoringError('Anonymous article ID collision')
        output[token] = dict(article_id=token, doi=article.doi, journal_name=article.journal_name,
                             planned_comparison_count=0, executed_comparison_count=None,
                             valid_evaluation_count=0, originality_mean=None,
                             significance_mean=None, rigour_mean=None, llm_ref=None,
                             score_std=None, scored=False, sources=[])
    statuses = {key: 0 for key in ('pending', 'exported', 'completed', 'failed')}
    event_articles = {}
    completed_events = set()
    task_hashes = []
    for task_id, raw_payload, raw_private, status, raw_result in task_rows:
        payload, private = decode_json(raw_payload), decode_json(raw_private)
        if status not in statuses:
            raise ScoringError(f'Unknown status for task {task_id}')
        statuses[status] += 1
        if payload.get('task_id') != task_id or payload.get('evaluation_run_id') != evaluation_run_id:
            raise ScoringError('Task/run identity mismatch in store')
        if any(payload.get(key) != value for key, value in metadata.items()):
            raise ScoringError('Mixed model/version/prompt metadata in run')
        event_id = private['event_id']
        if not isinstance(event_id, str) or event_id in event_articles:
            raise ScoringError('Duplicate or invalid comparison event in run')
        mapping = private['articles']
        if len(mapping) != 2 or len({a['article_id'] for a in mapping}) != 2:
            raise ScoringError('Invalid local article mapping')
        if len(payload['articles']) != 2 or {a['article_id'] for a in payload['articles']} != {a['article_id'] for a in mapping}:
            raise ScoringError('Payload/private mapping mismatch')
        for local in mapping:
            article = by_doi.get(local['doi'])
            token = local['article_id']
            if article is None or token not in output or output[token]['doi'] != article.doi:
                raise ScoringError('Roster missing a planned article or ID mapping mismatch')
            if article.journal_name != local['journal_name']:
                raise ScoringError('Journal mapping conflict')
            exported = next(a for a in payload['articles'] if a['article_id'] == token)
            if article.title != exported['title'] or article.abstract != exported['abstract']:
                raise ScoringError('Roster content differs from original task')
            output[token]['planned_comparison_count'] += 1
        event_articles[event_id] = tuple(a['article_id'] for a in mapping)
        task_hashes.append(dict(task_id=task_id, payload_sha256=hashlib.sha256(raw_payload.encode()).hexdigest()))
        if status != 'completed':
            continue
        try:
            result = parse_result(raw_result, payload)
        except ResultError as exc:
            raise ScoringError(f'Invalid completed result {task_id}: {exc}') from exc
        completed_events.add(event_id)
        result_hash = hashlib.sha256(raw_result.encode()).hexdigest()
        for rating in result['articles']:
            score = mean(rating[d] for d in DIMENSIONS)
            output[rating['article_id']]['sources'].append({
                'task_id': task_id, 'comparison_event_id': event_id, 'pair_id': private['pair_id'],
                'evaluation_run_id': evaluation_run_id, **metadata,
                'result_sha256': result_hash, **rating, 'composite_score': score,
            })
    actual_count = None
    if executed_event_ids is not None:
        executed = list(executed_event_ids)
        if len(executed) != len(set(executed)) or set(executed) - event_articles.keys():
            raise ScoringError('Executed event IDs must be distinct known events of this run')
        if not completed_events <= set(executed):
            raise ScoringError('Execution evidence omits completed events')
        actual_count = len(executed)
        for paper in output.values():
            paper['executed_comparison_count'] = 0
        for event in executed:
            for token in event_articles[event]:
                output[token]['executed_comparison_count'] += 1
    for paper in output.values():
        sources = paper['sources']
        if sources:
            paper.update(scored=True, valid_evaluation_count=len(sources),
                         llm_ref=mean(r['composite_score'] for r in sources),
                         score_std=pstdev(r['composite_score'] for r in sources))
            for dimension in DIMENSIONS:
                paper[dimension + '_mean'] = mean(r[dimension] for r in sources)
    statistics = dict(planned_comparison_count=len(task_rows),
                      executed_comparison_count=actual_count,
                      execution_count_source='caller_supplied_event_ids' if actual_count is not None else 'unknown',
                      executed_retry_attempt_count=None,
                      valid_comparison_count=len(completed_events),
                      valid_article_rating_count=2 * len(completed_events),
                      task_status_counts=statuses,
                      total_article_count=len(output),
                      scored_article_count=sum(a['scored'] for a in output.values()))
    return ArticleScoreReport(evaluation_run_id=evaluation_run_id, **metadata,
        articles=tuple(output.values()), run_statistics=statistics,
        provenance=dict(store_path=str(source), agent=config['agent'], task_hashes=task_hashes,
                        roster_sha256=digest([vars(a) for a in sorted(roster,key=lambda a:a.doi)]),
                        standard_deviation='population', actual_execution_not_inferred_from_export=True))
