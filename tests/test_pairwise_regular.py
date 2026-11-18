"""Independent checks of public plans; no production validator used by oracle."""
import unittest
from itertools import combinations
from unittest.mock import patch

from core.data_processing import Article
from core.pairwise_comparison import (
    GraphConstructionError, choose_regular_degree, generate_comparison_plan,
    validate_regular_graph,
)


def papers(n):
    return [Article(f'10.1234/{i:06}', 'A', 'Title', 'Abstract', 2025, i+2) for i in range(n)]


def independently_verify(plan):
    """Rebuild neighbours, components and event-to-pair counts from public output."""
    neighbours = {a:set() for a in plan.article_comparison_counts}
    unordered = set()
    by_pair = {}
    for pair in plan.pairs:
        a, b = pair.article_a, pair.article_b
        if a == b or a not in neighbours or b not in neighbours:
            raise AssertionError('self-loop or unknown vertex')
        key = frozenset((a,b))
        if key in unordered or pair.pair_id in by_pair:
            raise AssertionError('duplicate edge or pair ID')
        unordered.add(key)
        by_pair[pair.pair_id] = 0
        neighbours[a].add(b)
        neighbours[b].add(a)
    counts = {a:len(v) for a,v in neighbours.items()}
    if counts != plan.article_comparison_counts or set(counts.values()) != {plan.k}:
        raise AssertionError('degrees are not exactly K')
    event_ids = set()
    for event in plan.events:
        if event.event_id in event_ids or event.repetition_index != 1 or event.pair_id not in by_pair:
            raise AssertionError('invalid or repeated event')
        event_ids.add(event.event_id)
        by_pair[event.pair_id] += 1
    if any(c != 1 for c in by_pair.values()) or by_pair != plan.pair_event_counts:
        raise AssertionError('not one comparison per unique pair')
    if len(unordered)*2 != len(counts)*plan.k:
        raise AssertionError('handshake identity failed')
    unseen = set(neighbours)
    components = set()
    while unseen:
        queue = [next(iter(unseen))]
        component = set()
        while queue:
            current = queue.pop()
            if current not in component:
                component.add(current)
                queue.extend(neighbours[current]-component)
        unseen -= component
        components.add(frozenset(component))
    if components != {frozenset(c) for c in plan.article_components}:
        raise AssertionError('incorrect components')


class RegularGraphTests(unittest.TestCase):
    def test_required_sizes(self):
        for n,k,total in ((100,99,4950), (500,400,100000), (5923,34,100691)):
            with self.subTest(n=n):
                plan = generate_comparison_plan(papers(n), mode='single', seed=42)
                self.assertEqual(plan.k,k)
                self.assertEqual(plan.planned_counts, {'U':total,'T':total,'R':0})
                self.assertEqual(plan.eligible_unique_pairs,n*(n-1)//2)
                self.assertEqual(plan.design_summary['target_comparisons'],100000)
                self.assertEqual(plan.design_summary['participation_statistics']['std'],0)
                independently_verify(plan)

    def test_all_small_feasible_degrees(self):
        # Covers complete/empty, odd/even N and K, complement and matching paths.
        for n in range(2,17):
            for k in range(n):
                if n*k % 2:
                    continue
                for seed in (0,7):
                    with self.subTest(n=n,k=k,seed=seed):
                        plan = generate_comparison_plan(papers(n),mode='single',budget=n*k//2,seed=seed)
                        self.assertEqual(plan.k,k)
                        independently_verify(plan)

    def test_exhaustive_independent_small_graph_oracle(self):
        # Enumerate ALL simple labelled graphs up to N=5 to find feasible degrees,
        # independently of the production parity rule or construction algorithm.
        for n in range(2,6):
            possible = set()
            edges = list(combinations(range(n),2))
            for mask in range(1 << len(edges)):
                degree = [0]*n
                for index,(a,b) in enumerate(edges):
                    if mask & (1 << index):
                        degree[a]+=1; degree[b]+=1
                if min(degree)==max(degree):
                    possible.add(degree[0])
            for target in range(n*(n-1)//2+3):
                expected = min(possible,key=lambda k:(abs(n*k-2*target),k))
                self.assertEqual(choose_regular_degree(n,target),expected)
                independently_verify(generate_comparison_plan(papers(n),mode='single',budget=target))

    def test_nearest_degree_and_tie_policy(self):
        for n in range(2,50):
            for target in (0,1,9,100,100000):
                feasible = [k for k in range(n) if n*k%2==0]
                best = min(feasible,key=lambda k:(abs(n*k-2*target),k))
                self.assertEqual(choose_regular_degree(n,target),best)
        self.assertEqual(choose_regular_degree(8,10),2)  # 8 and 12 are equally near.
        self.assertEqual(choose_regular_degree(448),446)

    def test_reproducibility_and_seed_effect(self):
        data = papers(24)
        original = list(data)
        a = generate_comparison_plan(data,mode='single',budget=72,seed=21)
        b = generate_comparison_plan(reversed(data),mode='single',budget=72,seed=21)
        c = generate_comparison_plan(data,mode='single',budget=72,seed=22)
        self.assertEqual(a,b)
        self.assertNotEqual(a.pairs,c.pairs)
        self.assertEqual(data,original)
        independently_verify(a)
        self.assertGreater(a.randomization['accepted_switches'],0)
        full1 = generate_comparison_plan(papers(10),mode='single',seed=1)
        full2 = generate_comparison_plan(papers(10),mode='single',seed=2)
        self.assertEqual(full1.pairs,full2.pairs)  # Complete graph has no randomness.

    def test_disconnected_graph_is_reported_not_rejected(self):
        plan = generate_comparison_plan(papers(6),mode='single',budget=3)
        self.assertEqual(plan.k,1)
        self.assertEqual(len(plan.article_components),3)
        independently_verify(plan)
        empty = generate_comparison_plan(papers(5),mode='single',budget=0)
        self.assertEqual(len(empty.article_components),5)

    def test_validator_rejects_bad_graphs(self):
        for n,k,edges in ((4,2,[(0,0)]), (4,1,[(0,1),(1,0)]),
                          (4,1,[(0,1)]), (4,1,[(0,4),(1,2)]),
                          (3,1,[(0,1)])):
            with self.assertRaises(GraphConstructionError):
                validate_regular_graph(n,k,edges)
        with patch('core.pairwise_comparison.validate_regular_graph',
                   side_effect=GraphConstructionError('injected invalid construction')):
            with self.assertRaisesRegex(GraphConstructionError,'injected'):
                generate_comparison_plan(papers(10),mode='single',budget=20)


if __name__ == '__main__':
    unittest.main()
