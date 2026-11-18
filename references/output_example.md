# Synthetic output example / 合成输出示例

These values come from the packaged offline demo. They are fixed plumbing fixtures,
not real model judgments, publications or journal quality results.

| Synthetic article | Valid ratings | Originality mean | Significance mean | Rigour mean | LLM_REF | Population SD |
|---|---:|---:|---:|---:|---:|---:|
| jqa-demo-001 | 3 | 2 | 2 | 2 | 2 | 0 |
| jqa-demo-002 | 3 | 2 | 2 | 2 | 2 | 0 |
| jqa-demo-003 | 3 | 2 | 2 | 2 | 2 | 0 |
| jqa-demo-004 | 3 | 2 | 2 | 2 | 2 | 0 |

Each fictional journal contains two of these papers, coverage=100%, equal-article
mean=2 and SD=0. The complete four-paper graph has6 comparisons and12 rating instances.
The CSVs contain assessment_kind=SYNTHETIC_DEMO_ONLY; evidence in scores.json and
raw receipts also says synthetic. Do not interpret SD=0 as scientific reliability.

Actual user exports include DOI/journal mappings locally, per-paper evidence and
source instances, coverage, dimension distributions and context variability. They
must not be shared without checking rights/privacy. Incomplete real results are
partial_not_final and missing paper scores are blank/null, never zero.
