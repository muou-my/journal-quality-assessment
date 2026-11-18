"""Pairwise Comparison Design V0.2: simple K-regular randomized graphs.

No model calls, repeated comparisons, scoring, or connectivity enforcement.
Random relabelling and finite degree-preserving switches are NOT a uniform sampler
of all labelled regular graphs. See references/comparison_method.md.
"""
from __future__ import annotations

import hashlib
import random
from collections import Counter
from dataclasses import dataclass, field
from statistics import mean, pstdev
from typing import Iterable

from .data_processing import Article

ALGORITHM_VERSION = "k_regular_switch_v1"
SWITCH_ATTEMPTS_PER_EDGE = 10


class GraphConstructionError(RuntimeError):
    """A constructed graph failed its required simple/regular invariants."""


@dataclass(frozen=True)
class Pair:
    pair_id: str
    article_a: str
    article_b: str
    journal_a: str
    journal_b: str


@dataclass(frozen=True)
class ComparisonEvent:
    event_id: str
    pair_id: str
    repetition_index: int  # Always 1 under Design V0.2.
    status: str = "planned"


@dataclass
class ComparisonPlan:
    # Existing fields and their order are retained for callers and TaskManager.
    mode: str
    seed: int
    budget: int  # Target, not a hard ceiling, under Design V0.2.
    eligible_unique_pairs: int
    independent_article_count: int
    pairs: tuple[Pair, ...]
    events: tuple[ComparisonEvent, ...]
    pair_event_counts: dict[str, int]
    article_comparison_counts: dict[str, int]
    journal_components: tuple[tuple[str, ...], ...]
    repeats_per_pair: int = 0
    k: int = 0
    article_components: tuple[tuple[str, ...], ...] = ()
    algorithm_version: str = ALGORITHM_VERSION
    randomization: dict[str, int | bool] = field(default_factory=dict)

    @property
    def planned_counts(self) -> dict[str, int]:
        return {"U": len(self.pairs), "T": len(self.events),
                "R": len(self.events) - len(self.pairs)}

    @property
    def design_summary(self) -> dict:
        """Plan statistics, NOT evidence that any comparison has been executed."""
        return {
            "target_comparisons": self.budget,
            "planned_comparisons": len(self.events),
            "difference_from_target": len(self.events) - self.budget,
            "K": self.k,
            "eligible_unique_pairs": self.eligible_unique_pairs,
            **self.planned_counts,
            "article_comparison_counts": dict(self.article_comparison_counts),
            "participation_statistics": participation_statistics(self.article_comparison_counts),
            "article_component_count": len(self.article_components),
            "journal_component_count": len(self.journal_components),
            "algorithm_version": self.algorithm_version,
            "randomization": dict(self.randomization),
        }


def participation_statistics(counts: dict[str, int]) -> dict[str, int | float]:
    """Population statistics, including zero-exposure papers."""
    values = list(counts.values())
    if not values:
        return {"min": 0, "max": 0, "mean": 0.0, "std": 0.0}
    return {"min": min(values), "max": max(values),
            "mean": mean(values), "std": pstdev(values)}


