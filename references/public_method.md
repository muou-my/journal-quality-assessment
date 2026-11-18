# Scientific method and reproducibility

Method source: Li, Niu and Jiang (2026), *From paper content to journal quality:
an LLM-based comparative assessment approach within the REF2021 framework*,
https://doi.org/10.1007/s11192-026-05838-0. The distributed definitions are paraphrases,
with separately labelled operational additions; no paper PDF is bundled.

This software is an engineering implementation and adaptation. It does not claim
to reproduce all original experiments, models, decimal ratings, Bradley–Terry
procedures or Bayesian adjustments. Current scoring uses JSON integers1–4 only.
It is REF-inspired rather than an official REF assessment, and is not validated
as a sole basis for consequential journal, institution or individual decisions.

All eligible articles form one simple graph independent of journal membership.
If N(N-1)/2<=100000, use all pairs. Otherwise pick feasible K with NK even, closest
to the target100000 (ties lower K); construct with preserved degree switches and
verify every degree, no loops and no repeated pairs. The nearest feasible count
may exceed the target. Finite switches do not promise uniform graph sampling.
Every article has K planned participations; actual incomplete execution can differ.

Rate each article independently on Originality, Significance and Rigour1–4 with
text evidence and low/medium/high uncertainty. Equal scores are permitted; no
forced winner. Missing abstract method detail is uncertainty, not automatic fault.
One event score is the mean of the three dimensions. Article LLM_REF is the mean
of its own valid event scores; aggregate each dimension separately. Journal
LLM_REF is the equal-weight mean across distinct valid articles. Repeated attempts
never add independent papers; missing papers do not get zero. No BT_score,
Bayesian shrinkage, prestige or uncertainty weighting. Journal dimension means
also average per-article dimension means, not pooled event ratings.

Keep model/run/Prompt versions separate. Preserve CSV and pair plan hashes,
instruction version bytes, code hashes, requests, raw replies, usage, provenance
and exclusions. Report total/scored papers, task/rating counts, coverage and
population SD. A single observation's SD=0 is not stability evidence. Context
variation and score concentration are descriptive, not proof of bias or calibration.

Prompt jqa.assessment.v1.2 and Rubric jqa.rubric.v1.1 are byte-preserved from the
development lineage; their SHA-256 pins are in version_registry.json. Historical
v1.1/v1 texts are included for regression reproducibility, not the default method.
Public synthetic examples are deliberately fictional, including DOI-shaped IDs.
User abstracts and third-party model responses retain their separate rights.
