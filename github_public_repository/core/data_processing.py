"""Read-only CSV validation for the V0.2 engine (standard library only)."""
from __future__ import annotations

import csv
import json
import re
from collections import defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path

REQUIRED_FIELDS = ("doi", "journal_name", "title", "abstract", "year")


def normalize_doi(value: str) -> str:
    """Normalize common DOI representations without changing the source file."""
    return re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", "", value.strip(), flags=re.I).lower()


@dataclass(frozen=True)
class Article:
    doi: str
    journal_name: str
    title: str
    abstract: str
    year: int
    source_row: int


@dataclass
class DataProcessingResult:
    articles: tuple[Article, ...]
    raw_rows: tuple[dict, ...]
    report: dict


def load_csv(path: str | Path, *, encoding: str = "utf-8-sig",
             min_year: int = 1, max_year: int = 9999) -> DataProcessingResult:
    """Validate CSV, deduplicate identical records, and exclude all DOI conflicts.

    Row numbers are CSV record numbers (header is 1), not physical lines for
    multiline abstracts. Year bounds are explicit, not dependent on today's date.
    Missing/duplicate headers return a report with no eligible articles.
    File, decoding, and malformed CSV errors propagate to the caller.
    """
    if not 1 <= min_year <= max_year <= 9999:
        raise ValueError("Invalid year bounds")
    source = Path(path).resolve()
    with source.open(newline="", encoding=encoding) as handle:
        reader = csv.DictReader(handle, strict=True)
        headers = reader.fieldnames or []
        rows = tuple(dict(row) for row in reader)
    missing = sorted(set(REQUIRED_FIELDS) - set(headers))
    duplicate_headers = sorted({h for h in headers if headers.count(h) > 1})
    issues: list[dict] = []
    excluded: set[int] = set()
    candidates: dict[int, Article] = {}
    groups: dict[str, list[int]] = defaultdict(list)
    normalized: dict[int, dict] = {}
    for number, row in enumerate(rows, 2):
        values = {key: (row.get(key) or "").strip() for key in REQUIRED_FIELDS}
        values["doi"] = normalize_doi(values["doi"])
        normalized[number] = values
        if values["doi"]:
            groups[values["doi"]].append(number)
        errors = [f"missing_{key}" for key in REQUIRED_FIELDS if not values[key]]
        if None in row or any(value is None for value in row.values()):
            errors.append("column_count_mismatch")
        if values["doi"] and not re.fullmatch(r"10\.\d{4,9}/\S+", values["doi"]):
            errors.append("invalid_doi")
        year = values["year"]
        if not re.fullmatch(r"[0-9]{1,4}", year) or not min_year <= int(year) <= max_year:
            errors.append("invalid_year")
        if errors:
            excluded.add(number)
            issues.append({"row": number, "errors": errors})
        else:
            candidates[number] = Article(**{**values, "year": int(year)}, source_row=number)
    duplicates, conflicts = [], []
    for doi, numbers in groups.items():
        if len(numbers) < 2:
            continue
        differing = [field for field in REQUIRED_FIELDS[1:]
                     if len({normalized[n][field] for n in numbers}) > 1]
        duplicates.append({"doi": doi, "rows": numbers})
        if differing:
            conflicts.append({"doi": doi, "rows": numbers, "fields": differing})
            excluded.update(numbers)
        else:
            # Keep the first valid representative, not an invalid arbitrary row.
            valid = [n for n in numbers if n in candidates]
            excluded.update(n for n in numbers if not valid or n != valid[0])
    if missing or duplicate_headers:
        excluded.update(range(2, len(rows) + 2))
    articles = tuple(a for n, a in candidates.items() if n not in excluded)
    report = {
        "source": str(source), "encoding": encoding, "total_rows": len(rows),
        "valid_articles": len(articles), "excluded_rows": sorted(excluded),
        "missing_columns": missing, "duplicate_headers": duplicate_headers,
        "row_issues": issues, "duplicate_dois": duplicates,
        "metadata_conflicts": conflicts,
        "policy": "Normalize DOI; trim fields; deduplicate identical required metadata; exclude every conflicting DOI row.",
    }
    return DataProcessingResult(articles, rows, report)


def write_quality_report(result: DataProcessingResult, path: str | Path) -> None:
    """Write a separate UTF-8 JSON report, never overwrite the input CSV."""
    destination = Path(path).resolve()
    if destination == Path(result.report["source"]).resolve():
        raise ValueError("Report must not overwrite input CSV")
    destination.write_text(json.dumps(result.report, ensure_ascii=False, indent=2), encoding="utf-8")
