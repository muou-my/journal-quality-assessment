"""Offline synthetic tests: python -m unittest discover -s tests -v."""
import csv
import tempfile
import unittest
from pathlib import Path

from core.data_processing import Article, load_csv, write_quality_report
from core.pairwise_comparison import generate_comparison_plan, summarize_events


def articles(journals):
    return [Article(f"10.1234/{i}", journal, f"Title {i}", "Abstract", 2025, i+2)
            for i, journal in enumerate(journals)]


class DataTests(unittest.TestCase):
    def test_quality_and_source_preservation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "input.csv"
            rows = [
                ["10.1234/a", "A", "Title", "Abstract", "2025"],
                ["https://doi.org/10.1234/A", "A", "Title", "Abstract", "2025"],
                ["10.1234/b", "A", "Title", "Abstract", "2025"],
                ["10.1234/b", "B", "Different", "Abstract", "2025"],
                ["bad", "A", "", "", "bad"],
            ]
            with path.open("w", newline="", encoding="utf-8-sig") as stream:
                writer = csv.writer(stream)
                writer.writerow(["doi", "journal_name", "title", "abstract", "year"])
                writer.writerows(rows)
            original = path.read_bytes()
            result = load_csv(path)
            self.assertEqual(len(result.articles), 1)
            self.assertEqual(len(result.raw_rows), 5)
            self.assertEqual(len(result.report["duplicate_dois"]), 2)
            self.assertEqual(result.report["metadata_conflicts"][0]["fields"], ["journal_name", "title"])
            self.assertEqual(result.report["excluded_rows"], [3, 4, 5, 6])
            self.assertIn("invalid_year", result.report["row_issues"][0]["errors"])
            write_quality_report(result, Path(directory) / "report.json")
            with self.assertRaises(ValueError):
                write_quality_report(result, path)
            self.assertEqual(path.read_bytes(), original)

    def test_missing_columns(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "input.csv"
            path.write_text("doi,title\n10.1234/a,Title\n", encoding="utf-8")
            result = load_csv(path)
            self.assertFalse(result.articles)
            self.assertIn("abstract", result.report["missing_columns"])


class PairTests(unittest.TestCase):
    def test_complete_plan_and_execution_counts(self):
        plan = generate_comparison_plan(articles(["A"]*10), mode="single")
        self.assertEqual(plan.planned_counts, {"U":45,"T":45,"R":0})
        self.assertEqual(plan.k, 9)
        self.assertEqual(set(plan.article_comparison_counts.values()), {9})
        self.assertEqual(summarize_events(plan)["T"], 0)
        actual = summarize_events(plan, [e.event_id for e in plan.events])
        self.assertEqual({k: actual[k] for k in ("U","T","R")}, plan.planned_counts)
        self.assertEqual(actual["participation_statistics"], {"min":9,"max":9,"mean":9,"std":0})
        partial = summarize_events(plan, [plan.events[0].event_id])
        self.assertEqual(partial["T"], 1)
        self.assertEqual(sum(partial["article_comparison_counts"].values()), 2)

    def test_single_and_multi_share_edge_design(self):
        single = generate_comparison_plan(articles(["A"]*8), mode="single", budget=9)
        multi = generate_comparison_plan(articles(["A"]*3+["B"]*5), mode="multi", budget=9)
        self.assertEqual([(p.article_a,p.article_b) for p in single.pairs],
                         [(p.article_a,p.article_b) for p in multi.pairs])
        full = generate_comparison_plan(articles(["A","A","B","B"]), mode="multi")
        self.assertEqual(full.planned_counts, {"U":6,"T":6,"R":0})
        self.assertTrue(any(p.journal_a==p.journal_b for p in full.pairs))
        self.assertTrue(any(p.journal_a!=p.journal_b for p in full.pairs))

    def test_default_three_articles(self):
        plan = generate_comparison_plan(articles(["A"]*3), mode="single")
        self.assertEqual(plan.planned_counts, {"U":3,"T":3,"R":0})
        self.assertEqual(plan.independent_article_count, 3)
        self.assertEqual(sum(plan.article_comparison_counts.values()), 6)

    def test_invalid_inputs(self):
        for kwargs in ({"budget":-1}, {"budget":1.5}, {"seed":"x"},
                       {"repeats_per_pair":1}, {"repeats_per_pair":True},
                       {"repeats_per_pair":-1}, {"internal":True}):
            with self.assertRaises(ValueError):
                generate_comparison_plan(articles(["A","A"]), mode="single", **kwargs)
        for data, mode in ((articles(["A"]),"single"), ([],"single"),
                           (articles(["A","B"]),"single"),
                           (articles(["A","A"]),"multi")):
            with self.assertRaises(ValueError):
                generate_comparison_plan(data, mode=mode)
        data = articles(["A","A"])
        with self.assertRaises(ValueError):
            generate_comparison_plan([data[0],data[0]], mode="single")
        plan = generate_comparison_plan(data, mode="single", budget=0)
        self.assertEqual(plan.planned_counts, {"U":0,"T":0,"R":0})
        with self.assertRaises(ValueError):
            summarize_events(plan,["unknown"])
        plan = generate_comparison_plan(data, mode="single")
        with self.assertRaises(ValueError):
            summarize_events(plan,[plan.events[0].event_id]*2)


if __name__ == "__main__":
    unittest.main()
