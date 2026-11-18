"""Immutable instruction-version selection and operator-supplied identity records."""
import hashlib
import json
from datetime import datetime


def resolve_versions(repository,prompt_version,rubric_version,*,prompt_sha256=None,rubric_sha256=None):
    registry=json.loads((repository/'references/version_registry.json').read_text(encoding='utf-8'))
    if [prompt_version,rubric_version] not in registry['compatible_pairs']:
        raise ValueError('Unsupported or incompatible Prompt/Rubric version pair')
    result={}
    for kind,version,expected in (('prompt',prompt_version,prompt_sha256),('rubric',rubric_version,rubric_sha256)):
        entry=registry['versions'][version]
        path=(repository/entry['path']).resolve()
        if not path.is_relative_to(repository.resolve()):
            raise ValueError('Version file must remain in repository')
        content=path.read_bytes()
        actual=hashlib.sha256(content).hexdigest()
        if actual!=entry['sha256'] or (expected is not None and expected!=actual):
            raise ValueError(f'{kind} SHA-256 mismatch')
        text=content.decode('utf-8-sig')
        if kind=='prompt' and text.splitlines()[0].rsplit(' — ',1)[-1]!=version:
            raise ValueError('Prompt header/version mismatch')
        if kind=='rubric' and f'Version: {version}' not in text.splitlines():
            raise ValueError('Rubric header/version mismatch')
        result[kind]=dict(path=path,version=version,sha256=actual,text=text)
    return result


def validate_identity(config):
    """Validate RECORD completeness/consistency, not truth of external evidence."""
    record=config.get('model_identity_verification')
    if record is None:
        if config['prompt_version']=='jqa.assessment.v1.2':
            raise ValueError('New pilot requires model_identity_verification record')
        return dict(verification_scope='unverified',checked_at=None,
                    evidence_basis='legacy_note_only',metadata_copy_is_identity_verification=False)
    required={'checked_at','environment_visible_name','environment_visible_version',
              'version_information_source','verification_scope','evidence_basis','evidence_references'}
    if not isinstance(record,dict) or set(record)!=required:
        raise ValueError('Invalid identity verification fields')
    for key in ('environment_visible_name','environment_visible_version','version_information_source'):
        if not isinstance(record[key],str) or not record[key].strip():
            raise ValueError(f'Identity record requires {key}')
    scope=record['verification_scope']
    if scope not in ('unverified','name_only','name_and_version'):
        raise ValueError('Invalid identity verification scope')
    if record['evidence_basis'] not in ('unavailable','environment_display','provider_execution_record','operator_attestation'):
        raise ValueError('Model self-report or task-metadata copying is not identity evidence')
    refs=record['evidence_references']
    if not isinstance(refs,list) or any(not isinstance(r,str) or not r.strip() for r in refs):
        raise ValueError('evidence_references must be a list of nonempty local references')
    checked=record['checked_at']
    if checked is not None:
        if not isinstance(checked,str):
            raise ValueError('checked_at must be an aware ISO timestamp or null')
        value=datetime.fromisoformat(checked.replace('Z','+00:00'))
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError('checked_at requires a timezone')
    if scope=='unverified':
        if config['model_version']!='unknown':
            raise ValueError('Unverified model version must remain unknown')
    else:
        if not checked or record['evidence_basis']=='unavailable' or not refs:
            raise ValueError('Claimed identity scope requires time, source and evidence references')
        if config['model_name']=='unknown' or record['environment_visible_name']!=config['model_name']:
            raise ValueError('Configured name differs from identity record')
        if scope=='name_only' and config['model_version']!='unknown':
            raise ValueError('Name-only verification cannot verify a model version')
        if scope=='name_and_version' and (config['model_version']=='unknown' or
                record['environment_visible_version']!=config['model_version'] or
                record['version_information_source'] in ('unknown','unavailable','not_exposed')):
            raise ValueError('Known version requires matching environment/version source')
    return {**record,'metadata_copy_is_identity_verification':False,
            'record_is_operator_supplied':True,'evidence_truth_automatically_verified':False}
