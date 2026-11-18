# Pairwise Comparison Design V0.2 — K-Regular Random Graph

This specification supersedes earlier internal-only/cross-journal-only selection,
hard budget caps, and repeat-allocation rules. All eligible articles are vertices
in ONE undirected graph; journal membership never restricts edges in either mode.

## Degree selection

Let N>=2 and target B=100,000 (the existing budget parameter remains configurable).
All unique pairs: U_max=N*(N-1)/2. If U_max<=B, use the complete graph: K=N-1.
Otherwise select K in [0,N-1] with N*K even minimizing abs(N*K/2-B). Equal-distance
choices use smaller K. Use integer arithmetic, including odd N (K must be even).
B is now a target, NOT a strict ceiling: 5,923 articles yield K=34 and 100,691
comparisons. There is no repeat-based padding. Each edge generates one event.

| N | Target | K | Generated comparisons U=T | R |
|---|---|---|---|---|
| 100 | 100,000 | 99 | 4,950 | 0 |
| 500 | 100,000 | 400 | 100,000 | 0 |
| 5,923 | 100,000 | 34 | 100,691 | 0 |

## Verified construction and randomization

1. Sort stable article IDs so input row order cannot change the output.
2. For construction degree d, use edges with circular offsets 1 through floor(d/2).
   If d is odd, N must be even; add an antipodal perfect matching. These offset
   sets are disjoint and each vertex has degree d.
3. Shuffle vertex labels with a private random.Random(seed) instance.
4. Make 10*E_d double-edge-switch proposals (zero if E_d<2). Select two edges
   uniformly without replacement. For four distinct endpoints, choose either
   alternative pairing with equal probability. Accept only if both new edges are
   absent. Otherwise leave the graph unchanged. Every accepted switch preserves
   all degrees and simplicity. No unbounded rejection loop is used.
5. For dense K>(N-1)/2, construct/randomize degree d=N-1-K then take its complement;
   otherwise d=K. Complementation gives each vertex exactly K neighbours.
6. Verify vertex ranges, distinct unordered edges, no self-loops, E=N*K/2 and
   every vertex degree exactly K. Raise GraphConstructionError on any failure;
   there is no fallback to unbalanced sampling.

This is a randomized constructive design, NOT a proven uniform sampler over all
labelled K-regular graphs. Ten proposals per construction edge is a documented
heuristic, not a mixing guarantee or a promise of sufficient randomness for every
research application. Record accepted/proposed switches and the complement flag.
Rejected switches are normal, not construction failures. Complete/empty graphs
have no edge-set randomness; finite seeds can yield identical nontrivial graphs.
If uniform graph sampling becomes a research requirement, a separate validated
sampler and diagnostics are required; do not relabel this design as uniform.

## Output and compatibility

The existing Pair, ComparisonEvent, ComparisonPlan and generate_comparison_plan
interfaces remain. mode='single'/'multi' labels journal scope, not edge restrictions.
Single mode expects one journal; multi expects at least two. Old internal=True and
repeats_per_pair!=0 now raise ValueError explicitly. Their default values remain
accepted so existing TaskManager calls need no changes.

plan.k is K; plan.design_summary provides target, generated plan count, difference
from target, U/T/R, eligible unique-pair maximum, per-article counts, minimum,
maximum, mean, population standard deviation, and component counts. Full article
and journal components are available separately. All planned article counts equal
K; plan R=0. Each event has repetition_index=1 and a unique ID. Versioned event
IDs distinguish this design from legacy plans. Use a new evaluation_run_id for a
new plan; do not overwrite an existing run or its accepted results.

A generated comparison plan does not mean models have executed it. Use
summarize_events(plan, executed_event_ids) for actual U/T/R and participation
statistics. Until all planned events execute, actual exposure may be uneven.
Failures likewise mean valid-rating counts may differ. Keep planned, executed and
valid counts separate. Technical retry attempts belong in execution-layer audit
records, not additional designed edges or independent articles.

Report article-graph components without enforcing connectivity. A connected
journal projection does not prove that the article graph is connected, nor that
scores are calibrated. Different models, runs or comparison contexts are not
presumed comparable. This design does not establish a validated common ranking.

## Resource and input boundaries

At least two distinct eligible articles are required. K=0 is allowed for a zero
or very small target and is reported as N isolated components, not an evaluation.
K=1 yields a matching; disconnection is allowed. Sparse generation uses O(N+NK)
space/time apart from sorting and fixed-count switches. Dense output and complete
graphs inherently need O(N^2) space; complement enumeration also takes O(N^2)
time. Pair/event/component materialization uses additional memory proportional to
output. No scale-unbounded performance guarantee is made. Reproducibility is for
the same algorithm version, Python runtime, stable IDs, metadata and seed; pin
these in research records. No external dependencies or model APIs are used.

## Validation and unchanged scoring

Offline tests include all required large cases, odd/even N and K, reversed input
order, deterministic seeds, failure injection, disconnected graphs, independent
reconstruction of adjacency/degrees/components, and exhaustive enumeration of all
simple graphs through N=5 to check feasible degree selection. The independent
oracle does not call the production graph validator.

LLM_REF remains each article's own mean of valid assessment scores (each is the
mean of Originality, Significance and Rigour). JournalScore remains the equal-weight
mean of distinct valid article LLM_REF. No BT_score or Bayesian shrinkage is used.
