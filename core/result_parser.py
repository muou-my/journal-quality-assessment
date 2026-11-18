"""Strict offline JSON/JSONL result validation. No score coercion."""
import json


class ResultError(ValueError):
    pass


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ResultError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def decode_json(raw):
    try:
        return json.loads(raw, object_pairs_hook=_object,
                          parse_constant=lambda x: (_ for _ in ()).throw(ResultError(f"invalid constant: {x}")))
    except (ValueError, TypeError) as exc:
        raise ResultError(str(exc)) from exc


def parse_result(raw, expected_task):
    value = decode_json(raw)
    required = {'schema_version', 'task_id', 'evaluation_run_id', 'model_name',
                'model_version', 'prompt_version', 'articles'}
    if not isinstance(value, dict) or set(value) != required:
        raise ResultError('invalid result fields')
    if value['schema_version'] != 'jqa.result.v1':
        raise ResultError('unsupported schema_version')
    for field in ('task_id', 'evaluation_run_id', 'model_name', 'model_version', 'prompt_version'):
        if value[field] != expected_task[field]:
            raise ResultError(f'{field} mismatch')
    rows = value['articles']
    if not isinstance(rows, list) or len(rows) != 2:
        raise ResultError('exactly two articles required')
    ids = []
    for row in rows:
        if not isinstance(row, dict) or set(row) != {'article_id', 'originality', 'significance', 'rigour', 'evidence_note', 'uncertainty'}:
            raise ResultError('invalid article fields')
        if not isinstance(row['article_id'], str):
            raise ResultError('invalid article_id')
        ids.append(row['article_id'])
        for field in ('originality', 'significance', 'rigour'):
            if type(row[field]) is not int or not 1 <= row[field] <= 4:
                raise ResultError(f'{field} must be integer 1–4')
        if not isinstance(row['evidence_note'], str) or not row['evidence_note'].strip():
            raise ResultError('evidence_note must be nonempty text')
        if row['uncertainty'] not in ('low', 'medium', 'high'):
            raise ResultError('invalid uncertainty')
    if len(set(ids)) != 2 or set(ids) != {a['article_id'] for a in expected_task['articles']}:
        raise ResultError('article_id mismatch')
    return value


def read_jsonl(path):
    """Yield (line_number, raw_text); blank records are invalid, not skipped."""
    with open(path, encoding='utf-8-sig') as stream:
        for number, line in enumerate(stream, 1):
            yield number, line.rstrip('\r\n')
