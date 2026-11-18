---
name: journal-quality-assessment
description: Evaluate user supplied article CSV data and journal quality with REF inspired content ratings. Use for dataset validation, balanced pair preparation, Agent or DeepSeek execution, interrupted run recovery and score export.
---

# Journal Quality Assessment

Use this Skill for user-supplied Chinese/English paper titles and abstracts. Locate
`scripts/public_cli.py` relative to this SKILL.md and invoke it with an absolute path
when the working directory differs. Python 3.10+ is required; diagnose with `doctor`.
If the Agent host cannot execute local Python/files or approved HTTPS, explain the
missing capability and provide CLI instructions; a plugin install does not grant it.

Read [workflow and commands](references/public_workflow.md) for execution/recovery,
[method](references/public_method.md) for interpretation, and [privacy](PRIVACY.md)
before transmitting user content. Never load all private mappings into an evaluator.

## Workflow

1. Validate CSV fields `doi,journal_name,title,abstract,year`; report missing values,
   duplicates/conflicts, suspected nonresearch records and identity clues. Do not
   fabricate abstracts, silently exclude requested papers or infer quality from prestige.
2. Prepare a fresh user-owned output directory with `prepare --csv ... --mode single|multi
   --provider agent|deepseek`. No personal reference workbook is required. Keep output
   outside the installed code, record provenance and use independent runs per model.
3. Agent mode exports anonymous tasks for one-pair-at-a-time assessment; read the
   complete frozen instructions. Record actual start only when evaluation begins,
   save original UTF-8 JSONL, import/validate immediately and retain execution evidence.
4. DeepSeek mode estimates charges and displays transmitted fields. Obtain explicit
   user authorization for third-party transmission and spending before using `run`
   with `--allow-paid --confirm-send`; never treat preparing a run as paid consent.
   Keys go through hidden local input or ignored local credentials, never through chat,
   task files or logs. If a budget is in CNY, ask for an explicit conversion rate/date
   or use a user-approved USD budget. Do not guess an exchange rate.
5. Resume the SAME run, skipping completed tasks. Unknown POST delivery or a persistent
   safety stop requires audit of receipts/attempts, not automatic re-evaluation.
   `api-reconcile` records a specifically authorized retry disposition; it does not call
   the model. Keep prior responses and cost holds. Stop new work on serious errors.
6. Export articles/journals, coverage, distributions, variability, evidence, raw
   responses and logs. Label incomplete comparisons partial_not_final; do not call
   partial journal scores final. Never merge runs/models to improve coverage.

## Immutable method

Use existing core algorithms. All valid articles form one undirected graph,
irrespective of journal. Complete comparison if N(N-1)/2<=100000; otherwise choose
feasible regular K nearest the target, with NK even. No self-pair, repeated pair or
budget-padding repetitions. The target may be exceeded by the nearest feasible K.

Each article independently receives integer Originality, Significance and Rigour
1–4, its own evidence_note and low/medium/high uncertainty. Same scores are valid;
missing abstract details increase uncertainty, not automatic penalties. Preserve
jqa.result.v1 and pinned Prompt jqa.assessment.v1.2 / Rubric jqa.rubric.v1.1.
First average each article's valid instances, then distinct articles equally within
a journal. No BT_score, Bayesian adjustment, reputation or uncertainty weighting.
This is an engineered adaptation, not an official REF score or exact reproduction
of all experiments/algorithms in the cited paper. Cross-model/discipline/language
comparability requires separate validation.

## Natural-language routing

- “评价这个CSV” → validate, report issues, prepare the chosen mode/provider, ask only
  for missing material choices and paid/data-transfer authorization.
- “最多花费10元” → freeze a user-approved USD equivalent with conversion provenance.
- “恢复上次评价” → identify the user's run, inspect status/receipts, resume safely.
- “导出论文评分表和期刊排名” → export equal-paper journal means and coverage;
  do not imply calibrated rankings or final coverage from an incomplete run.

The packaged `demo` is deliberately synthetic and offline. Never substitute its
fixture scores for user research or present them as real model judgments.
