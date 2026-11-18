# REF-framework adapted scoring rubric

Version: jqa.rubric.v1.1
Compatible prompt: jqa.assessment.v1.2

## A. Source framework — paraphrased, not verbatim per-star operational anchors

Source: Li, Niu and Jiang (2026), From paper content to journal quality: an
LLM-based comparative assessment approach within the REF 2021 framework,
DOI 10.1007/s11192-026-05838-0. Table 1 (PDF page 10), Appendix Fig. 3 (page 22).
The study's REF context is Main Panel A, UoA 5 (Biological Sciences). Using this
Skill elsewhere does not establish disciplinary calibration or official REF status.

- Originality: the novel and important contribution of research to knowledge and
  understanding in the field. The Appendix also refers to methods and applications.
- Significance: actual or potential influence on knowledge, scholarly thought,
  policy or practice; the Appendix includes realistic influence on further research.
- Rigour: intellectual coherence, scholarly integrity, and the appropriateness and
  robustness of concepts/theory, analyses and methods. Missing reporting detail
  alone does not demonstrate methodological weakness or lack of integrity.

## B. Common star semantics — shared across all three dimensions

- 4★ World-leading.
- 3★ Internationally excellent, below the highest standard of excellence.
- 2★ Internationally recognised.
- 1★ Nationally recognised.

These denote research quality, not geographic author identity, international
co-authorship, journal reputation, citation counts or the size of a topic's audience.
A narrow specialist contribution can be of high quality. Do not require broad
cross-field reach as a necessary condition for the highest rating.

## C. Integer adaptation — explicit departure from the Appendix

Assign each dimension a JSON INTEGER 1, 2, 3 or 4. The Appendix permits decimals
and gives 4★=3.8–4.0, 3★=3.0–3.7, 2★=2.0–2.9, 1★=1.0–1.9, plus unclassified<1.
The main text also describes discrete star categories. This implementation keeps
only integer categories and does NOT implement those decimal bands or unclassified.
Do not generate decimals and silently round them. This is an operational,
discretized adaptation, not exact reproduction or verified star-equivalent calibration.
Use the framework in A and the shared level semantics in B directly. The Skill
illustrations below are not the paper's verbatim twelve dimension-by-star anchors.

## D. Skill operational guidance — additions, not source quotations

### Originality
Assess the novelty and importance of the stated contribution, including questions,
concepts, methods, evidence, findings or synthesis. Explain how the available text
supports the selected level. Do not infer novelty merely from promotional language,
and do not turn an absence of context into an automatic lowest or middle score.

### Significance
Assess the evidenced contribution and plausible potential to influence knowledge,
understanding, scholarly thought, further research, policy or practice. Do not
require already realized impact, citations, global dissemination or a broad topic.
Ground claims about potential in the supplied text, not speculation or reputation.

### Rigour
Assess evidence of coherence and methodological/conceptual appropriateness and
robustness. Distinguish explicit problems (for example, a stated conclusion not
supported by the described design) from details simply not reported in the abstract.
Do not assign a low score just because sample size, robustness tests or other
method details are absent. Conversely, do not assume unreported strengths.

### Evidence, equal scores and uncertainty
- Give each article three separate integer scores and its own evidence_note.
- Use all levels when warranted, but do not impose equal frequencies or extreme-score quotas.
- Avoid mechanically defaulting to middle categories without judgment; distinguish
  levels only when supported by textual evidence. These points are discussed in
  the paper's main text (page 11), though not printed in its Fig. 3 example.
- Two articles may have identical scores in all dimensions. No forced difference.
- Do not favour an article because it appears second/last or first.
- Unreported detail is not itself negative quality evidence. Appropriately increase
  uncertainty for insufficient evidence; do not automatically lower or raise scores.
- Use uncertainty low/medium/high and explain material limits in evidence_note.
  Uncertainty is not a fourth quality dimension or a numerical adjustment.
- Evidence notes must refer to the supplied title/abstract. Do not invent methods,
  results or hidden strengths, and do not use identity or reputation signals.

## E. Unchanged output and aggregation contract

Keep jqa.result.v1 unchanged: only the existing article scores, evidence_note and
uncertainty plus existing routing metadata. No binary winner or extra score fields.
Each instance score remains the arithmetic mean of three dimensions; each article
averages its own valid instances; journal means give distinct valid articles equal
weight. No BT_score, Bayesian shrinkage, or uncertainty-based reweighting is added.
