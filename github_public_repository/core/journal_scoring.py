"""Equal-paper journal aggregation, never a pooled average of rating instances."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from statistics import mean, median, pstdev

from .article_scoring import ArticleScoreReport, DIMENSIONS, ScoringError


@dataclass
class JournalScoreReport:
    evaluation_run_id: str
    model_name: str
    model_version: str
    prompt_version: str
    journals: tuple[dict, ...]
    run_statistics: dict
    provenance: dict


def aggregate_journal_scores(article_report: ArticleScoreReport) -> JournalScoreReport:
    """Group unique papers by validated local journal name in ONE article report.

    Coverage denominator includes every article in the supplied original valid
    roster, not every imported rating. Unscored values remain None. Journal SD and
    distribution describe article LLM_REF, not all per-comparison rating instances.
    Source model/run/prompt metadata is checked again before any averaging.
    """
    if not isinstance(article_report, ArticleScoreReport):
        raise ScoringError('Expected one run-isolated ArticleScoreReport, not combined runs')
    metadata = {key:getattr(article_report,key) for key in
                ('evaluation_run_id','model_name','model_version','prompt_version')}
    journals = defaultdict(list)
    seen = set()
    for paper in article_report.articles:
        if paper['article_id'] in seen:
            raise ScoringError('Duplicate independent article in report')
        seen.add(paper['article_id'])
        for source in paper['sources']:
            if any(source.get(key) != value for key,value in metadata.items()):
                raise ScoringError('Mixed run/model/prompt sources')
        if paper['scored'] != bool(paper['sources']) or paper['valid_evaluation_count'] != len(paper['sources']):
            raise ScoringError('Inconsistent valid-rating counts')
        journals[paper['journal_name']].append(paper)
    output = []
    for name, papers in sorted(journals.items()):
        scored = [p for p in papers if p['scored']]
        values = [p['llm_ref'] for p in scored]
        count = len(scored)
        row = dict(journal_name=name, total_article_count=len(papers),
                   scored_article_count=count, unscored_article_count=len(papers)-count,
                   coverage=count/len(papers), partial_sample=count<len(papers),
                   evaluation_status='unscored' if not count else ('partial_sample' if count<len(papers) else 'complete'),
                   originality_mean=None, significance_mean=None, rigour_mean=None,
                   journal_score=mean(values) if values else None,
                   article_score_std=pstdev(values) if values else None,
                   article_score_distribution=[dict(article_id=p['article_id'],llm_ref=p['llm_ref']) for p in scored],
                   distribution_summary=dict(min=min(values),max=max(values),median=median(values)) if values else None,
                   unscored_article_ids=[p['article_id'] for p in papers if not p['scored']],
                   valid_article_rating_count=sum(p['valid_evaluation_count'] for p in papers),
                   source_task_ids=sorted({s['task_id'] for p in papers for s in p['sources']}))
        if scored:
            for dimension in DIMENSIONS:
                row[dimension+'_mean'] = mean(p[dimension+'_mean'] for p in scored)
        output.append(row)
    return JournalScoreReport(**metadata, journals=tuple(output),
                              run_statistics=dict(article_report.run_statistics),
                              provenance={**article_report.provenance,
                                          'weighting':'equal distinct valid articles',
                                          'article_scores_include_source_records':True})