def choose_regular_degree(n: int, target: int = 100_000) -> int:
    """Select nearest feasible NK/2 using integer arithmetic; ties choose lower K.

    A simple undirected K-regular graph exists exactly when 0<=K<N and NK is
    even. If all pairs fit the target, return N-1 (the complete graph).
    """
    if type(n) is not int or n < 2:
        raise ValueError("At least two eligible articles are required")
    if type(target) is not int or target < 0:
        raise ValueError("target must be a nonnegative integer")
    if n * (n - 1) // 2 <= target:
        return n - 1
    step = 2 if n % 2 else 1
    lower = (2 * target // (n * step)) * step
    choices = {0, lower, min(lower + step, n - 1)}
    return min(choices, key=lambda k: (abs(n * k - 2 * target), k))


def _edge(a: int, b: int) -> tuple[int, int]:
    return (a, b) if a < b else (b, a)


def validate_regular_graph(n: int, k: int, edges: Iterable[tuple[int, int]]) -> tuple[int, ...]:
    """Check all invariants without trusting edge-generation bookkeeping."""
    if type(n) is not int or n < 2 or type(k) is not int or not 0 <= k < n or n * k % 2:
        raise GraphConstructionError("Invalid N/K for a simple regular graph")
    seen = set()
    degrees = [0] * n
    for item in edges:
        if not isinstance(item, (tuple, list)) or len(item) != 2:
            raise GraphConstructionError("Invalid edge representation")
        a, b = item
        # bool is not accepted as a vertex index.
        if type(a) is not int or type(b) is not int or not 0 <= a < n or not 0 <= b < n:
            raise GraphConstructionError("Edge has an invalid vertex")
        if a == b:
            raise GraphConstructionError("Self-loop detected")
        pair = _edge(a, b)
        if pair in seen:
            raise GraphConstructionError("Duplicate unordered edge detected")
        seen.add(pair)
        degrees[a] += 1
        degrees[b] += 1
    if len(seen) != n * k // 2 or any(d != k for d in degrees):
        raise GraphConstructionError("Graph is not exactly K-regular")
    return tuple(degrees)


def _regular_edges(n: int, k: int, seed: int) -> tuple[list[tuple[int, int]], dict]:
    """Construct a circulant (plus matching), randomize, optionally complement.

    Fixed proposal count avoids failure/retry loops on dense or rigid graphs.
    Rejected proposals leave the graph unchanged. Every legal accepted proposal
    replaces two disjoint edges with two absent edges and preserves all degrees.
    """
    rng = random.Random(seed)
    complement = k > (n - 1) // 2
    degree = n - 1 - k if complement else k
    labels = list(range(n))
    rng.shuffle(labels)
    edges = set()
    for offset in range(1, degree // 2 + 1):
        for vertex in range(n):
            edges.add(_edge(labels[vertex], labels[(vertex + offset) % n]))
    if degree % 2:
        if n % 2:
            raise GraphConstructionError("Odd degree requires even N")
        for vertex in range(n // 2):
            edges.add(_edge(labels[vertex], labels[vertex + n // 2]))
    validate_regular_graph(n, degree, edges)
    edge_list = sorted(edges)  # Never depend on hash-set iteration order.
    attempts = SWITCH_ATTEMPTS_PER_EDGE * len(edge_list) if len(edge_list) >= 2 else 0
    accepted = 0
    for _ in range(attempts):
        i = rng.randrange(len(edge_list))
        j = rng.randrange(len(edge_list) - 1)
        if j >= i:
            j += 1
        a, b = edge_list[i]
        c, d = edge_list[j]
        if len({a, b, c, d}) != 4:
            continue
        if rng.getrandbits(1):
            c, d = d, c
        first, second = _edge(a, c), _edge(b, d)
        if first in edges or second in edges:
            continue
        edges.remove(edge_list[i])
        edges.remove(edge_list[j])
        edges.update((first, second))
        edge_list[i], edge_list[j] = first, second
        accepted += 1
    validate_regular_graph(n, degree, edges)
    if complement:
        edges = {(a, b) for a in range(n) for b in range(a + 1, n) if (a, b) not in edges}
    result = sorted(edges)
    validate_regular_graph(n, k, result)
    return result, {"used_complement": complement, "switch_attempts": attempts,
                    "accepted_switches": accepted}


def _components(neighbours: dict) -> tuple[tuple, ...]:
    unseen = set(neighbours)
    result = []
    while unseen:
        stack, component = [min(unseen)], set()
        while stack:
            vertex = stack.pop()
            if vertex in component:
                continue
            component.add(vertex)
            stack.extend(neighbours[vertex] - component)
        unseen -= component
        result.append(tuple(sorted(component)))
    return tuple(result)


def generate_comparison_plan(articles: Iterable[Article], *, mode: str,
                             budget: int = 100_000, seed: int = 0,
                             internal: bool = False,
                             repeats_per_pair: int = 0) -> ComparisonPlan:
    """Generate the same all-article regular design for single and multi modes.

    mode remains a dataset label, not an edge restriction. budget is the target
    edge count and may be exceeded by nearest feasible NK/2. internal=True and
    nonzero repeats_per_pair are rejected explicitly, not silently reinterpreted.
    N<2 is rejected; target=0 produces a valid edgeless graph (K=0). Small positive
    targets may also select K=0 as the closest feasible degree. Components are
    reported but never forced to connect. No existing plan or Article is mutated.
    """
    if mode not in {"single", "multi"}:
        raise ValueError("mode must be 'single' or 'multi'")
    if type(budget) is not int or budget < 0:
        raise ValueError("budget must be a nonnegative integer target")
    if type(seed) is not int or type(internal) is not bool:
        raise ValueError("seed must be integer and internal must be boolean")
    if internal:
        raise ValueError("internal=True is obsolete: Design V0.2 has no journal-based pairing restrictions")
    if type(repeats_per_pair) is not int or repeats_per_pair != 0:
        raise ValueError("Design V0.2 forbids repeated comparisons; repeats_per_pair must be 0")
    records = sorted(articles, key=lambda a: a.doi)
    ids = [a.doi for a in records]
    if len(ids) != len(set(ids)) or any(not identifier for identifier in ids):
        raise ValueError("Articles must have unique nonempty DOI identifiers")
    journals = sorted({a.journal_name for a in records})
    if (mode == "single" and len(journals) != 1) or (mode == "multi" and len(journals) < 2):
        raise ValueError("Journal count does not match mode")
    n = len(records)
    k = choose_regular_degree(n, budget)
    edges, randomization = _regular_edges(n, k, seed)
    plan_mode = f"{mode}_k_regular"
    context = repr((ALGORITHM_VERSION, plan_mode, seed, budget,
                    [(a.doi, a.journal_name) for a in records]))
    run_id = hashlib.sha256(context.encode()).hexdigest()[:20]
    pairs, events = [], []
    exposure = dict.fromkeys(ids, 0)
    article_neighbours = {doi: set() for doi in ids}
    journal_neighbours = {journal: set() for journal in journals}
    for a, b in edges:
        left, right = records[a], records[b]
        pair_id = hashlib.sha256((left.doi + "\0" + right.doi).encode()).hexdigest()
        pairs.append(Pair(pair_id, left.doi, right.doi, left.journal_name, right.journal_name))
        events.append(ComparisonEvent(f"{run_id}:{len(events)+1}", pair_id, 1))
        exposure[left.doi] += 1
        exposure[right.doi] += 1
        article_neighbours[left.doi].add(right.doi)
        article_neighbours[right.doi].add(left.doi)
        if left.journal_name != right.journal_name:
            journal_neighbours[left.journal_name].add(right.journal_name)
            journal_neighbours[right.journal_name].add(left.journal_name)
    return ComparisonPlan(
        mode=plan_mode, seed=seed, budget=budget,
        eligible_unique_pairs=n * (n - 1) // 2, independent_article_count=n,
        pairs=tuple(pairs), events=tuple(events),
        pair_event_counts={p.pair_id: 1 for p in pairs}, article_comparison_counts=exposure,
        journal_components=_components(journal_neighbours), k=k,
        article_components=_components(article_neighbours), randomization=randomization,
    )


def summarize_events(plan: ComparisonPlan, executed_event_ids: Iterable[str] = ()) -> dict:
    """Count only explicitly supplied executed events; no model is invoked.

    A partially executed regular plan need not yet have regular actual exposure.
    Technical retries require separate execution-layer attempt records; duplicate
    event IDs are rejected rather than treated as additional comparisons.
    """
    requested = list(executed_event_ids)
    if len(requested) != len(set(requested)):
        raise ValueError("Executed event IDs must be unique")
    events = {e.event_id: e for e in plan.events}
    if set(requested) - events.keys():
        raise ValueError("Unknown comparison event ID")
    counts = Counter(events[i].pair_id for i in requested)
    pairs = {p.pair_id: p for p in plan.pairs}
    exposure = dict.fromkeys(plan.article_comparison_counts, 0)
    for pair_id, count in counts.items():
        pair = pairs[pair_id]
        exposure[pair.article_a] += count
        exposure[pair.article_b] += count
    return {"U": len(counts), "T": len(requested), "R": len(requested)-len(counts),
            "pair_event_counts": dict(counts), "article_comparison_counts": exposure,
            "participation_statistics": participation_statistics(exposure),
            "independent_article_count": plan.independent_article_count,
            "target_comparisons": plan.budget, "planned_K": plan.k}

